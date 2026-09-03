# main.py
import argparse
import asyncio
import difflib
import hashlib
import io
import json
import os
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import aiohttp
import pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm
import gender_guesser.detector as gender_guesser

try:
    import pdfplumber
except Exception:  # pragma: no cover - optional at runtime
    pdfplumber = None

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional at runtime
    OpenAI = None

from config import (
    STATE, MAX_CONCURRENT, CACHE_DIR, OUTPUT_CSV,
    OPENAI_MODEL, GEMINI_MODEL, GEMINI_BASE_URL,
    ARCHIVE_YEAR_FLOOR, MAX_DOCS_PER_YEAR, MAX_DOCS_KEEP, MAX_DOCS_SCAN,
)
from counties import COUNTIES
from cache import cache

CURRENT_YEAR = datetime.now().year
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# aiohttp session is created in main() and shared by all workers.
session: aiohttp.ClientSession = None
# Optional Playwright browser for JS-rendered county sites (shared).
_playwright = None
_browser = None
_browser_lock = asyncio.Lock()
_RENDER_SEM = asyncio.Semaphore(3)

COLUMNS = [
    "state", "county", "name", "status", "term_start", "term_end",
    "gender", "tenure",
]

# Timeout stays at the plan's 30s total, but a shorter connect budget keeps the
# crawl from hanging on dead hosts (this is the main speed fix).
TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=25)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# PHASE 1: URL generation
# ---------------------------------------------------------------------------
def candidate_urls(county):
    base = county.lower().replace(" ", "")
    return [
        f"https://www.{base}county.sc.gov",
        f"https://www.{base}countysc.gov",
        f"https://www.{base}countygov.com",
        f"https://www.{base}county.org",
        f"https://{base}county.sc.gov",
        f"https://{base}countysc.gov",
        f"https://www.{base}county.com",
        f"https://www.{county.lower()}.sc.gov",
        f"https://www.{base}county.gov",
    ]


# ---------------------------------------------------------------------------
# PHASE 2: async HTTP client (with diskcache-backed persistence)
# ---------------------------------------------------------------------------
def _needs_js_render(html):
    """True when static HTML looks like an empty SPA shell / JS-gated page."""
    if not html:
        return True
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    links = soup.find_all("a", href=True)
    if len(text) < 500 and len(links) < 8:
        return True
    low = html.lower()
    spa_markers = (
        'id="root"',
        "id='root'",
        'id="app"',
        "id='app'",
        "ng-app",
        "data-reactroot",
        "__next",
        "window.__NUXT",
    )
    if any(m.lower() in low for m in spa_markers) and len(text) < 2000:
        return True
    if "enable javascript" in low or "requires javascript" in low:
        return True
    return False


async def _ensure_browser():
    """Lazily start a shared Chromium instance for rendered fetches."""
    global _playwright, _browser
    async with _browser_lock:
        if _browser is not None:
            return _browser
        try:
            from playwright.async_api import async_playwright
        except Exception:
            return None
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        return _browser


async def _close_browser():
    global _playwright, _browser
    async with _browser_lock:
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None
        if _playwright is not None:
            try:
                await _playwright.stop()
            except Exception:
                pass
            _playwright = None


async def fetch_rendered(url):
    """Fetch page HTML after JS execution via Playwright. Cached under RENDER::."""
    ckey = "RENDER::" + url
    if ckey in cache:
        return cache[ckey]
    browser = await _ensure_browser()
    if browser is None:
        return None
    text = None
    async with _RENDER_SEM:
        context = None
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (compatible; boza-scraper/1.0; "
                    "+https://github.com/coolgithub1/scaled-sc-scrape)"
                ),
                ignore_https_errors=True,
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            # Give late SPA route/content a beat to paint.
            await page.wait_for_timeout(750)
            text = await page.content()
        except Exception:
            text = None
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
    if text is not None:
        cache[ckey] = text
    return text


async def fetch(url, allow_render=False):
    """Fetch text for url. Checks cache first (key = url), then retries up to 3x.

    When allow_render=True and the static body looks JS-gated, fall back to
    Playwright (Chromium) so SPA / CivicEngage / Drupal AJAX pages still yield
    roster and agenda links.
    """
    if url in cache:
        text = cache[url]
        if allow_render and _needs_js_render(text):
            rendered = await fetch_rendered(url)
            if rendered and not _needs_js_render(rendered):
                return rendered
        return text
    text = None
    for attempt in range(3):  # max 3 retries
        try:
            async with session.get(url, timeout=TIMEOUT, allow_redirects=True) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="ignore")
                    break
                if resp.status in RETRYABLE_STATUS:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                break  # definitive non-200 (e.g. 404): do not retry
        except Exception:
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
    if text is not None:
        cache[url] = text
    if allow_render and _needs_js_render(text):
        rendered = await fetch_rendered(url)
        if rendered and (text is None or not _needs_js_render(rendered)):
            return rendered
    return text


async def fetch_bytes(url):
    """Fetch raw bytes for url (used for PDFs). Cached under a BYTES:: key."""
    ckey = "BYTES::" + url
    if ckey in cache:
        return cache[ckey]
    data = None
    for attempt in range(3):  # max 3 retries
        try:
            async with session.get(url, timeout=TIMEOUT, allow_redirects=True) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    break
                if resp.status in RETRYABLE_STATUS:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                break
        except Exception:
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
    if data is not None:
        cache[ckey] = data
    return data


# ---------------------------------------------------------------------------
# PHASE 3: locate and parse the current BOZA members page
# ---------------------------------------------------------------------------
BOZA_PATHS = [
    "/boards/zoning",
    "/departments/planning-zoning/board-of-zoning-appeals",
    "/government/boards-commissions",
    "/planning/board-of-zoning-appeals",
    "/zoning-board",
]

TERM_RE = re.compile(r"(\d{4})[-\u2013](\d{4})")
KEYWORD_RE = re.compile(r"Term:|Appointed:|Expires:", re.IGNORECASE)
NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)\b")


def _looks_like_boza(html):
    if not html:
        return False
    low = html.lower()
    # Soft-404 / error / interstitial pages often still mention zoning in the nav.
    if any(
        bad in low
        for bad in (
            "404 web page error",
            "page not found",
            "404 error",
            "404.0 - not found",
            "aspxerrorpath",
            ">redirecting",
            "redirecting...",
            "error - 404",
        )
    ):
        return False
    title_m = re.search(r"<title[^>]*>([^<]+)", html, re.I)
    if title_m:
        title = title_m.group(1).lower()
        if any(bad in title for bad in ("404", "not found", "error", "redirect")):
            return False
    return "zoning" in low and "appeal" in low


async def _reachable_bases(county):
    """Probe all candidate roots concurrently; return [(base, root_html), ...]."""
    urls = candidate_urls(county)
    # County homepages are often JS shells — allow Playwright fallback.
    roots = await asyncio.gather(*(fetch(u, allow_render=True) for u in urls))
    return [(u, html) for u, html in zip(urls, roots) if html]


def _homepage_boza_links(base, root_html):
    """Scan homepage nav for links that look like a zoning/appeals board page."""
    soup = BeautifulSoup(root_html, "html.parser")
    scored = []
    for a in soup.find_all("a", href=True):
        text = (a.get_text(" ", strip=True) or "").lower()
        href = a["href"].lower()
        blob = text + " " + href
        score = ("zoning" in blob) + ("appeal" in blob) + ("board" in blob)
        if score >= 2:
            scored.append((score, urljoin(base, a["href"])))
    scored.sort(key=lambda item: item[0], reverse=True)
    seen, links = set(), []
    for _, link in scored:
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links[:5]


async def find_boza_page(county):
    """Return (base_url, boza_url, html). base_url is set whenever a root loads."""
    bases = await _reachable_bases(county)
    if not bases:
        return None, None, None
    primary_base = bases[0][0]

    for base, root_html in bases:
        # 1. Explicit candidate paths (fetched concurrently).
        paths_html = await asyncio.gather(
            *(fetch(base + p, allow_render=True) for p in BOZA_PATHS)
        )
        for path, html in zip(BOZA_PATHS, paths_html):
            if html and _looks_like_boza(html):
                return base, base + path, html

        # 2. Homepage navigation links.
        for link in _homepage_boza_links(base, root_html):
            html = await fetch(link, allow_render=True)
            if html and _looks_like_boza(html):
                return base, link, html

        # 3. Site-search fallback.
        search_url = base + "/search?q=Board+of+Zoning+Appeals"
        html = await fetch(search_url, allow_render=True)
        if html:
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].lower()
                if "zoning" in href and "appeal" in href:
                    target = urljoin(base, a["href"])
                    page = await fetch(target, allow_render=True)
                    if page:
                        return base, target, page

    return primary_base, None, None


DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")

# Words that mark a string as an institution/section title rather than a person.
_NON_PERSON_WORDS = {
    "board", "zoning", "appeals", "appeal", "meeting", "meetings", "minutes",
    "agenda", "agendas", "committee", "commission", "department", "county",
    "planning", "council", "office", "division", "authority", "court", "clerk",
    "city", "town", "member", "members", "title", "video", "file", "files",
    "land", "use", "development", "services", "district", "property", "owner",
    "applicant", "application", "variance", "hardship", "exception", "request",
    "information", "signature", "signed", "email", "phone", "address", "other",
    "instructions", "form", "yes", "site", "subject", "location", "map",
}


def _is_person(name):
    if not name:
        return False
    # Form fields / PDF boilerplate leave underscores, blanks, punctuation junk.
    if re.search(r"[_=/\\]|_{2,}|\({3,}|\d{3,}", name):
        return False
    if len(name) > 60:
        return False
    if name.isupper() and len(name.split()) >= 2:
        # "PROPERTY OWNER" etc.
        return False
    tokens = [t.strip(".").lower() for t in name.replace("(", " ").replace(")", " ").split()]
    tokens = [t for t in tokens if t]
    if any(t in _NON_PERSON_WORDS for t in tokens):
        return False
    alpha = [t for t in tokens if t.isalpha() and len(t) >= 2]
    if len(alpha) < 2:
        return False
    # Require mostly capitalized person-name tokens (reject sentence fragments).
    raw_tokens = [t.strip(".,'") for t in name.replace("(", " ").replace(")", " ").split()]
    named = [t for t in raw_tokens if t.isalpha() and len(t) >= 2]
    if not named:
        return False
    caps = sum(1 for t in named if t[0].isupper())
    return caps >= max(2, len(named) - 1)


def _extract_name(text):
    match = NAME_RE.search(text)
    if not match:
        return None
    name = match.group(1)
    return name if _is_person(name) else None


def _clean_name(raw):
    """Extract a person name from a roster cell that may include an address/phone."""
    raw = re.sub(r"District\s*#?\s*\d+", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"At[-\s]?Large", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"Seat\s*#?\s*\d+", " ", raw, flags=re.IGNORECASE)
    # Keep quoted nicknames as plain tokens ("Ray" -> Ray) so minutes aliases match.
    raw = re.sub(r"[\"\u201c\u201d\u2018\u2019']", " ", raw)
    # An address/phone starts with a digit; cut the cell there.
    raw = re.split(r"\d", raw, 1)[0]
    tokens = re.findall(r"[A-Z][a-zA-Z.'\-]*", raw)
    if len(tokens) < 2:
        return None
    name = " ".join(tokens[:4]).strip()
    return name if _is_person(name) else None


def _years_from_dates(text):
    years = []
    for m in DATE_RE.finditer(text):
        year = int(m.group(3))
        if year < 100:
            year += 2000 if year <= 50 else 1900
        years.append(year)
    # Also handle "Month YYYY" and bare four-digit years (e.g. "January 2030").
    for m in re.finditer(r"\b(19|20)\d{2}\b", text):
        years.append(int(m.group(0)))
    return years


def _member(county, name, status, term_start, term_end, tenure):
    return {
        "state": STATE,
        "county": county,
        "name": name,
        "status": status,
        "term_start": term_start,
        "term_end": term_end,
        "gender": None,
        "tenure": tenure,
        "_from_roster": True,
    }


def _status_for(term_end):
    if term_end and str(term_end).isdigit() and int(term_end) < CURRENT_YEAR:
        return "historical"
    return "sitting"


def _parse_table(table, county):
    """Parse a roster table by mapping header columns (name / appointed / expires)."""
    out = []
    rows = table.find_all("tr")
    if not rows:
        return out
    header = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]

    def find_col(keys):
        for i, h in enumerate(header):
            if any(k in h for k in keys):
                return i
        return None

    name_i = find_col(["name", "member", "district", "appointee", "commissioner"])
    appt_i = find_col(["first appointed", "appointed", "appointment", "since", "start"])
    exp_i = find_col(["expires", "expiration", "term end", "term expires"])
    has_headers = any(v is not None for v in (name_i, appt_i, exp_i))
    body = rows[1:] if any(header) else rows

    for tr in body:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue
        rowtext = " ".join(cells)
        name_src = cells[name_i] if (name_i is not None and name_i < len(cells)) else rowtext
        name = _clean_name(name_src)
        if not name:
            continue

        term_start = term_end = None
        if appt_i is not None and appt_i < len(cells):
            ys = _years_from_dates(cells[appt_i])
            if ys:
                term_start = str(min(ys))
        if exp_i is not None and exp_i < len(cells):
            ys = _years_from_dates(cells[exp_i])
            if ys:
                term_end = str(max(ys))
        if term_start is None and term_end is None:
            m = TERM_RE.search(rowtext)
            if m:
                term_start, term_end = m.group(1), m.group(2)

        # Accept a row only when it carries a real temporal signal or the table
        # is clearly a roster (name column plus appointment/expiration column).
        # This gate runs BEFORE any year backfill so unrelated tables (e.g. a
        # list of monthly meeting minutes) are not mistaken for members.
        roster_table = name_i is not None and (appt_i is not None or exp_i is not None)
        if term_start is None and term_end is None and not roster_table:
            continue

        # For an accepted roster row, backfill a missing expiry from any year.
        if term_end is None:
            row_years = re.findall(r"\b(?:19|20)\d{2}\b", rowtext)
            if row_years:
                term_end = max(row_years)

        out.append(_member(county, name, _status_for(term_end), term_start, term_end, rowtext[:200]))
    return out


def parse_current_members(html, county):
    members = []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    # 1. Header-mapped roster tables (handles CivicPlus-style member tables).
    for table in soup.find_all("table"):
        members.extend(_parse_table(table, county))

    # 2. Plan heuristic: list items with a name and a Term/Appointed/Expires cue.
    for lst in soup.find_all(["ul", "ol"]):
        for li in lst.find_all("li"):
            text = li.get_text(" ", strip=True)
            if not text:
                continue
            term = TERM_RE.search(text)
            has_keyword = KEYWORD_RE.search(text)
            name = _extract_name(text)
            if not name or not (term or has_keyword):
                continue
            term_start = term.group(1) if term else None
            term_end = term.group(2) if term else None
            members.append(
                _member(county, name, _status_for(term_end), term_start, term_end, text[:200])
            )
    return members


def _member_subpages(boza_url, boza_html):
    """Collect links on the BOZA page whose text mentions 'member' (roster pages)."""
    soup = BeautifulSoup(boza_html, "html.parser")
    targets, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = (a.get_text(" ", strip=True) or "").lower()
        if "member" in text:
            target = urljoin(boza_url, a["href"])
            if target not in seen and target != boza_url:
                seen.add(target)
                targets.append(target)
    return targets[:5]


# ---------------------------------------------------------------------------
# PHASE 4: find historical meeting minutes (incl. year-by-year archives)
# ---------------------------------------------------------------------------
MINUTES_PATHS = [
    "/agendas",
    "/minutes",
    "/government/agendas-minutes",
    "/council/agendas",
    "/AgendaCenter",          # CivicPlus agenda/minutes portal
]
PORTALS = ["legistar.com", "granicus.com", "iqm2.com", "civicplus.com", "civicweb.net"]
DOC_KEYWORDS = ["appointed", "zoning", "bza", "board of zoning appeals"]
# Hints that a link points at an agenda/minutes listing page worth crawling.
AGENDA_HINTS = ["agendacenter", "agenda center", "agenda", "minutes"]
MAX_DOCS_PER_COUNTY = 120

# CivicPlus AgendaCenter year archive endpoint (works without JS).
CIVICPLUS_YEAR_PATH = (
    "/AgendaCenter/UpdateCategoryList?year={year}&month=0&day=0&catID={cat}"
)
# Drupal Views year filter used by counties like Colleton on BOZA pages.
DRUPAL_YEAR_PARAM = "field_meeting_date_value__vc3_content_date_year_offset"


def _is_document_link(url, link_text):
    low = (url + " " + link_text).lower()
    if "viewfile" in low or url.lower().endswith(".pdf"):
        return True
    return any(portal in url.lower() for portal in PORTALS)


def _doc_year(url, link_text=""):
    """Best-effort year from a CivicPlus ViewFile path or PDF filename."""
    blob = f"{url} {link_text}"
    # CivicPlus: /ViewFile/Minutes/_12222020-1595  or  /Agenda/_08252026-2038
    m = re.search(r"ViewFile/(?:Agenda|Minutes?)/_(\d{2})(\d{2})(20\d{2})", blob, re.I)
    if m:
        return int(m.group(3))
    # Filenames like bza-agenda-package-8-5-26.pdf or 2024-03-01-minutes.pdf
    m = re.search(r"(?:^|[^\d])(20\d{2})(?:[^\d]|$)", blob)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:^|[^\d])(\d{1,2})[-_/](\d{1,2})[-_/](\d{2})(?:[^\d]|$)", blob)
    if m:
        yy = int(m.group(3))
        return 2000 + yy if yy <= 50 else 1900 + yy
    m = re.search(r"(?:^|[^\d])(19\d{2})(?:[^\d]|$)", blob)
    return int(m.group(1)) if m else None


def _is_minutes_link(url, link_text=""):
    low = f"{url} {link_text}".lower()
    return "minute" in low


def _is_minutes_document(url, link_text="", content=None):
    """True when a doc looks like meeting minutes/agenda, not an application form."""
    blob = f"{url} {link_text}".lower()
    if any(
        bad in blob
        for bad in (
            "application", "variance-special-exception", "request-form",
            "special-exception-application", "variance_request",
            "variance-request", "petition-form", "requestapplication",
        )
    ):
        return False
    if content:
        head = content[:1200].lower()
        if any(
            bad in head
            for bad in (
                "request application",
                "applicants must complete",
                "application fee",
                "property owner:",
                "variance & special exception",
                "variance request & hardship",
                "hardship information",
                "i do hereby certify",
                "tax map #",
            )
        ):
            return False
        # Real minutes almost always have an attendance block.
        if not re.search(
            r"(?i)members?\s*(present|absent)?|staff\s+present",
            content[:3000],
        ):
            return False
        # Content-only path (URL already vetted): accept when attendance exists
        # and application markers are absent.
        if not blob.strip():
            return True
    return "minute" in blob or "agenda" in blob or "viewfile" in blob


def _content_is_minutes(content):
    """Content-only minutes gate used after a PDF/HTML body is downloaded."""
    return _is_minutes_document("", "", content=content)


def _title_case_name(name):
    """Normalize 'Les green' -> 'Les Green' without wrecking Mc/O' names badly."""
    suffixes = {"jr", "sr", "ii", "iii", "iv", "v"}
    parts = []
    for token in name.split():
        if re.fullmatch(r"[A-Za-z]\.", token):
            parts.append(token.upper())
        elif "'" in token or "\u2019" in token:
            sep = "'" if "'" in token else "\u2019"
            bits = token.split(sep)
            parts.append(sep.join(b[:1].upper() + b[1:].lower() if b else b for b in bits))
        elif token.lower().rstrip(".") in suffixes:
            core = token.rstrip(".")
            parts.append(
                (core.upper() if core.lower() in {"ii", "iii", "iv", "v"} else core.title())
                + ("." if token.endswith(".") else "")
            )
        else:
            parts.append(token[:1].upper() + token[1:].lower() if token else token)
    return " ".join(parts)


def _civicplus_bza_categories(html):
    """Return CivicPlus AgendaCenter category IDs whose label looks like a BZA."""
    if not html:
        return []
    cats = []
    # Checkbox labels: <input name="chkCategoryID" value="6"> Board of Zoning Appeals
    for m in re.finditer(
        r'name=["\']chkCategoryID["\'][^>]*value=["\'](\d+)["\'][^>]*>\s*([^<]+)',
        html,
        re.I,
    ):
        cat_id, label = m.group(1), m.group(2).strip().lower()
        if "zoning" in label and "appeal" in label:
            cats.append(cat_id)
    # Fallback: section headers / changeYear near BZA wording.
    if not cats:
        for m in re.finditer(
            r'(?is)board of zoning appeals.{0,400}?changeYear\(\d+,\s*(\d+)',
            html,
        ):
            cats.append(m.group(1))
    # Preserve order, unique.
    seen, out = set(), []
    for c in cats:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _civicplus_years_for_category(html, cat_id):
    """Years advertised for a CivicPlus category via changeYear(year, catId, ...)."""
    if not html:
        return []
    years = {
        int(y)
        for y in re.findall(
            rf"changeYear\((\d{{4}}),\s*{re.escape(str(cat_id))}\b",
            html,
        )
    }
    # Keep only plausible archive years; newest first (present → historic).
    years = {
        y for y in years
        if ARCHIVE_YEAR_FLOOR <= y <= CURRENT_YEAR + 1
    }
    return sorted(years, reverse=True)


def _drupal_year_options(html):
    """Years listed in a Drupal BOZA meeting-date year filter select."""
    if not html or DRUPAL_YEAR_PARAM not in html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    years = []
    for sel in soup.find_all("select"):
        name = (sel.get("name") or sel.get("id") or "").lower()
        if DRUPAL_YEAR_PARAM not in name and "meeting_date" not in name:
            continue
        for opt in sel.find_all("option"):
            val = (opt.get("value") or opt.get_text(strip=True) or "").strip()
            if re.fullmatch(r"(?:19|20)\d{2}", val):
                year = int(val)
                if ARCHIVE_YEAR_FLOOR <= year <= CURRENT_YEAR + 1:
                    years.append(year)
    return sorted(set(years), reverse=True)


def _fallback_year_list():
    """Present → floor when a portal does not advertise its year list."""
    return list(range(CURRENT_YEAR, ARCHIVE_YEAR_FLOOR - 1, -1))


def _collect_docs_from_html(base, html, seen, assume_bza=False):
    """Extract (url, link_text, year, is_minutes) docs from a listing page."""
    docs = []
    if not html:
        return docs
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        full = urljoin(base, a["href"])
        link_text = a.get_text(" ", strip=True) or ""
        # CivicPlus minutes icons often have empty visible text; aria-label helps.
        if not link_text:
            link_text = a.get("aria-label") or ""
        if not _is_document_link(full, link_text) or full in seen:
            continue
        # Never treat application/variance forms as minutes/agenda docs.
        blob_early = f"{full} {link_text}".lower()
        if any(
            bad in blob_early
            for bad in (
                "application", "variance-special-exception", "request-form",
                "special-exception-application", "variance_request",
                "variance-request", "petition-form",
            )
        ):
            continue
        if not _is_minutes_document(full, link_text):
            # Keep cryptic CivicPlus ViewFile / unlabeled PDFs on BZA pages.
            if not (
                _is_minutes_link(full, link_text)
                or "viewfile" in full.lower()
                or (assume_bza and full.lower().endswith(".pdf"))
            ):
                continue
        blob = f"{full} {link_text}".lower()
        on_bza_path = "zoning" in full.lower() and (
            "appeal" in full.lower() or "board_of_appeals" in full.lower()
        )
        labeled_bza = (
            ("zoning" in blob and "appeal" in blob)
            or re.search(r"\bbza\b", blob) is not None
            or "board of zoning" in blob
            or "land management board of appeals" in blob
        )
        if not (assume_bza or on_bza_path or labeled_bza):
            continue
        year = _doc_year(full, link_text)
        seen.add(full)
        docs.append({
            "url": full,
            "text": link_text,
            "year": year,
            "is_minutes": _is_minutes_link(full, link_text),
        })
    return docs


async def _listing_pages(base, boza_url, boza_html):
    """Collect candidate agenda/minutes listing URLs from paths + page links."""
    listing = set()
    for path in MINUTES_PATHS:
        listing.add(base.rstrip("/") + path)

    home_html = await fetch(base)
    for src_url, html in [(boza_url, boza_html), (base, home_html)]:
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            blob = ((a.get_text(" ", strip=True) or "") + " " + a["href"]).lower()
            if any(hint in blob for hint in AGENDA_HINTS):
                listing.add(urljoin(src_url or base, a["href"]))
    # Case-insensitive de-dupe (/AgendaCenter vs /agendacenter).
    seen, out = set(), []
    for u in listing:
        key = u.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out[:12]


async def _historic_archive_pages(base, listing_htmls, boza_url, boza_html):
    """Build year-by-year archive listing URLs from present back to oldest available."""
    archive_urls = []

    # CivicPlus: UpdateCategoryList for each BZA category × every year the portal lists.
    for html in listing_htmls:
        for cat in _civicplus_bza_categories(html):
            years = _civicplus_years_for_category(html, cat) or _fallback_year_list()
            for year in years:
                archive_urls.append(
                    urljoin(base, CIVICPLUS_YEAR_PATH.format(year=year, cat=cat))
                )

    # Drupal Views year filter on the BOZA page itself (e.g. Colleton).
    drupal_years = _drupal_year_options(boza_html) if boza_html else []
    if boza_url and drupal_years:
        for year in drupal_years:
            sep = "&" if "?" in boza_url else "?"
            archive_urls.append(f"{boza_url}{sep}{DRUPAL_YEAR_PARAM}={year}")

    # De-dupe while preserving order (already newest-first per source).
    seen, out = set(), []
    for u in archive_urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _sample_docs_across_years(docs):
    """Prefer minutes; cover every archive year from present back to oldest."""
    # Newest year first so the crawl walks present → historic.
    def sort_key(d):
        year = d["year"] if d["year"] is not None else CURRENT_YEAR
        return (0 if d["is_minutes"] else 1, -year, d["url"])

    ranked = sorted(docs, key=sort_key)
    per_year = {}
    selected = []
    for doc in ranked:
        year = doc["year"] if doc["year"] is not None else CURRENT_YEAR
        if per_year.get(year, 0) >= MAX_DOCS_PER_YEAR:
            continue
        # Within a year, prefer minutes; skip agendas once we have minutes.
        if (
            not doc["is_minutes"]
            and per_year.get(year, 0) > 0
            and any(s["year"] == year and s["is_minutes"] for s in selected)
        ):
            continue
        selected.append(doc)
        per_year[year] = per_year.get(year, 0) + 1
        if len(selected) >= MAX_DOCS_PER_COUNTY:
            break
    # Download order: minutes first, then present → historic.
    selected.sort(
        key=lambda d: (0 if d["is_minutes"] else 1, -(d["year"] or 0), d["url"])
    )
    return selected


async def find_minutes_docs(base, boza_url=None, boza_html=None):
    """Discover agenda/minutes docs from present back through oldest archive year."""
    listing_urls = await _listing_pages(base, boza_url, boza_html)
    listing_pages = await asyncio.gather(
        *(fetch(u, allow_render=True) for u in listing_urls)
    )

    archive_urls = await _historic_archive_pages(
        base, listing_pages, boza_url, boza_html
    )
    # Bound concurrent archive fetches; full year×category grids can be large.
    archive_pages = []
    for i in range(0, len(archive_urls), 20):
        chunk = archive_urls[i:i + 20]
        archive_pages.extend(
            await asyncio.gather(*(fetch(u, allow_render=True) for u in chunk))
        )

    seen = set()
    docs = []
    # Walk every archive year (present → oldest). Do not early-stop on raw doc
    # count — older years must not be skipped because newer years filled a cap.
    for html in archive_pages:
        docs.extend(_collect_docs_from_html(base, html, seen, assume_bza=True))
    for html in listing_pages:
        docs.extend(_collect_docs_from_html(base, html, seen, assume_bza=False))

    # BOZA page PDFs (Colleton hosts minutes directly on the board page).
    if boza_html and boza_url:
        docs.extend(
            _collect_docs_from_html(boza_url, boza_html, seen, assume_bza=True)
        )

    return _sample_docs_across_years(docs)


async def fetch_document(url):
    """Return extracted text for a document URL, handling PDFs by content sniffing."""
    data = await fetch_bytes(url)
    if not data:
        return None
    if data[:4] == b"%PDF":
        if pdfplumber is None:
            return None
        try:
            parts = []
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages[:8]:
                    parts.append(page.extract_text() or "")
            return "\n".join(parts)
        except Exception:
            return None
    html = data.decode("utf-8", "ignore")
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


# ---------------------------------------------------------------------------
# PHASE 5: extract appointments using an LLM
# ---------------------------------------------------------------------------
# Prompt follows LocalGovPL's design: give the model the known participant list
# (Stage 1 output) so it normalizes names, forbid inference to curb
# overgeneration, and bind output to the text with an explicit end sentinel.
PROMPT_TEMPLATE = (
    "Extract all Board of Zoning Appeals (BZA) members explicitly named in the text below.\n"
    "{known_block}"
    "Return a JSON list of objects with keys: name, status (\"sitting\" or \"historical\"), "
    "term_start (YYYY-MM-DD or YYYY), term_end, gender, tenure (free text).\n"
    "Rules:\n"
    "- Only include people explicitly named in the text. Do NOT infer, guess, or invent members.\n"
    "- When a name matches a known participant, reuse that participant's spelling.\n"
    "- If a field is not mentioned, set it to null.\n"
    "- Return only a valid JSON list, no prose. Stop when you reach END_OF_TEXT.\n"
    "Text:\n{text}\n"
    "END_OF_TEXT"
)

# Documents are trimmed to windows around these cues before hitting the LLM,
# which cuts input tokens (cost/latency) and reduces spurious attributions.
DOC_FOCUS_KEYWORDS = [
    "board of zoning appeals", "zoning appeals", "zoning board", "bza",
    "board of appeals", "unified land management",
    "members present", "staff present",
    "appointed", "reappoint", "term expires", "vacancy",
]
MAX_LLM_CHARS = 12000

# Roles stripped from attendance lines (not part of the person name).
_ATTENDANCE_ROLE_RE = re.compile(
    r",?\s*(?:Vice[-\s]?Chair(?:man)?|Chairman|Chair|Secretary)\s*$",
    re.I,
)
_ATTENDANCE_STOP = re.compile(
    r"(?i)\b(staff\s+present|staff\b|notice:|call to order|called the meeting|"
    r"motion to approve|transcriptionist)\b"
)


def _fix_ocr_name_gaps(text):
    """Repair PDF extractions like 'Pad gett', 'Brad y', 'Chairm an'."""
    # Trailing 1-2 letter fragment: "Brad y" -> "Brady", "Chairm an" -> "Chairman"
    text = re.sub(r"\b([A-Za-z]{4,})\s+([a-z]{1,2})\b", r"\1\2", text)
    # Split surname fragment: "Pad gett" / "Dav ies" -> "Padgett" / "Davies"
    text = re.sub(r"\b([A-Z][a-z]{1,3})\s+([a-z]{2,4})\b", r"\1\2", text)
    return text


def _attendance_header(text):
    """Return the Members Present/Absent header, excluding staff lists."""
    if not text:
        return ""
    # Prefer cutting at Staff Present; else at call-to-order / first motion.
    cut = _ATTENDANCE_STOP.search(text)
    head = text[:cut.start()] if cut else text[:2500]
    return _fix_ocr_name_gaps(head)


def _parse_attendance_names(section):
    """Pull person names out of a Present/Absent section body."""
    names = []
    if not section:
        return names
    # Drop section labels that may remain inline.
    section = re.sub(
        r"(?i)\b(members?\s+present|members?\s+absent|members?|present|absent)\s*:?\s*",
        "\n",
        section,
    )
    role_words = {
        "chairman", "chair", "vice", "secretary", "vicechairman",
        "vicechair", "vice-chair", "vice-chairman",
    }
    for raw_line in section.splitlines():
        line = _fix_ocr_name_gaps(raw_line)
        line = _ATTENDANCE_ROLE_RE.sub("", line).strip(" \t-–—,:;")
        if not line:
            continue
        # A line may list multiple people separated by commas or "and".
        parts = re.split(r"\s*,\s*|\s+and\s+", line)
        for part in parts:
            part = _fix_ocr_name_gaps(part)
            part = _ATTENDANCE_ROLE_RE.sub("", part).strip(" \t-–—,:;")
            part = re.sub(r"\s+", " ", part)
            part = _title_case_name(part)
            if not part or not _is_person(part):
                continue
            low = part.lower()
            if low.replace(" ", "").replace("-", "") in role_words:
                continue
            # Reject leftover institutional phrases.
            if any(
                bad in low
                for bad in (
                    "board of", "zoning", "appeals", "department", "county",
                    "planning", "minutes", "agenda", "staff",
                    "hardship", "variance", "applicant", "owner",
                    "information", "signature", "property",
                )
            ):
                continue
            names.append(part)
    # Unique, preserve order.
    seen, out = set(), []
    for n in names:
        key = _norm_name_key(n)
        if key and key not in seen:
            seen.add(key)
            out.append(n)
    return out


def parse_minutes_attendance(text):
    """Deterministically parse BZA Members Present/Absent from minutes text.

    Spartanburg (and many SC CivicPlus boards) put attendance at the top:
        Members <name>, Chairman
        Present: <names...>
        Members <names...>          # sometimes absent names after a second Members
        Absent: <names...>
        Staff Present: ...
    Returns a list of {name, attendance} dicts (attendance = present|absent|unknown).
    """
    head = _attendance_header(text)
    if not head or not re.search(r"(?i)\bmembers?\b", head):
        return []

    # Normalize label variants onto their own lines for simpler splitting.
    norm = re.sub(r"(?i)\bmembers?\s*present\s*:\s*", "\nMEMBERS_PRESENT:\n", head)
    norm = re.sub(r"(?i)\bpresent\s*:\s*", "\nMEMBERS_PRESENT:\n", norm)
    norm = re.sub(r"(?i)\bmembers?\s*absent\s*:\s*", "\nMEMBERS_ABSENT:\n", norm)
    norm = re.sub(r"(?i)\babsent\s*:\s*", "\nMEMBERS_ABSENT:\n", norm)
    # A bare "Members" line after Present usually introduces Absent names.
    norm = re.sub(r"(?im)^\s*members?\s*$", "MEMBERS_ABSENT:", norm)
    # Leading "Members Name, Chairman" before the first Present label.
    norm = re.sub(r"(?i)\bmembers?\s+", "\nMEMBERS_PRESENT:\n", norm, count=1)

    present, absent = [], []
    current = None
    for line in norm.splitlines():
        label = line.strip()
        if label == "MEMBERS_PRESENT:":
            current = "present"
            continue
        if label == "MEMBERS_ABSENT:":
            current = "absent"
            continue
        if current == "present":
            present.append(line)
        elif current == "absent":
            absent.append(line)

    present_names = _parse_attendance_names("\n".join(present))
    absent_names = _parse_attendance_names("\n".join(absent))
    # If a name is listed under both, Present wins.
    absent_keys = {_norm_name_key(n) for n in absent_names}
    present_keys = {_norm_name_key(n) for n in present_names}
    out = [{"name": n, "attendance": "present"} for n in present_names]
    for n in absent_names:
        if _norm_name_key(n) not in present_keys:
            out.append({"name": n, "attendance": "absent"})
    return out


def attendance_extract(county, documents, roster_keys=None):
    """Build member rows from attendance headers across dated minutes.

    documents: iterable of (text, source_year)
    Members not on the Stage-1 roster are marked historical; appearance years
    become term_start/term_end so historic rows are not empty.
    """
    roster_keys = roster_keys or set()
    # Accumulate min/max year and best name spelling per identity key.
    acc = {}
    for text, year in documents:
        if not text:
            continue
        # Only parse documents that look like actual meeting minutes.
        if not _content_is_minutes(text):
            continue
        year_i = None
        if year is not None:
            try:
                year_i = int(year)
            except (TypeError, ValueError):
                year_i = None
        for row in parse_minutes_attendance(text):
            name = _title_case_name(row["name"])
            if not _is_person(name):
                continue
            key = _norm_name_key(name)
            if not key:
                continue
            slot = acc.get(key)
            if slot is None:
                slot = {
                    "name": name,
                    "years": [],
                    "attendances": set(),
                }
                acc[key] = slot
            else:
                # Prefer the longer / richer spelling.
                if len(name) > len(slot["name"]):
                    slot["name"] = name
            if year_i:
                slot["years"].append(year_i)
            slot["attendances"].add(row.get("attendance") or "unknown")

    extracted = []
    for key, slot in acc.items():
        years = sorted(slot["years"])
        term_start = str(years[0]) if years else None
        term_end = str(years[-1]) if years else None
        on_roster = key in roster_keys
        # Still appearing in the current year's minutes → treat as sitting even
        # if the static roster page omitted them; otherwise historic.
        if on_roster or (years and years[-1] >= CURRENT_YEAR):
            status = "sitting"
        else:
            status = "historical"
        tenure_bits = []
        if years:
            tenure_bits.append(
                f"Minutes attendance {years[0]}-{years[-1]}"
                if years[0] != years[-1]
                else f"Minutes attendance {years[0]}"
            )
        if slot["attendances"]:
            tenure_bits.append(
                "seen as " + "/".join(sorted(slot["attendances"]))
            )
        extracted.append({
            "state": STATE,
            "county": county,
            "name": slot["name"],
            "status": status,
            "term_start": term_start,
            "term_end": term_end,
            "gender": None,
            "tenure": "; ".join(tenure_bits) if tenure_bits else None,
            "_from_roster": False,
            "_from_attendance": True,
        })
    return extracted


def _spread_docs_for_llm(documents, limit):
    """Pick up to `limit` docs spread across years (not only the oldest)."""
    if len(documents) <= limit:
        return list(documents)
    by_year = {}
    for item in documents:
        year = item[1] if item[1] is not None else CURRENT_YEAR
        by_year.setdefault(year, []).append(item)
    years = sorted(by_year)
    selected, idx = [], 0
    # Round-robin across years so historic + recent both reach the LLM.
    while len(selected) < limit and years:
        year = years[idx % len(years)]
        bucket = by_year[year]
        if bucket:
            selected.append(bucket.pop(0))
        if not bucket:
            years = [y for y in years if by_year[y]]
            if not years:
                break
            idx = idx % len(years)
            continue
        idx += 1
    return selected


def _llm_client():
    """Return (client, model). Prefers Gemini (OpenAI-compatible), else OpenAI."""
    if OpenAI is None:
        return None, None
    if GEMINI_API_KEY:
        try:
            return OpenAI(api_key=GEMINI_API_KEY, base_url=GEMINI_BASE_URL), GEMINI_MODEL
        except Exception:
            return None, None
    if OPENAI_API_KEY:
        try:
            return OpenAI(), OPENAI_MODEL
        except Exception:
            return None, None
    return None, None


def _strip_json_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _relevant_excerpt(text, radius=1200, max_chars=MAX_LLM_CHARS):
    """Keep windows around BZA cues, always including the attendance header."""
    low = text.lower()
    spans = [(0, min(len(text), 1800))]  # attendance block lives at the top
    for kw in DOC_FOCUS_KEYWORDS:
        start = 0
        while True:
            idx = low.find(kw, start)
            if idx == -1:
                break
            spans.append((max(0, idx - radius), min(len(text), idx + radius)))
            start = idx + len(kw)
    spans.sort()
    merged = [spans[0]]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return "\n...\n".join(text[s:e] for s, e in merged)[:max_chars]


def _chunk_text(text, size=MAX_LLM_CHARS):
    """Split into <=size chunks instead of truncating, so long docs aren't dropped."""
    if len(text) <= size:
        return [text]
    return [text[i:i + size] for i in range(0, len(text), size)]


def _llm_cache_key(model, prompt):
    return "LLM::" + hashlib.sha256((model + "\x00" + prompt).encode("utf-8")).hexdigest()


def _llm_call(client, model, prompt):
    """Call the LLM, caching raw responses by (model, prompt) so re-runs are free."""
    key = _llm_cache_key(model, prompt)
    if key in cache:
        return cache[key]
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        content = resp.choices[0].message.content
    except Exception:
        return None
    if content is not None:
        cache[key] = content
    return content


def llm_extract(county, documents, known=None):
    """Extract members from document texts.

    Batches up to 3 documents per call, chunks overflow instead of truncating,
    passes the known participant list to normalize names, and caches responses.
    """
    client, model = _llm_client()
    if client is None or not documents:
        return []

    known_block = ""
    if known:
        uniq = ", ".join(sorted({n for n in known if n}))[:1200]
        if uniq:
            known_block = (
                "Known current board participants (reuse these spellings when a name "
                f"matches): {uniq}.\n"
            )

    extracted = []
    for i in range(0, len(documents), 3):  # group up to 3 documents per call
        combined = "\n\n---\n\n".join(documents[i:i + 3])
        for chunk in _chunk_text(combined):
            prompt = PROMPT_TEMPLATE.format(known_block=known_block, text=chunk)
            content = _llm_call(client, model, prompt)
            if not content:
                continue
            try:
                data = json.loads(_strip_json_fences(content))
            except Exception:
                continue
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                name = _title_case_name(str(item.get("name")).strip())
                if not _is_person(name):
                    continue
                extracted.append({
                    "state": STATE,
                    "county": county,
                    "name": name,
                    "status": item.get("status"),
                    "term_start": item.get("term_start"),
                    "term_end": item.get("term_end"),
                    "gender": item.get("gender"),
                    "tenure": item.get("tenure"),
                })
    return extracted


# ---------------------------------------------------------------------------
# PHASE 6: parallel execution (one async task per county)
# ---------------------------------------------------------------------------
semaphore = asyncio.Semaphore(MAX_CONCURRENT)


async def process_county(county):
    async with semaphore:
        members = []
        try:
            # Phase 3
            base, boza_url, boza_html = await find_boza_page(county)
            if boza_html:
                members.extend(parse_current_members(boza_html, county))
                # Rosters often live on a linked "... Members" sub-page.
                for sub_url in _member_subpages(boza_url, boza_html):
                    sub_html = await fetch(sub_url, allow_render=True)
                    if sub_html:
                        members.extend(parse_current_members(sub_html, county))

            # Stage-1 roster becomes the "known participants" list for the LLM.
            roster_names = [m["name"] for m in members]
            roster_keys = {_norm_name_key(n) for n in roster_names if n}

            # Phase 4 + 5: year-by-year historic minutes + attendance parse + LLM.
            if base:
                docs = await find_minutes_docs(base, boza_url, boza_html)
                # Attendance parsing is cheap — use the full year-sampled set so
                # term spans cover the whole archive (not just the oldest chunk).
                attendance_docs = []
                scanned = 0
                for doc in docs:
                    if scanned >= MAX_DOCS_SCAN:
                        break
                    scanned += 1
                    content = await fetch_document(doc["url"])
                    if not content:
                        continue
                    # Skip variance applications / forms that slipped past URL filters.
                    if not _content_is_minutes(content):
                        continue
                    low = content.lower()
                    if not (
                        ("zoning" in low and "appeal" in low)
                        or "bza" in low
                        or "board of zoning appeals" in low
                        or "land management board of appeals" in low
                        or "board of appeals" in low
                    ):
                        continue
                    attendance_docs.append((content, doc.get("year")))

                if attendance_docs:
                    members.extend(
                        attendance_extract(county, attendance_docs, roster_keys)
                    )
                    # LLM only on a year-spread subset (cost/latency bound).
                    llm_docs = _spread_docs_for_llm(attendance_docs, MAX_DOCS_KEEP)
                    excerpts = [_relevant_excerpt(text) for text, _ in llm_docs]
                    extracted = await asyncio.to_thread(
                        llm_extract, county, excerpts, roster_names
                    )
                    for item in extracted:
                        key = _norm_name_key(item.get("name") or "")
                        if key and key not in roster_keys:
                            # Never let the LLM promote a non-roster person to
                            # sitting just because they were "present" in old minutes.
                            item["status"] = "historical"
                        members.append(item)
        except Exception as exc:  # log any errors but continue
            print(f"error [{county}]: {exc}")

        return county, members


# ---------------------------------------------------------------------------
# PHASE 7: deduplication & augmentation
# ---------------------------------------------------------------------------
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _first_name(name):
    for token in name.replace(".", " ").split():
        if len(token) > 1 and token.isalpha():
            return token
    return None


def _surname(name):
    tokens = [t.strip(".") for t in name.split()]
    tokens = [t for t in tokens if t.isalpha() and len(t) > 1 and t.lower() not in _NAME_SUFFIXES]
    return tokens[-1] if tokens else None


def _norm_name_key(name):
    """First+last, lowercased, minus initials/suffixes - merges roster and LLM names."""
    tokens = [t.strip(".").lower() for t in name.split()]
    tokens = [t for t in tokens if t.isalpha() and len(t) > 1 and t not in _NAME_SUFFIXES]
    if len(tokens) >= 2:
        return tokens[0] + " " + tokens[-1]
    return " ".join(tokens)


def _filled_count(member):
    return sum(1 for v in member.values() if v not in (None, "", "null"))


def _first_initial(name):
    for token in name.split():
        token = token.strip(".")
        if token[:1].isalpha():
            return token[0].lower()
    return None


def _same_person(a, b):
    """Relaxed identity match (LocalGovPL §5.3.4): surname + initial, or fuzzy name."""
    ka, kb = _norm_name_key(a["name"]), _norm_name_key(b["name"])
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    sa, sb = _surname(a["name"]), _surname(b["name"])
    if sa and sb and sa.lower() == sb.lower():
        fa, fb = _first_initial(a["name"]), _first_initial(b["name"])
        if not fa or not fb or fa == fb:
            return True
        # "Jason Patrick" vs "Wallace Jason Patrick": shorter first appears in longer.
        tokens_a = {t.strip(".").lower() for t in a["name"].split() if t.isalpha()}
        tokens_b = {t.strip(".").lower() for t in b["name"].split() if t.isalpha()}
        first_a = (_first_name(a["name"]) or "").lower()
        first_b = (_first_name(b["name"]) or "").lower()
        if first_a in tokens_b or first_b in tokens_a:
            return True
    return difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.85


def _year_value(value):
    """Best-effort YYYY int from a term field."""
    if value in (None, "", "null"):
        return None
    m = re.search(r"(19|20)\d{2}", str(value))
    return int(m.group(0)) if m else None


def _merge_members(base, other):
    """Fill null fields from `other`; keep the more complete name; union term years."""
    merged = dict(base)
    for key, value in other.items():
        if key.startswith("_"):
            merged[key] = bool(merged.get(key)) or bool(value)
            continue
        if merged.get(key) in (None, "", "null") and value not in (None, "", "null"):
            merged[key] = value

    def _name_richness(n):
        n = n or ""
        return (len(n.split()), sum(c.isalpha() for c in n))

    if _name_richness(other.get("name")) > _name_richness(base.get("name")):
        merged["name"] = other["name"]

    merged["_from_roster"] = bool(base.get("_from_roster")) or bool(other.get("_from_roster"))
    merged["_from_attendance"] = bool(base.get("_from_attendance")) or bool(
        other.get("_from_attendance")
    )

    # Union attendance/roster years so historic rows keep a real span.
    starts = [_year_value(base.get("term_start")), _year_value(other.get("term_start"))]
    ends = [_year_value(base.get("term_end")), _year_value(other.get("term_end"))]
    starts = [y for y in starts if y]
    ends = [y for y in ends if y]
    if starts:
        merged["term_start"] = str(min(starts))
    if ends:
        merged["term_end"] = str(max(ends))

    # Prefer tenure text that mentions minutes attendance when merging.
    b_ten, o_ten = base.get("tenure") or "", other.get("tenure") or ""
    if "minutes attendance" in o_ten.lower() and "minutes attendance" not in b_ten.lower():
        merged["tenure"] = o_ten if not b_ten else f"{b_ten} | {o_ten}"
    elif "minutes attendance" in b_ten.lower() and o_ten and o_ten not in b_ten:
        merged["tenure"] = f"{b_ten} | {o_ten}"

    # Status from evidence, not LLM vibes:
    # roster row or still current (term_end >= this year) => sitting; else historical.
    end = _year_value(merged.get("term_end"))
    if merged.get("_from_roster") or (end is not None and end >= CURRENT_YEAR):
        merged["status"] = "sitting"
    elif end is not None and end < CURRENT_YEAR:
        merged["status"] = "historical"
    else:
        statuses = {
            (base.get("status") or "").lower(),
            (other.get("status") or "").lower(),
        }
        if "historical" in statuses and not merged.get("_from_roster"):
            merged["status"] = "historical"
        elif "sitting" in statuses:
            merged["status"] = "sitting"

    return merged


def _guess_gender(detector, name):
    """Map gender-guesser labels to plain male/female/unknown.

    The library returns `andy` for androgynous names (e.g. Jackie) and
    `mostly_male` / `mostly_female` for weak signals — none of those belong
    in the CSV as-is. Clear male/female wins; a later given name's weak lean
    (e.g. Ray in Jackie Ray) is used only as a fallback.
    """
    tokens = []
    for token in (name or "").replace(".", " ").split():
        token = token.strip(",'")
        if token.isalpha() and len(token) > 1 and token.lower() not in _NAME_SUFFIXES:
            tokens.append(token)
    # Try each given name before the surname.
    given = tokens[:-1] if len(tokens) >= 2 else tokens
    weak = None
    for i, token in enumerate(given):
        raw = detector.get_gender(token)
        if raw in ("male", "female"):
            return raw
        # First-token weak/andy signals stay unresolved (Kyle → mostly_female
        # is a known library miss). Later tokens like a nickname may lean.
        if i > 0 and raw == "mostly_male" and weak is None:
            weak = "male"
        elif i > 0 and raw == "mostly_female" and weak is None:
            weak = "female"
    return weak or "unknown"


def augment_and_dedupe(all_members):
    detector = gender_guesser.Detector(case_sensitive=False)

    # Relaxed, per-county merge so name variants (roster vs minutes) collapse and
    # complementary fields are combined rather than discarded.
    buckets = {}
    for member in all_members:
        if not member.get("name"):
            continue
        bucket = buckets.setdefault(member.get("county"), [])
        for i, existing in enumerate(bucket):
            if _same_person(existing, member):
                bucket[i] = _merge_members(existing, member)
                break
        else:
            bucket.append(member)

    final = []
    for bucket in buckets.values():
        for member in bucket:
            name = member.get("name") or ""
            if name:
                raw_gender = (member.get("gender") or "").strip().lower()
                # Always normalize library/LLM codes; never leave `andy` in the CSV.
                if raw_gender in ("", "null", "andy", "mostly_male", "mostly_female", "unknown"):
                    member["gender"] = _guess_gender(detector, name)
                else:
                    member["gender"] = raw_gender
            # Final status normalization from evidence (roster / term years).
            end = _year_value(member.get("term_end"))
            if member.get("_from_roster") or (end is not None and end >= CURRENT_YEAR):
                member["status"] = "sitting"
            elif end is not None and end < CURRENT_YEAR and not member.get("_from_roster"):
                member["status"] = "historical"
            # Drop internal bookkeeping flags / retired columns before CSV output.
            member.pop("_from_roster", None)
            member.pop("_from_attendance", None)
            member.pop("place_of_birth", None)
            member.pop("surname_origin", None)
            final.append(member)
    return final


# ---------------------------------------------------------------------------
# PHASE 8: output CSV + summary  /  PHASE 6.3: run selected counties
# ---------------------------------------------------------------------------
def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape SC Board of Zoning Appeals members (current + historic)."
    )
    parser.add_argument(
        "--county",
        action="append",
        dest="counties",
        metavar="NAME",
        help=(
            "Run only this county (repeatable). Use before fanning out statewide "
            "to validate historic archive pulls. Default: all COUNTIES."
        ),
    )
    parser.add_argument(
        "--list-counties",
        action="store_true",
        help="Print the configured county list and exit.",
    )
    return parser.parse_args(argv)


def _resolve_counties(selected):
    if not selected:
        return list(COUNTIES)
    known = {c.lower(): c for c in COUNTIES}
    out = []
    for name in selected:
        key = name.strip().lower()
        if key not in known:
            raise SystemExit(
                f"Unknown county {name!r}. Use --list-counties to see options."
            )
        canon = known[key]
        if canon not in out:
            out.append(canon)
    return out


async def main(counties=None):
    global session
    counties = list(counties) if counties is not None else list(COUNTIES)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT, ssl=False)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; boza-scraper/1.0)"}
    try:
        async with aiohttp.ClientSession(connector=connector, headers=headers) as sess:
            session = sess
            tasks = [process_county(county) for county in counties]
            results = []
            for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="counties"):
                results.append(await coro)
    finally:
        await _close_browser()

    all_members = []
    summary = {}
    status_summary = {}
    for county, members in results:
        summary[county] = len(members)
        status_summary[county] = {
            "sitting": sum(1 for m in members if (m.get("status") or "").lower() == "sitting"),
            "historical": sum(1 for m in members if (m.get("status") or "").lower() == "historical"),
        }
        all_members.extend(members)

    final = augment_and_dedupe(all_members)

    df = pd.DataFrame(final, columns=COLUMNS)
    df.to_csv(OUTPUT_CSV, index=False)

    for county in counties:
        raw = summary.get(county, 0)
        st = status_summary.get(county, {})
        print(
            f"{county}: {raw} raw / "
            f"{sum(1 for m in final if m.get('county') == county)} unique "
            f"(sitting={st.get('sitting', 0)}, historical={st.get('historical', 0)})"
        )
    n_hist = sum(1 for m in final if (m.get("status") or "").lower() == "historical")
    n_sit = sum(1 for m in final if (m.get("status") or "").lower() == "sitting")
    print(
        f"TOTAL: {len(final)} unique members "
        f"(sitting={n_sit}, historical={n_hist}) written to {OUTPUT_CSV}"
    )


if __name__ == "__main__":
    args = _parse_args()
    if args.list_counties:
        print("\n".join(COUNTIES))
        raise SystemExit(0)
    asyncio.run(main(_resolve_counties(args.counties)))
