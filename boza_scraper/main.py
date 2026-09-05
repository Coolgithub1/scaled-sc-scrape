# main.py
import argparse
import asyncio
import difflib
import hashlib
import io
import json
import os
import re
import threading
import zipfile
from datetime import datetime
from urllib.parse import urlencode, urljoin, urlparse

import aiohttp
import pandas as pd
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from tqdm import tqdm
import gender_guesser.detector as gender_guesser
import warnings
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

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
from county_sources import (
    COUNTY_STAFF_EXCLUDE,
    KNOWN_BOZA_URLS,
    LOCKED_MINUTES_INDEX_URLS,
    LOCKED_ROSTER_URLS,
    MATCHBOARD_ENTITY_IDS,
)
from cleanup import (
    VACANT_NAME,
    invert_last_first,
    invert_name_from_tenure,
    is_attendance_only_tenure,
    sanitize_tenure,
)

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
_OCR_SEM = asyncio.Semaphore(1)  # asyncio gate for OCR jobs
# Thread lock survives wait_for cancellation — asyncio.Semaphore alone can
# release while a cancelled to_thread still runs tesseract, which then piles up.
_OCR_THREAD_LOCK = threading.Lock()
_OCR_MAX_PAGES = 2
_OCR_DPI = 150
_OCR_MAX_BYTES = 4_000_000  # skip giant scanned packets
_OCR_TIMEOUT_SEC = 45

# Broken SGML marked sections (e.g. <![ila3]>) crash html.parser on some
# CivicPlus AgendaCenter pages (Williamsburg). Strip non-CDATA / non-IE ones.
_BAD_MARKED_SECTION_RE = re.compile(
    r"<!\[(?!(?:CDATA|if\b))[^\]]*\]>",
    re.IGNORECASE,
)


def _soup(html):
    """BeautifulSoup wrapper that tolerates broken marked sections."""
    if not html:
        return BeautifulSoup("", "html.parser")
    try:
        return BeautifulSoup(html, "html.parser")
    except Exception:
        cleaned = _BAD_MARKED_SECTION_RE.sub("", html)
        try:
            return BeautifulSoup(cleaned, "html.parser")
        except Exception:
            # Last resort: drop all marked sections, then give up to empty soup.
            cleaned = re.sub(r"<!\[.*?\]>", "", html, flags=re.S)
            try:
                return BeautifulSoup(cleaned, "html.parser")
            except Exception:
                return BeautifulSoup("", "html.parser")

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
    urls = [
        f"https://www.{base}county.sc.gov",
        f"https://www.{base}countysc.gov",
        f"https://www.{base}countygov.com",
        f"https://www.{base}county.org",
        f"https://{base}county.sc.gov",
        f"https://{base}countysc.gov",
        f"https://www.{base}county.com",
        f"https://www.{county.lower()}.sc.gov",
        f"https://www.{base}county.gov",
        f"https://www.{base}sc.gov",
        f"https://{base}sc.gov",
        f"https://www.co.{base}.sc.us",
    ]
    # County-specific root aliases seen in the wild.
    aliases = {
        "Lexington": ["https://lex-co.sc.gov", "https://www.lex-co.sc.gov"],
        "Darlington": ["https://www.darcosc.com", "https://darcosc.com"],
        "Georgetown": ["https://www.gtcountysc.gov"],
        "Florence": ["https://www.florenceco.org"],
        "Oconee": ["https://oconeesc.com", "https://www.oconeesc.com"],
        "Anderson": ["https://www.andersoncountysc.org"],
        "Pickens": ["https://www.co.pickens.sc.us"],
        "Abbeville": ["https://abbevillecountysc.com"],
        "Fairfield": ["https://www.fairfieldsc.com"],
        "Hampton": ["https://www.hamptoncountysc.org", "http://www.hamptoncountysc.org"],
        "Kershaw": ["https://www.kershaw.sc.gov"],
        "Marion": ["https://www.marionsc.org"],
        "Sumter": ["https://www.sumtersc.gov"],
        "Greenwood": ["https://www.greenwoodcounty-sc.gov"],
        "McCormick": ["https://www.mccormickcountysc.org"],
        "Dillon": ["https://dilloncountysc.org"],
        "Lee": ["https://www.leecountysc.org"],
        "Union": ["https://gearupunionsc.com"],
        "Chesterfield": ["https://www.chesterfieldcountysc.com"],
    }
    for u in aliases.get(county, []):
        if u not in urls:
            urls.insert(0, u)
    return urls


# ---------------------------------------------------------------------------
# PHASE 2: async HTTP client (with diskcache-backed persistence)
# ---------------------------------------------------------------------------
def _needs_js_render(html):
    """True when static HTML looks like an empty SPA shell / JS-gated page."""
    if not html:
        return True
    soup = _soup(html)
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
            # Dismiss common cookie / OneTrust / GDPR banners so roster HTML loads.
            for sel in (
                "button#onetrust-accept-btn-handler",
                "button.accept-cookies",
                "button:has-text('Accept All')",
                "button:has-text('Accept all')",
                "button:has-text('I Accept')",
                "button:has-text('Agree')",
                "a:has-text('Accept')",
            ):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=1500)
                        await page.wait_for_timeout(500)
                        break
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
    denied = False
    for attempt in range(3):  # max 3 retries
        try:
            async with session.get(url, timeout=TIMEOUT, allow_redirects=True) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="ignore")
                    if text and re.search(r"(?i)access\s+denied|errors\.edgesuite\.net", text[:800]):
                        denied = True
                        text = None
                        break
                    break
                if resp.status in (401, 403):
                    denied = True
                    break
                if resp.status in RETRYABLE_STATUS:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                break  # definitive non-200 (e.g. 404): do not retry
        except Exception:
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
    if text is None and denied:
        archived = await _fetch_wayback(url)
        if archived:
            text = archived
    if text is not None:
        cache[url] = text
    if allow_render and _needs_js_render(text):
        rendered = await fetch_rendered(url)
        if rendered and (text is None or not _needs_js_render(rendered)):
            return rendered
        if rendered is None and text is None and denied:
            # Playwright also blocked (Akamai) — try Wayback once more.
            archived = await _fetch_wayback(url)
            if archived:
                cache[url] = archived
                return archived
    return text


async def _fetch_wayback(url):
    """Fetch a recent Wayback Machine snapshot when the live host blocks us."""
    if not url or "web.archive.org" in url.lower():
        return None
    ckey = "WAYBACK::" + url
    if ckey in cache:
        return cache[ckey]
    # Prefer a 2025/2026 snapshot if available via the availability API.
    api = (
        "https://archive.org/wayback/available?"
        + urlencode({"url": url, "timestamp": f"{CURRENT_YEAR}0601"})
    )
    snapshot = None
    try:
        async with session.get(api, timeout=TIMEOUT, allow_redirects=True) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                snapshot = (
                    (data.get("archived_snapshots") or {})
                    .get("closest", {})
                    .get("url")
                )
    except Exception:
        snapshot = None
    if not snapshot:
        # Direct calendar path used successfully for Dorchester.
        snapshot = f"https://web.archive.org/web/{CURRENT_YEAR}/{url}"
    try:
        async with session.get(snapshot, timeout=TIMEOUT, allow_redirects=True) as resp:
            if resp.status != 200:
                return None
            text = await resp.text(errors="ignore")
            if text and not re.search(r"(?i)access\s+denied|errors\.edgesuite\.net", text[:800]):
                cache[ckey] = text
                return text
    except Exception:
        return None
    return None


async def fetch_bytes(url):
    """Fetch raw bytes for url (used for PDFs). Cached under a BYTES:: key."""
    ckey = "BYTES::" + url
    if ckey in cache:
        return cache[ckey]
    data = None
    denied = False
    for attempt in range(3):  # max 3 retries
        try:
            async with session.get(url, timeout=TIMEOUT, allow_redirects=True) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    break
                if resp.status in (403, 503):
                    denied = True
                if resp.status in RETRYABLE_STATUS:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                break
        except Exception:
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
    if data is None and denied and "web.archive.org" not in (url or "").lower():
        # Same Akamai pattern as HTML fetch — try a Wayback snapshot of the file.
        archived = await _fetch_wayback_bytes(url)
        if archived:
            data = archived
    if data is not None:
        cache[ckey] = data
    return data


async def _fetch_wayback_bytes(url):
    """Fetch raw bytes from a Wayback snapshot of url (PDFs/docx)."""
    if not url or "web.archive.org" in url.lower():
        return None
    ckey = "WAYBACK_BYTES::" + url
    if ckey in cache:
        return cache[ckey]
    api = (
        "https://archive.org/wayback/available?"
        + urlencode({"url": url, "timestamp": f"{CURRENT_YEAR}0601"})
    )
    snapshot = None
    try:
        async with session.get(api, timeout=TIMEOUT, allow_redirects=True) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                snapshot = (
                    (data.get("archived_snapshots") or {})
                    .get("closest", {})
                    .get("url")
                )
    except Exception:
        snapshot = None
    if not snapshot:
        snapshot = f"https://web.archive.org/web/{CURRENT_YEAR}/{url}"
    # Prefer the iframe-stripped raw capture for binaries.
    if "/web/" in snapshot and "if_/" not in snapshot:
        snapshot = snapshot.replace("/web/", "/web/", 1)
        # Insert if_ after the timestamp segment: /web/YYYYMMDDhhmmss/...
        snapshot = re.sub(
            r"(https?://web\.archive\.org/web/\d+)",
            r"\1if_",
            snapshot,
            count=1,
        )
    try:
        async with session.get(snapshot, timeout=TIMEOUT, allow_redirects=True) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
            if data and data[:15].lower() != b"<!doctype html>" and len(data) > 200:
                cache[ckey] = data
                return data
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# PHASE 3: locate and parse the current BOZA members page
# ---------------------------------------------------------------------------
BOZA_PATHS = [
    "/boards/zoning",
    "/departments/planning-zoning/board-of-zoning-appeals",
    "/departments/zoning-planning/bza.php",
    "/departments/zoning-planning/board-of-zoning-appeals",
    "/government/boards-commissions",
    "/planning/board-of-zoning-appeals",
    "/zoning-board",
    "/departments/zoning-planning/board-of-zoning-appeals",
    "/government/board-of-zoning-appeals",
    "/board-of-zoning-appeals",
    "/boards-and-commissions/board-of-zoning-appeals",
    "/residents/boards-commissions/board-of-zoning-appeals",
]

TERM_RE = re.compile(r"(\d{4})[-\u2013](\d{4})")
KEYWORD_RE = re.compile(r"Term:|Appointed:|Expires:", re.IGNORECASE)
NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)\b")


def _looks_like_boza(html):
    if not html:
        return False
    # PDFs / binary responses are not roster pages.
    if html.lstrip().startswith("%PDF") or "\x00" in html[:200]:
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
            "access denied",
            "permission to access",
            "errors.edgesuite.net",
            "request blocked",
            "just a moment...",  # Cloudflare challenge
        )
    ):
        return False
    title_m = re.search(r"<title[^>]*>([^<]+)", html, re.I)
    title = title_m.group(1).lower() if title_m else ""
    if any(bad in title for bad in ("404", "not found", "error", "redirect", "access denied", "denied")):
        return False
    # Prefer visible text so cookie-CMP / minified JS "bza" tokens do not match.
    soup = _soup(html)
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    visible = (title + " " + soup.get_text(" ", strip=True)).lower()
    return bool(
        re.search(
            r"board of zoning appeals|"
            r"zoning board of appeals|"
            r"land (?:use|management) board of appeals|"
            r"\bbza\b",
            visible,
        )
    )


async def _reachable_bases(county):
    """Probe all candidate roots concurrently; return [(base, root_html), ...]."""
    urls = candidate_urls(county)
    # County homepages are often JS shells — allow Playwright fallback.
    roots = await asyncio.gather(*(fetch(u, allow_render=True) for u in urls))
    return [(u, html) for u, html in zip(urls, roots) if html]


def _homepage_boza_links(base, root_html):
    """Scan homepage nav for links that look like a zoning/appeals board page."""
    soup = _soup(root_html)
    scored = []
    for a in soup.find_all("a", href=True):
        text = (a.get_text(" ", strip=True) or "").lower()
        href = a["href"].lower()
        blob = text + " " + href
        score = ("zoning" in blob) + ("appeal" in blob) + ("board" in blob)
        if re.search(r"\bbza\b", blob):
            score += 2
        if score >= 2:
            scored.append((score, urljoin(base, a["href"])))
    scored.sort(key=lambda item: item[0], reverse=True)
    seen, links = set(), []
    for _, link in scored:
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links[:8]


def _planning_hub_links(base, root_html):
    """Planning/zoning department hubs often link to the BZA one level down."""
    soup = _soup(root_html)
    hubs = []
    for a in soup.find_all("a", href=True):
        blob = ((a.get_text(" ", strip=True) or "") + " " + a["href"]).lower()
        if ("zoning" in blob or "planning" in blob) and any(
            k in blob for k in ("department", "planning", "zoning", "community", "development")
        ):
            full = urljoin(base, a["href"])
            if full.startswith(base.rstrip("/") ) or urlparse(full).netloc.endswith(
                urlparse(base).netloc.split(".", 1)[-1]
            ):
                hubs.append(full)
    # Unique, capped.
    seen, out = set(), []
    for u in hubs:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:6]


async def find_boza_page(county):
    """Return (base_url, boza_url, html). base_url is set whenever a root loads."""
    # Locked counties: official roster page only — never homepage nav.
    for known in LOCKED_ROSTER_URLS.get(county, []):
        html = await fetch(known, allow_render=True)
        if html:
            parsed = urlparse(known)
            base = f"{parsed.scheme}://{parsed.netloc}"
            return base, known, html

    # 0. Known county-specific BZA URLs (highest precision).
    for known in KNOWN_BOZA_URLS.get(county, []):
        html = await fetch(known, allow_render=True)
        if not html:
            continue
        # Accept known pages even when soft filters are strict — they were curated.
        if _looks_like_boza(html) or len(_soup(html).get_text(" ", strip=True)) > 400:
            # Derive a site root for subsequent archive crawls.
            parsed = urlparse(known)
            base = f"{parsed.scheme}://{parsed.netloc}"
            return base, known, html

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

        # 3. One-hop from planning/zoning department hubs (Charleston pattern).
        for hub in _planning_hub_links(base, root_html):
            hub_html = await fetch(hub, allow_render=True)
            if not hub_html:
                continue
            for link in _homepage_boza_links(hub if "://" in hub else base, hub_html):
                html = await fetch(link, allow_render=True)
                if html and _looks_like_boza(html):
                    return base, link, html

        # 4. Site-search fallback.
        search_url = base + "/search?q=Board+of+Zoning+Appeals"
        html = await fetch(search_url, allow_render=True)
        if html:
            soup = _soup(html)
            for a in soup.find_all("a", href=True):
                href = a["href"].lower()
                if ("zoning" in href and "appeal" in href) or href.endswith("bza.php") or "/bza" in href:
                    target = urljoin(base, a["href"])
                    page = await fetch(target, allow_render=True)
                    if page and _looks_like_boza(page):
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
    "administration", "departments", "legislative", "delegation",
    "accommodations", "communications", "welfare",
    "questions", "attorney", "planner", "landscape", "architect",
    "rights", "reserved", "marriage", "license", "directions",
    "legislation", "inmate", "foreclosure", "holiday", "taxes",
    "privacy", "register", "deeds", "vacancies", "website",
    "interim", "deputy", "officer", "ordinance", "hours",
    "appointed", "enabling", "foreclosure", "unclaimed",
    "cards", "links", "notice", "schedule",
    "applicant", "application", "variance", "hardship", "exception", "request",
    "information", "signature", "signed", "email", "phone", "address", "other",
    "instructions", "form", "yes", "site", "subject", "location", "map",
    "councilmember", "councilman", "councilwoman", "subcommittee", "workshop",
    "ceremony", "hearing", "budget", "recreation", "nuisance", "codes",
    "directory", "staff", "position", "term", "fire", "arts", "homes",
    "business", "election", "officers", "powers", "duties",
}


def _is_person(name):
    if not name:
        return False
    # Form fields / PDF boilerplate leave underscores, blanks, punctuation junk.
    if re.search(r"[_=/\\]|_{2,}|\({3,}|\d{3,}", name):
        return False
    if len(name) > 60:
        return False
    # PDF rosters are often ALL CAPS — normalize before token checks.
    if name.isupper() and len(name.split()) >= 2:
        name = _title_case_name(name)
    tokens = [t.strip(".").lower() for t in name.replace("(", " ").replace(")", " ").split()]
    tokens = [t for t in tokens if t]
    if any(t in _NON_PERSON_WORDS for t in tokens):
        return False
    # City + state abbreviation lines ("North Augusta SC", "Edgefield SC").
    if tokens and re.fullmatch(r"[a-z]{2}", tokens[-1]) and tokens[-1] in {
        "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
        "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
        "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
        "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
        "wi", "wy", "dc",
    }:
        return False
    # Meeting/agenda titles mistaken for people.
    if any(
        bad in " ".join(tokens)
        for bad in (
            "meeting", "session", "download", "agenda", "minutes", "workshop",
            "cancellation", "orientation", "subcommittee", "work session",
            "tax advisory", "sales tax", "video for",
            "voted that", "motion was", "consideration of", "joint water",
            "housing study", "would serve", "seconded by",
        )
    ):
        return False
    alpha = [t for t in tokens if t.isalpha() and len(t) >= 2]
    if len(alpha) < 2:
        # Allow apostrophe names: La'Jessica -> lajessica after strip.
        alpha_ap = [
            re.sub(r"['\u2019]", "", t)
            for t in tokens
            if len(re.sub(r"['\u2019]", "", t)) >= 2
            and re.sub(r"['\u2019]", "", t).isalpha()
        ]
        if len(alpha_ap) < 2:
            return False
        alpha = alpha_ap
    # Reject merged multi-person blobs ("Mike Watson Christopher Pullen").
    if len(alpha) >= 4 and not re.search(r"(?i)\b(jr|sr|ii|iii|iv)\b", name):
        # Allow 4-token names with middle initials only.
        if not any(len(t) == 1 for t in tokens):
            return False
    # Require mostly capitalized person-name tokens (reject sentence fragments).
    raw_tokens = [t.strip(".,'") for t in name.replace("(", " ").replace(")", " ").split()]
    named = []
    for t in raw_tokens:
        core = re.sub(r"['\u2019]", "", t)
        if core.isalpha() and len(core) >= 2:
            named.append(t)
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
    raw = re.sub(r"(?i)^(mr|ms|mrs|miss|dr|rev)\.?\s+", "", raw.strip())
    # Strip CivicPlus seat-type labels (Lancaster), even mid-string before dates.
    raw = re.sub(
        r"(?i)\s*development[-\s]?related\s+professional\b",
        " ",
        raw,
    )
    raw = re.sub(
        r"(?i)\s*at[-\s]?large\s+member(?:\s*\([^)]*\))?",
        " ",
        raw,
    )
    raw = re.sub(r"(?i)\s*vice[-\s]?chairman\b", " ", raw)
    raw = re.sub(r"District\s*#?\s*\d+", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"At[-\s]?Large", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"Seat\s*#?\s*\d+", " ", raw, flags=re.IGNORECASE)
    # Strip trailing role labels glued onto names.
    raw = re.sub(
        r"(?i)\s*[,\-]?\s*(?:vice[-\s]?chair(?:man|woman|person)?|chair(?:man|woman|person)?|"
        r"secretary|councilmember|council\s*member)\s*$",
        " ",
        raw,
    )
    # Leftover hyphenated role stubs ("Tom Audette Vice-").
    raw = re.sub(r"(?i)\s*vice-?\s*$", " ", raw)
    # Unwrap quoted nicknames only; keep mid-name apostrophes (La'Jessica).
    raw = re.sub(r'[\"\u201c\u201d]([^\"\u201c\u201d]+)[\"\u201c\u201d]', r"\1", raw)
    raw = re.sub(r"[\u2018\u2019]([A-Za-z]+)[\u2018\u2019]", r"\1", raw)
    # Drop trailing calendar dates before digit-split ("June 30, 2028").
    raw = re.sub(
        r"(?i)\s+(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2},?\s*(?:(?:19|20)\d{2})?.*$",
        " ",
        raw,
    )
    # An address/phone starts with a digit; cut the cell there.
    raw = re.split(r"\d", raw, 1)[0]
    tokens = re.findall(r"[A-Z][a-zA-Z.'\u2019\-]*", raw)
    # Drop role tokens / leftover month words.
    tokens = [
        t for t in tokens
        if t.lower().rstrip(".") not in {
            "vice", "chair", "chairman", "chairwoman", "chairperson",
            "secretary", "councilmember", "member",
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
        }
    ]
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


def _vacant_member(county, seat_label):
    return {
        "state": STATE,
        "county": county,
        "name": VACANT_NAME,
        "status": "vacant",
        "term_start": None,
        "term_end": None,
        "gender": None,
        "tenure": sanitize_tenure(seat_label) or seat_label,
        "_from_roster": True,
    }


def _member(county, name, status, term_start, term_end, tenure):
    return {
        "state": STATE,
        "county": county,
        "name": name,
        "status": status,
        "term_start": term_start,
        "term_end": term_end,
        "gender": None,
        "tenure": sanitize_tenure(tenure),
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

    def find_col(keys, exact=False):
        for i, h in enumerate(header):
            h_norm = h.strip()
            if exact:
                if h_norm in keys:
                    return i
            elif any(k in h_norm for k in keys):
                return i
        return None

    # York-style: First Name | Last Name | Term Start | Term End
    first_i = find_col(["first name", "firstname"], exact=True) or find_col(["first name"])
    last_i = find_col(["last name", "lastname"], exact=True) or find_col(["last name"])
    name_i = find_col([
        "zoning board member", "board member", "member name", "full name",
        "appointee", "commissioner",
    ])
    # Prefer a dedicated name column; avoid matching "first name"/"last name" as "name".
    if name_i is None:
        for i, h in enumerate(header):
            if h.strip() in ("name", "member", "members"):
                name_i = i
                break
    if name_i is None and not (first_i is not None and last_i is not None):
        name_i = find_col(["member"])
    appt_i = find_col([
        "first appointed", "appointed", "appointment", "since", "term start",
        "start", "term served",
    ])
    exp_i = find_col([
        "expires", "expiration", "term end", "term expires", "appointment ends",
    ])
    has_headers = any(v is not None for v in (name_i, appt_i, exp_i, first_i, last_i))
    body = rows[1:] if any(header) else rows

    for tr in body:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue
        rowtext = " ".join(cells)
        if first_i is not None and last_i is not None and first_i < len(cells) and last_i < len(cells):
            name_src = f"{cells[first_i]} {cells[last_i]}".strip()
        else:
            name_src = cells[name_i] if (name_i is not None and name_i < len(cells)) else rowtext
        if re.search(r"(?i)\bvacant\b", name_src or "") or (
            re.search(r"(?i)\bvacant\b", rowtext) and not re.search(r"[A-Z][a-z]{2,}", name_src or "")
        ):
            out.append(_vacant_member(county, rowtext[:200] or "Vacant seat"))
            continue
        name = _clean_name(name_src)
        if not name:
            # Last, First layout (Beaufort former members).
            m = re.match(r"^\s*([A-Z][A-Za-z.'\-]+),\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z]\.?)?)\s*$", name_src or "")
            if m:
                name = _title_case_name(f"{m.group(2)} {m.group(1)}")
                if not _is_person(name):
                    name = None
        if name:
            name = invert_last_first(name)
            name = invert_name_from_tenure(name, name_src)
        if not name:
            continue

        term_start = term_end = None
        if appt_i is not None and appt_i < len(cells):
            ys = _years_from_dates(cells[appt_i])
            if ys:
                term_start = str(min(ys))
                if term_end is None and len(ys) > 1:
                    term_end = str(max(ys))
        if exp_i is not None and exp_i < len(cells):
            ys = _years_from_dates(cells[exp_i])
            if ys:
                term_end = str(max(ys))
        if term_start is None and term_end is None:
            m = TERM_RE.search(rowtext)
            if m:
                term_start, term_end = m.group(1), m.group(2)
        if term_start is None and term_end is None:
            # "January 2012 - March 2016" / "2012-2016"
            ys = _years_from_dates(rowtext)
            if len(ys) >= 2:
                term_start, term_end = str(min(ys)), str(max(ys))
            elif len(ys) == 1:
                term_end = str(ys[0])

        # Accept a row only when it carries a real temporal signal or the table
        # is clearly a roster (name column plus appointment/expiration column).
        roster_table = (
            (name_i is not None or (first_i is not None and last_i is not None))
            and (appt_i is not None or exp_i is not None)
        )
        # Name | District N tables on BZA pages (Calhoun) — accept as sitting.
        district_pair = (
            len(cells) >= 2
            and re.search(r"(?i)^district\s*\d+", cells[1] if len(cells) > 1 else "")
            and name_i is None
        )
        if term_start is None and term_end is None and not roster_table and not district_pair:
            # Lexington-style: Zoning Board Member column with no dates.
            if name_i is not None and "member" in (header[name_i] if name_i < len(header) else ""):
                pass
            else:
                continue

        # For an accepted roster row, backfill a missing expiry from any year.
        if term_end is None:
            row_years = re.findall(r"\b(?:19|20)\d{2}\b", rowtext)
            if row_years:
                term_end = max(row_years)

        out.append(_member(county, name, _status_for(term_end), term_start, term_end, rowtext[:200]))
    return out


def _parse_current_members_block(text, county):
    """Richland-style: Current Members / Name / (Nth Term) / Appointment / Term Expires."""
    out = []
    m = re.search(r"(?is)current members?\b(.*?)(?:\bcontact\b|\bshare\b|\bformer members?\b|\bagendas?\b|$)", text)
    if not m:
        return out
    block = m.group(1)
    # Each member starts on its own line with a person-like name.
    chunks = re.split(
        r"(?m)\n(?=[A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]*){0,3}\s*$)",
        block,
    )
    for chunk in chunks:
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        raw = lines[0]
        if re.search(r"(?i)^\s*vacant\s*$", raw):
            out.append(_vacant_member(county, " ".join(lines)[:200] or "Vacant seat"))
            continue
        name = _clean_name(raw) or (
            _title_case_name(raw) if _is_person(_title_case_name(raw)) else None
        )
        if not name:
            continue
        blob = " ".join(lines)
        if not re.search(r"(?i)appointment|term expire|reappointment|\d(st|nd|rd|th)\s+term", blob):
            continue
        years = _years_from_dates(blob)
        term_start = str(min(years)) if years else None
        term_end = None
        end_m = re.search(r"(?i)term\s*expires?\s*:?\s*.*?\b((?:19|20)\d{2})\b", blob)
        if end_m:
            term_end = end_m.group(1)
        elif years:
            term_end = str(max(years))
        out.append(_member(county, name, _status_for(term_end), term_start, term_end, blob[:200]))
    return out


def _drop_leading_planning_commission(text):
    """On combined pages, keep only the BZA table that follows Planning Commission."""
    if not text or not re.search(r"(?i)planning commission", text):
        return text
    m = re.search(
        r"(?is)(?:board of zoning appeals|zoning board of appeals)\s*"
        r"(?:\n+\s*zoning district)?\s*\n",
        text,
    )
    if m and re.search(r"(?i)planning commission", text[: m.start()]):
        return text[m.start() :]
    return text


def _parse_district_roster_text(text, county):
    """Chester/Calhoun-style: District N \\n Name \\n Appointment Ends: MM-YYYY."""
    out = []
    text = _drop_leading_planning_commission(text)
    # Prefer the Board of Zoning Appeals section that actually contains district
    # rows. Early nav text like "Agendas + Minutes" must not truncate the roster.
    bza = None
    for m in re.finditer(
        r"(?is)(?:board of zoning appeals|zoning board of appeals)\b(.*?)(?="
        r"\bordiances\b|\bmembership criteria\b|\bconstruction board of appeals\b|"
        r"\bpurpose\b|$)",
        text,
    ):
        if re.search(r"(?im)^(?:district\s*\d+|at\s*large)\s*$", m.group(1)):
            bza = m
            break
        if bza is None:
            bza = m
    scope = bza.group(1) if bza else text
    # Strip a leading "Zoning District" column header.
    scope = re.sub(r"(?im)^\s*zoning\s+district\s*$", "", scope)
    pattern = re.compile(
        r"(?im)^(?:district\s*\d+|at\s*large)\s*\n+"
        r"([A-Z][^\n]{2,60}?)\s*\n+"
        r"((?:(?:Re)?Appointment(?:\s*Ends)?|Term\s*Expires?)[^\n]*\n?)+",
    )
    for m in pattern.finditer(scope):
        raw_name = m.group(1).strip()
        if re.search(r"(?i)^\s*vacant\s*$", raw_name):
            out.append(_vacant_member(county, m.group(0)[:200]))
            continue
        name = _clean_name(raw_name) or (
            _title_case_name(re.sub(r"\s*\(.*\)\s*", " ", raw_name).strip())
        )
        if not name or not _is_person(name):
            continue
        meta = m.group(2)
        years = _years_from_dates(meta)
        end_m = re.search(r"(?i)(?:appointment\s*ends|term\s*expires?)\s*:?\s*.*?\b((?:19|20)\d{2})\b", meta)
        term_end = end_m.group(1) if end_m else (str(max(years)) if years else None)
        start_m = re.search(r"(?i)(?<!re)appointment\s*:?\s*.*?\b((?:19|20)\d{2})\b", meta)
        term_start = start_m.group(1) if start_m else (str(min(years)) if years else None)
        out.append(_member(county, name, _status_for(term_end), term_start, term_end, (raw_name + " " + meta)[:200]))
    # Calhoun/Oconee BZA: Name then District N / At-Large with no dates.
    if not out:
        for m in re.finditer(
            r"(?im)^([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3})(?:\s*\([^)]+\))?\s*\n+"
            r"(?:(?:Vice[-\s]?Chair(?:man|woman)?|Chairman|Chairwoman|Chairperson|Chair|"
            r"Councilmember|Council\s*Member)\s*\n+)?"
            r"(?:District\s*\d+|At[-\s]?Large)\s*$",
            scope,
        ):
            raw = re.sub(r"\s*\([^)]*\)\s*", " ", m.group(1)).strip()
            if re.search(r"(?i)zoning|district|board|appeals|council|vice|chair|staff|liaison", raw):
                continue
            # Skip county-council directory widgets embedded on BZA pages (York).
            block = m.group(0)
            if re.search(r"(?i)council\s*member|councilmember|vice[-\s]?chair", block):
                continue
            name = _clean_name(raw) or _title_case_name(raw)
            if name and _is_person(name):
                out.append(_member(county, name, "sitting", None, None, m.group(0)[:200]))
    return out


def _parse_abbeville_board_table(text, county):
    """Abbeville Boards & Commissions: Member / Position / Term rows under BZA."""
    out = []
    m = re.search(
        r"(?is)board of zoning appeals\b(.*?)(?=\bplanning commission\b|"
        r"\btitle iii\b|\baccommodations tax\b|$)",
        text,
    )
    if not m:
        return out
    scope = m.group(1)
    # Name / District N / MM/YYYY-MM/YYYY  (or Vacant / District / 4 Year Term)
    for row in re.finditer(
        r"(?im)^([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3}|Vacant)\s*\n+"
        r"(District\s*\d+|At[-\s]?Large|Non-?Elected|Elected)\s*\n+"
        r"([^\n]+)$",
        scope,
    ):
        raw = row.group(1).strip()
        if re.search(r"(?i)^\s*vacant\s*$", raw):
            out.append(_vacant_member(county, f"{row.group(2)} vacant"))
            continue
        name = _clean_name(raw) or (
            _title_case_name(raw) if _is_person(_title_case_name(raw)) else None
        )
        if not name or not _is_person(name):
            continue
        meta = row.group(3)
        years = _years_from_dates(meta)
        term_start = str(min(years)) if years else None
        term_end = str(max(years)) if years else None
        out.append(
            _member(
                county,
                name,
                _status_for(term_end),
                term_start,
                term_end,
                f"{raw} {row.group(2)} {meta}"[:200],
            )
        )
    return out


def _parse_lancaster_members_block(text, county):
    """Lancaster CivicPlus: Members / Name / seat type / Month DD, YYYY."""
    out = []
    m = re.search(
        r"(?is)\bmembers\b\s*\n(.*?)(?=\ncitizen|\bagendas?\b|\bcontact us\b|\bpay taxes\b|$)",
        text,
    )
    if not m:
        return out
    block = m.group(1)
    # Require at least one seat-type cue so we don't scrape random Members nav.
    if not re.search(r"(?i)development[-\s]?related|at[-\s]?large member", block):
        return out
    for row in re.finditer(
        r"(?im)^([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3})\s*\n+"
        r"([^\n]+)\s*\n+"
        r"((?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},\s*(?:19|20)\d{2})\s*$",
        block,
    ):
        raw = row.group(1).strip()
        name = _clean_name(raw) or (
            _title_case_name(raw) if _is_person(_title_case_name(raw)) else None
        )
        if not name or not _is_person(name):
            continue
        years = _years_from_dates(row.group(3))
        term_end = str(max(years)) if years else None
        out.append(
            _member(
                county,
                name,
                _status_for(term_end),
                None,
                term_end,
                f"{raw}; {row.group(2)}; {row.group(3)}"[:200],
            )
        )
    return out


def _parse_contacts_people_roster(soup, county):
    """Chester-style contacts-people-list: h4 District / h5 Name / Appointment Ends."""
    out = []
    lists = soup.select("ul.contacts-people-list")
    if not lists:
        return out
    page_text = soup.get_text(" ", strip=True)
    if not re.search(r"(?i)board of zoning appeals|zoning board of appeals", page_text):
        return out
    for lst in lists:
        for li in lst.find_all("li", recursive=False):
            blob = li.get_text("\n", strip=True)
            h5 = li.find("h5")
            h4 = li.find("h4")
            raw_name = h5.get_text(" ", strip=True) if h5 else ""
            seat = h4.get_text(" ", strip=True) if h4 else ""
            if not raw_name or re.search(r"(?i)^\s*vacant\s*$", raw_name):
                if seat or re.search(r"(?i)district|at\s*large|vacant", blob):
                    out.append(_vacant_member(county, (seat or blob)[:200] or "Vacant seat"))
                continue
            name = _clean_name(raw_name) or (
                _title_case_name(raw_name) if _is_person(_title_case_name(raw_name)) else None
            )
            if not name or not _is_person(name):
                continue
            # Require appointment/term cues so nav widgets aren't treated as seats.
            if not re.search(r"(?i)appointment|term\s*expires?", blob):
                continue
            years = _years_from_dates(blob)
            end_m = re.search(
                r"(?i)(?:appointment\s*ends|term\s*expires?)\s*:?\s*.*?\b((?:19|20)\d{2})\b",
                blob,
            )
            term_end = end_m.group(1) if end_m else (str(max(years)) if years else None)
            start_m = re.search(
                r"(?i)(?<!re)appointment\s*:?\s*.*?\b((?:19|20)\d{2})\b", blob
            )
            term_start = start_m.group(1) if start_m else None
            out.append(
                _member(county, name, _status_for(term_end), term_start, term_end, blob[:200])
            )
    return out


def _parse_lastname_comma_roster(text, county):
    """Beaufort former: 'Baisch, Gregory' + 'January 2012 - March 2016'."""
    out = []
    for m in re.finditer(
        r"(?m)^([A-Z][A-Za-z.'\-]+),\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z]\.?)?)\s*$"
        r"\n+([^\n]*(?:19|20)\d{2}[^\n]*)",
        text,
    ):
        name = _title_case_name(f"{m.group(2)} {m.group(1)}")
        if not _is_person(name):
            continue
        years = _years_from_dates(m.group(3))
        term_start = str(min(years)) if years else None
        term_end = str(max(years)) if years else None
        status = "historical" if term_end and int(term_end) < CURRENT_YEAR else _status_for(term_end)
        out.append(_member(county, name, status, term_start, term_end, m.group(0)[:200]))
    return out


def _parse_numbered_membership_roster(text, county):
    """Berkeley master-list style: '# 1 MR. RICHARD W. SMITH (Chair) December 31, 2024'."""
    out = []
    # Require an explicit BZA heading — never scrape other numbered boards.
    # Stop at the next board title / "Updated" footer, not at COUNCIL/DISTRICT labels.
    bza = re.search(
        r"(?is)((?:berk(?:e)?ley\s+county\s+)?board of zoning appeals)\b(.*?)(?="
        r"Updated\s+\d|"
        r"Board of Zoning Appeals\s*Page|"
        r"\n(?:[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){1,8}\s+)?"
        r"(?:Commission|Committee|Authority)\b|"
        r"\n[A-Z][A-Z0-9 /,&'\-]{10,80}(?:BOARD|COMMISSION|COMMITTEE)\b)",
        text,
    )
    if not bza:
        return out
    scope = bza.group(0)
    months = (
        "January|February|March|April|May|June|July|August|"
        "September|October|November|December"
    )
    pattern = re.compile(
        rf"(?im)^#\s*\d+\s+"
        rf"(?:(?:MR|MS|MRS|DR|MISS|REV)\.?\s+)?"
        rf"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){{1,3}}?)"
        rf"(?=\s*(?:\(|{months}\b|Appt\b|Re-?appt\b|$))"
        rf"(?:\s*\([^)]*\))?"
        rf"(?:\s+(?:{months})\s+\d{{1,2}},\s*((?:19|20)\d{{2}}))?",
    )
    for m in pattern.finditer(scope):
        raw = m.group(1).strip()
        if re.search(r"(?i)^(replacing|appt|re-?appt|december|january)\b", raw):
            continue
        name = _title_case_name(raw)
        if not _is_person(name):
            continue
        term_end = m.group(2)
        block_start = m.start()
        # Berkeley sometimes prints the expiry on the line *before* "# N".
        if not term_end:
            prev = scope[max(0, block_start - 80) : block_start]
            ym = re.search(rf"(?i)(?:{months})\s+\d{{1,2}},\s*((?:19|20)\d{{2}})", prev)
            if ym:
                term_end = ym.group(1)
        if not term_end:
            tail = scope[m.end() : m.end() + 120]
            ym = re.search(rf"(?i)(?:{months})\s+\d{{1,2}},\s*((?:19|20)\d{{2}})", tail)
            term_end = ym.group(1) if ym else None
        block_end = m.end() + 220
        nxt = re.search(r"(?m)^#\s*\d+\s+", scope[m.end() :])
        if nxt:
            block_end = min(block_end, m.end() + nxt.start())
        block = scope[block_start:block_end]
        years = _years_from_dates(block)
        appt_ys = []
        for am in re.finditer(
            r"(?i)(?:re-?appt|appt)\s+by[^\n]*?(\d{1,2}/\d{1,2}/\d{2,4})",
            block,
        ):
            appt_ys.extend(_years_from_dates(am.group(1)))
        if appt_ys:
            term_start = str(min(appt_ys))
        elif years:
            start_candidates = [y for y in years if term_end is None or y != int(term_end)]
            term_start = str(min(start_candidates or years))
        else:
            term_start = None
        tenure = re.sub(r"\s+", " ", scope[m.start() : m.end() + 80])[:200]
        out.append(_member(county, name, _status_for(term_end), term_start, term_end, tenure))
    return out


def _bza_scoped_text(text):
    """Prefer the Board of Zoning Appeals section on multi-board pages."""
    if not text:
        return text
    text = _drop_leading_planning_commission(text)
    # Prefer an explicit "… Appeals Members" roster block when present
    # (Greenwood lists BZA early in nav copy, then the real roster later).
    members_block = re.search(
        r"(?is)((?:board of zoning appeals|zoning board of appeals|"
        r"zoning appeals board)\s+members?\b.*?)(?="
        r"\n(?:contact us|privacy policy|planning commission members|"
        r"board of architectural|county council)\b|$)",
        text,
    )
    if members_block and len(members_block.group(1)) > 80:
        return members_block.group(1)
    # Richland-style "Current Members" under a BZA page title.
    current = re.search(
        r"(?is)((?:board of zoning appeals|zoning board of appeals).{0,400}?"
        r"current members?\b.*?)(?=\bcontact\b|\bshare\b|\bformer members?\b|$)",
        text,
    )
    if current and len(current.group(1)) > 80:
        return current.group(1)
    # Abbeville multi-board page: BZA block ends at the next named board.
    abbeville = re.search(
        r"(?is)(board of zoning appeals\b.*?)(?=\bplanning commission\b|"
        r"\btitle iii\b|\baccommodations tax\b|\bboard of assessment\b|$)",
        text,
    )
    if abbeville and re.search(r"(?i)\b(?:member|district|vacant)\b", abbeville.group(1)):
        return abbeville.group(1)
    matches = list(re.finditer(
        r"(?is)((?:board of zoning appeals|zoning board of appeals|"
        r"zoning board of adjustment|board of adjustment(?:s)? and appeals|"
        r"zoning appeals board|board of zoning appeal)\b.*?)"
        r"(?=\n(?:[A-Z][^\n]{0,40}\n)?(?:board of|commission|committee|authority|council)\b|"
        r"\n[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4}\s+Board\b|"
        r"\nPlanning Commission\b|$)",
        text,
    ))
    if not matches:
        return text
    # Prefer the longest match that looks like a roster (has person-like lines).
    def _score(m):
        block = m.group(1)
        score = len(block)
        if re.search(r"(?i)\bmembers?\b", block):
            score += 5000
        if re.search(r"(?i)\b(?:chair|appointed|term|district)\b", block):
            score += 1000
        # Penalize huge multi-board dumps.
        if re.search(r"(?i)\bplanning commission\b", block):
            score -= 3000
        return score
    best = max(matches, key=_score)
    if len(best.group(1)) > 80:
        return best.group(1)
    return text


def _parse_bza_members_lines(text, county):
    """Greenwood-style: 'Board of Zoning Appeals Members' then one name per line."""
    out = []
    m = re.search(
        r"(?is)(?:board of zoning appeals|zoning board of appeals)\s+members?\b"
        r"(.*?)(?=\n(?:contact us|privacy policy|planning commission|board of architectural|"
        r"county council|agendas?\s*\+|business\s*\+|on this page|purpose|membership criteria|"
        r"government|departments)\b|$)",
        text,
    )
    if not m:
        return out
    block = m.group(1)
    # Nav/TOC blocks are not rosters.
    if re.search(r"(?i)agendas?\s*\+|business\s*\+|forms?\s+directory|gis mapping", block):
        return out
    skip = re.compile(
        r"(?i)^(appointed by|member|term|term of office|authority|membership|"
        r"responsibilities|meeting schedule|staff liaison|agendas?\s+and\s+minutes|"
        r"greenwood city|board of zoning|vacant|\u200b|$)",
    )
    for raw in block.splitlines():
        line = raw.strip().strip("\u200b").strip()
        if not line or skip.match(line):
            continue
        if re.search(r"(?i)^\d+\s*years|appointed by council|first monday", line):
            continue
        if ":" in line and len(line.split()) <= 6:
            # "Term of Office : 3 years" style metadata
            continue
        if "+" in line or re.search(r"(?i)\b(directory|mapping|careers|resources|bids)\b", line):
            continue
        cleaned = re.sub(r"\s*\([^)]*(?:chair|vice)[^)]*\)\s*", " ", line, flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t-–—,:;")
        name = _clean_name(cleaned) or (
            _title_case_name(cleaned) if _is_person(_title_case_name(cleaned)) else None
        )
        if name and _is_person(name):
            out.append(_member(county, name, "sitting", None, None, line[:200]))
    return out


def _parse_members_colon_block(text, county):
    """Jasper-style: 'Members:' then one 'Name (Role)' / 'Name, Secretary' per line."""
    out = []
    # Prefer a Members: block that sits under a BZA heading.
    m = re.search(
        r"(?is)(?:board of zoning appeals|zoning board of appeals|board of appeals)\b"
        r".{0,1200}?members?\s*:\s*\n(.*?)(?=\n\s*(?:board of|agendas?|minutes?|e-?packet|"
        r"section menu|contact|home\b|government\b)|$)",
        text,
    )
    if not m:
        m = re.search(
            r"(?im)^members?\s*:\s*\n(.*?)(?=\n\s*(?:board of|agendas?|minutes?|e-?packet|"
            r"section menu|contact)|$)",
            text,
        )
    if not m:
        return out
    block = m.group(1)
    for raw in block.splitlines():
        line = raw.strip().strip("\u200b").strip(" \t-–—•*")
        if not line or re.search(r"(?i)^\s*vacant\s*$", line):
            continue
        # Staff secretary lines are not voting board members.
        if re.search(r"(?i),\s*secretary\s*$", line) or re.search(
            r"(?i)^\s*.+\s+secretary\s*$", line
        ):
            if not re.search(r"(?i)\b(chair|vice)\b", line):
                continue
        cleaned = re.sub(r"\s*\([^)]*(?:chair|vice)[^)]*\)\s*", " ", line, flags=re.I)
        cleaned = re.sub(r"(?i)^(mr|ms|mrs|miss|dr|rev)\.?\s+", "", cleaned).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t-–—,:;")
        name = _clean_name(cleaned) or (
            _title_case_name(cleaned) if _is_person(_title_case_name(cleaned)) else None
        )
        if name and _is_person(name):
            out.append(_member(county, name, "sitting", None, None, line[:200]))
    return out


def _parse_card_title_roster(soup, county):
    """Anderson-style Bootstrap cards: h4.card-title name + District N body text."""
    out = []
    cards = soup.select("h4.card-title, .card-title")
    if not cards:
        return out
    # Require BZA context somewhere on the page so we don't scoop council cards.
    page_text = soup.get_text(" ", strip=True)
    if not re.search(r"(?i)board of zoning appeals|zoning board of appeals", page_text):
        return out
    for title in cards:
        raw = title.get_text(" ", strip=True)
        parent = title.find_parent(class_=re.compile(r"card"))
        body = parent.get_text(" ", strip=True) if parent else raw
        if not raw:
            continue
        if re.search(r"(?i)^\s*vacant\s*$", raw):
            out.append(_vacant_member(county, body[:200] or "Vacant seat"))
            continue
        # Skip non-person card titles (section headers).
        if re.search(
            r"(?i)board of zoning|application|packet|hearing|schedule|contact|ordinance",
            raw,
        ):
            continue
        cleaned = re.sub(r"(?i)^(mr|ms|mrs|miss|dr|rev)\.?\s+", "", raw).strip()
        name = _clean_name(cleaned) or (
            _title_case_name(cleaned) if _is_person(_title_case_name(cleaned)) else None
        )
        if not name or not _is_person(name):
            continue
        # Prefer cards that look like board seats (district / chair / vice).
        if body and not re.search(
            r"(?i)district|chair|vice|member|at-?large", body
        ):
            # Still accept if the card title itself is clearly a person and
            # neighboring cards carry district labels (Anderson pattern).
            pass
        out.append(_member(county, name, "sitting", None, None, (body or raw)[:200]))
    return out


def _table_is_non_bza_board(table):
    """True for Planning Commission (or similar) tables on a combined page."""
    first = table.find("tr")
    if not first:
        return False
    head = first.get_text(" ", strip=True)
    if re.search(r"(?i)planning commission", head) and not re.search(
        r"(?i)zoning appeals|board of zoning", head
    ):
        return True
    caption = table.find("caption")
    if caption and re.search(r"(?i)planning commission", caption.get_text(" ", strip=True)):
        return True
    return False


def _table_under_bza_heading(table):
    """True when the nearest prior board-like heading is the ZBA/BZA."""
    for prev in table.find_all_previous(["h1", "h2", "h3", "h4", "h5", "h6", "p", "strong"]):
        title = prev.get_text(" ", strip=True)
        if not title or len(title) > 100:
            continue
        # Ignore metadata lines under each board.
        if re.search(
            r"(?i)^(term of office|authority|membership|responsibilities|"
            r"meeting schedule|staff liaison|member|position|term)\b",
            title,
        ):
            continue
        if re.search(
            r"(?i)board of zoning appeals|zoning board of appeals|zoning appeals board",
            title,
        ):
            return True
        if re.search(
            r"(?i)\b(?:commission|committee|board|authority|task force)\b", title
        ):
            return False
    # No clear heading — allow (single-board pages).
    return True


def parse_locked_roster_tables(html, county):
    """Sitting roster from official tables only — no nav / full-text heuristics."""
    members = []
    if not html:
        return members
    soup = _soup(html)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for table in soup.find_all("table"):
        if _table_is_non_bza_board(table):
            continue
        members.extend(_parse_table(table, county))
    return members


def parse_current_members(html, county):
    members = []
    if not html:
        return members
    # PDF bytes sometimes sneak through as latin1 text from known URL overrides.
    if html.lstrip().startswith("%PDF") or "\x00" in html[:200]:
        return members
    soup = _soup(html)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # 1. Header-mapped roster tables (handles CivicPlus-style member tables).
    # On multi-board pages (Abbeville), only keep tables under a BZA heading.
    multi_board = len(re.findall(
        r"(?i)term of office", soup.get_text(" ", strip=True)
    )) >= 2
    for table in soup.find_all("table"):
        if _table_is_non_bza_board(table):
            continue
        if multi_board and not _table_under_bza_heading(table):
            continue
        # Combined PC + BZA pages (Calhoun): skip tables under Planning Commission.
        if not _table_under_bza_heading(table) and re.search(
            r"(?i)planning commission", soup.get_text(" ", strip=True)[:2000]
        ):
            continue
        members.extend(_parse_table(table, county))

    # 1b. Card-title rosters (Anderson County).
    members.extend(_parse_card_title_roster(soup, county))
    # 1c. Contacts-people lists (Chester County).
    members.extend(_parse_contacts_people_roster(soup, county))

    full_text = soup.get_text("\n", strip=True)
    text = _bza_scoped_text(full_text)

    # 2. Structured text rosters.
    members.extend(_parse_current_members_block(text, county))
    members.extend(_parse_current_members_block(full_text, county))
    members.extend(_parse_district_roster_text(text, county))
    members.extend(_parse_abbeville_board_table(full_text, county))
    members.extend(_parse_lancaster_members_block(full_text, county))
    members.extend(_parse_lastname_comma_roster(text, county))
    members.extend(_parse_numbered_membership_roster(text, county))
    members.extend(_parse_bza_members_lines(full_text, county))
    members.extend(_parse_bza_members_lines(text, county))
    members.extend(_parse_members_colon_block(full_text, county))
    members.extend(_parse_members_colon_block(text, county))

    # 3. Plan heuristic: list items with a name and a Term/Appointed/Expires cue.
    for lst in soup.find_all(["ul", "ol"]):
        for li in lst.find_all("li"):
            text_li = li.get_text(" ", strip=True)
            if not text_li:
                continue
            term = TERM_RE.search(text_li)
            has_keyword = KEYWORD_RE.search(text_li)
            name = _extract_name(text_li)
            if not name or not (term or has_keyword):
                continue
            term_start = term.group(1) if term else None
            term_end = term.group(2) if term else None
            members.append(
                _member(county, name, _status_for(term_end), term_start, term_end, text_li[:200])
            )
    return members


def _member_subpages(boza_url, boza_html):
    """Collect roster/minutes/agenda links from the BOZA page."""
    soup = _soup(boza_html)
    targets, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = (a.get_text(" ", strip=True) or "").lower()
        href = (a["href"] or "").lower()
        blob = text + " " + href
        # Stay on BZA-related pages — never fan out to every county board.
        zoningish = any(k in blob for k in ("zoning", "appeal", "bza", "zba", "zboa"))
        rosterish = any(k in blob for k in ("member", "roster", "former", "minute", "agenda", "attendance"))
        if not rosterish:
            continue
        if not zoningish and not any(k in blob for k in ("former member", "current member", "minute", "agenda")):
            # Allow former/current/minutes links that live under the ZBA folder.
            path = urlparse(urljoin(boza_url, a["href"])).path.lower()
            if not any(k in path for k in ("zoning", "appeal", "bza", "zba", "zboa")):
                continue
        target = urljoin(boza_url, a["href"])
        if target not in seen and target != boza_url and not target.lower().startswith("mailto:"):
            # Skip county-wide MatchBoard directories (all boards).
            if "matchboard.tech" in target.lower() and "boardid=" not in target.lower():
                continue
            seen.add(target)
            targets.append(target)
    # Prefer minutes/agenda PDFs so attendance harvest isn't crowded out by
    # unrelated nav PDFs (org charts, applications).
    def _rank(u):
        low = u.lower()
        score = 0
        if "minute" in low:
            score -= 20
        if "agenda" in low:
            score -= 10
        if any(k in low for k in ("zoning", "appeal", "bza", "zba")):
            score -= 5
        if any(k in low for k in ("application", "orgchart", "org-chart", "dock")):
            score += 20
        return (score, u)

    targets.sort(key=_rank)
    return targets[:40]


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
    path = urlparse(url).path.lower()
    if (
        "viewfile" in low
        or "showpublisheddocument" in low
        or "details.aspx" in low and ".pdf" in low
        or path.endswith(".pdf")
        or ".pdf?" in low
        or path.endswith(".docx")
        or ".docx?" in low
    ):
        return True
    return any(portal in url.lower() for portal in PORTALS)


def _doc_year(url, link_text=""):
    """Best-effort year from a CivicPlus ViewFile path or PDF filename."""
    blob = f"{url} {link_text}"
    # CivicPlus: /ViewFile/Minutes/_12222020-1595  or  /Agenda/_08252026-2038
    m = re.search(r"ViewFile/(?:Agenda|Minutes?)/_(\d{2})(\d{2})(20\d{2})", blob, re.I)
    if m:
        return int(m.group(3))
    # Wayback Machine timestamp: /web/20250607205224/... or /web/20250607205224if_/...
    m = re.search(r"web\.archive\.org/web/((?:19|20)\d{2})\d{8,14}", blob, re.I)
    if m:
        return int(m.group(1))
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
        # Real minutes almost always have an attendance block (not bare "members").
        if not re.search(
            r"(?i)"
            r"members?\s*(?:present|absent)"
            r"|staff\s+present"
            r"|(?:^|\n)\s*present\s*:"
            r"|(?:^|\n)\s*absent\s*:"
            r"|commission(?:ers?)?\s+present"
            r"|were\s+present"
            r"|were\s+absent"
            r"|board members?\s*[–—:-].{0,200}?\bpresent\b"
            r"|board members?\s*:"
            r"|minutes of the meeting"
            r"|meeting minutes"
            r"|members?\s+present"
            r"|summary of .{0,40}meeting",
            content[:3500],
        ):
            return False
        # Content-only path (URL already vetted): accept when attendance exists
        # and application markers are absent.
        if not blob.strip():
            return True
    return "minute" in blob or "agenda" in blob or "viewfile" in blob or "summary" in blob


def _content_is_minutes(content):
    """Content-only minutes gate used after a PDF/HTML body is downloaded."""
    return _is_minutes_document("", "", content=content)


def _title_case_name(name):
    """Normalize 'Les green' -> 'Les Green' without wrecking Mc/O' names badly."""
    suffixes = {"jr", "sr", "ii", "iii", "iv", "v"}
    parts = []
    for token in name.split():
        # Preserve parenthetical nicknames: "(al)" -> "(Al)"
        if token.startswith("(") and token.endswith(")") and len(token) > 2:
            inner = token[1:-1]
            parts.append("(" + (inner[:1].upper() + inner[1:].lower() if inner else inner) + ")")
            continue
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
        elif (
            "board of appeals" in label
            and not any(x in label for x in ("assessment", "building", "stormwater", "tax"))
        ):
            cats.append(cat_id)
        # Aiken publishes BZA under Planning and Development.
        elif "planning" in label and "development" in label and re.search(
            r"(?i)board of appeals", html
        ):
            cats.append(cat_id)
    # Fallback: section headers / changeYear near BZA wording.
    if not cats:
        for m in re.finditer(
            r'(?is)board of zoning appeals.{0,400}?changeYear\(\d+,\s*(\d+)',
            html,
        ):
            cats.append(m.group(1))
    if not cats:
        for m in re.finditer(
            r'(?is)board of appeals.{0,400}?changeYear\(\d+,\s*(\d+)',
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
    soup = _soup(html)
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
    soup = _soup(html)

    def _add(full, link_text):
        if not _is_document_link(full, link_text) or full in seen:
            return
        full = _rewrite_revize_document_url(full)
        blob_early = f"{full} {link_text}".lower()
        if any(
            bad in blob_early
            for bad in (
                "application", "variance-special-exception", "request-form",
                "special-exception-application", "variance_request",
                "variance-request", "petition-form",
            )
        ):
            return
        if not _is_minutes_document(full, link_text):
            if not (
                _is_minutes_link(full, link_text)
                or "viewfile" in full.lower()
                or "details.aspx" in full.lower()
                or (assume_bza and (full.lower().endswith(".pdf") or ".pdf" in full.lower()))
            ):
                return
        blob = f"{full} {link_text}".lower()
        if "planning commission" in blob and "appeal" not in blob:
            return
        if re.search(r"\bacpc\b", blob) and "appeal" not in blob:
            return
        on_bza_path = "zoning" in full.lower() and (
            "appeal" in full.lower() or "board_of_appeals" in full.lower()
        )
        labeled_bza = (
            ("zoning" in blob and "appeal" in blob)
            or re.search(r"\bbza\b", blob) is not None
            or "bzaagendas" in blob
            or "bza-minutes" in blob
            or "board of zoning" in blob
            or "land management board of appeals" in blob
            or (
                "board of appeals" in blob
                and not any(
                    x in blob
                    for x in (
                        "assessment", "building", "stormwater", "tax ",
                        "construction", "board of voter",
                    )
                )
            )
        )
        if not (assume_bza or on_bza_path or labeled_bza):
            return
        if assume_bza and not (on_bza_path or labeled_bza):
            if any(
                x in blob
                for x in (
                    "historical commission", "recreation commission",
                    "transportation", "legislative delegation",
                    "county council", "voter registration",
                )
            ):
                return
        year = _doc_year(full, link_text)
        seen.add(full)
        docs.append({
            "url": full,
            "text": link_text,
            "year": year,
            "is_minutes": _is_minutes_link(full, link_text) or "minutes" in blob,
        })

    for a in soup.find_all("a", href=True):
        full = urljoin(base, a["href"])
        link_text = a.get_text(" ", strip=True) or ""
        if not link_text:
            link_text = a.get("aria-label") or ""
        _add(full, link_text)
    # Charleston BZA minutes live in <select><option value="...pdf">.
    for opt in soup.find_all("option", value=True):
        val = (opt.get("value") or "").strip()
        if not val or val.startswith("#") or val in {"0", "-1"}:
            continue
        _add(urljoin(base, val), opt.get_text(" ", strip=True) or "")
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
        soup = _soup(html)
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


def _rewrite_revize_document_url(url):
    """Map broken county-host document_center PDFs onto the Revize CDN host."""
    if not url:
        return url
    # Pickens: document_center PDFs 404 on the county host.
    m = re.search(
        r"(?i)https?://(?:www\.)?co\.pickens\.sc\.us/.+?/(document_center/.+\.pdf)",
        url,
    )
    if m:
        path = m.group(1).split("?")[0]
        from urllib.parse import quote
        enc = "/".join(quote(p) if p else p for p in path.split("/"))
        return "https://cms5.revize.com/revize/pickenscountysc/" + enc
    m = re.search(
        r"(?i)^(https?://(?:www\.)?co\.pickens\.sc\.us/)(.*?)(document_center/.+\.pdf)",
        url,
    )
    if m:
        path = m.group(3).split("?")[0]
        from urllib.parse import quote
        enc = "/".join(quote(p) if p else p for p in path.split("/"))
        return "https://cms5.revize.com/revize/pickenscountysc/" + enc
    # McCormick: Agenda & Minutes PDFs resolve on the Revize CDN only.
    m = re.search(
        r"(?i)https?://(?:www\.)?mccormickcountysc\.org/+(?:government/+)?"
        r"(Agenda\s*&\s*Minutes/.+\.pdf)",
        url,
    )
    if m:
        path = m.group(1).split("?")[0]
        from urllib.parse import quote
        enc = "/".join(quote(p) if p else p for p in path.split("/"))
        return "https://cms5.revize.com/revize/mccormickcountysc/" + enc
    m = re.search(
        r"(?i)https?://cms5\.revize\.com/revize/mccormickcountysc/"
        r"(?:government/)+(Agenda\s*&\s*Minutes/.+\.pdf)",
        url,
    )
    if m:
        path = m.group(1).split("?")[0]
        from urllib.parse import quote
        enc = "/".join(quote(p) if p else p for p in path.split("/"))
        return "https://cms5.revize.com/revize/mccormickcountysc/" + enc
    return url


def _parse_district_comma_roster(text, county):
    """Pickens BOA agenda: 'SAMUEL GILLESPIE, District 2, Chair'."""
    out = []
    scope = text
    m_block = re.search(
        r"(?is)\bMEMBERS\b(.*?)(?=\bAGENDA\b|\bI\.\s+Welcome|\bCALL TO ORDER\b|$)",
        text,
    )
    if m_block:
        scope = m_block.group(1)
    years = _years_from_dates(text[:1500])
    doc_year = max(years) if years else None
    for m in re.finditer(
        r"(?im)^([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3})\s*,\s*"
        r"(?:District\s*\d+|At[-\s]?Large)\b"
        r"(?:\s*,\s*(?:Chair|Vice[-\s]?Chair|Chairman|Vice[-\s]?Chairman))?",
        scope,
    ):
        raw = m.group(1).strip()
        name = _title_case_name(raw)
        if not name or not _is_person(name):
            continue
        term_end = str(doc_year) if doc_year else None
        out.append(
            _member(county, name, _status_for(term_end), None, term_end, m.group(0)[:200])
        )
    return out


def _docx_to_text(data):
    """Extract plain text from a .docx (OOXML) byte blob."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "ignore")
    except Exception:
        return None
    # Preserve paragraph breaks and tab-separated columns (Edgefield rosters).
    xml = re.sub(r"<w:tab[^/]*/>", "\t", xml)
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    xml = (
        xml.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )
    return xml.strip() or None


def _is_binary_roster_url(url):
    """True for PDFs / ViewFile / Word docs that must go through fetch_document."""
    low = (url or "").lower()
    return (
        low.endswith(".pdf")
        or ".pdf?" in low
        or low.endswith(".docx")
        or ".docx?" in low
        or "/viewfile/" in low
        or "showpublisheddocument" in low
    )


async def fetch_document(url):
    """Return extracted text for a document URL, handling PDFs by content sniffing."""
    url = _rewrite_revize_document_url(url)
    data = await fetch_bytes(url)
    if not data:
        return None
    # Word .docx member lists (Edgefield ZBA Members.docx).
    if data[:2] == b"PK" and (
        url.lower().endswith(".docx")
        or ".docx?" in url.lower()
        or b"word/document.xml" in data[:8192]
        or b"[Content_Types].xml" in data[:4096]
    ):
        # Confirm OOXML before treating every ZIP as a docx.
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if "word/document.xml" in zf.namelist():
                    return _docx_to_text(data)
        except Exception:
            pass
    if data[:4] == b"%PDF":
        text = None
        if pdfplumber is not None:
            try:
                parts = []
                bza_parts = []
                with pdfplumber.open(io.BytesIO(data)) as pdf:
                    # Minutes usually sit in the first pages; county-wide board
                    # master lists bury BZA later — scan further for those.
                    page_cap = min(len(pdf.pages), 50)
                    for i in range(page_cap):
                        page_text = pdf.pages[i].extract_text() or ""
                        if i < 8:
                            parts.append(page_text)
                        if re.search(
                            r"(?i)board of zoning|zoning board of (?:appeals|adjustment)|"
                            r"zoning appeals board|\bbza\b",
                            page_text,
                        ):
                            bza_parts.append(page_text)
                            if i + 1 < page_cap:
                                bza_parts.append(pdf.pages[i + 1].extract_text() or "")
                text = "\n".join(parts)
                if bza_parts:
                    # Prefer BZA pages when present (avoids truncating master lists).
                    text = text + "\n" + "\n".join(bza_parts)
            except Exception:
                text = None
        # Image-only / scanned minutes: OCR a couple pages under a hard timeout.
        if (
            (not text or len(text.strip()) < 80)
            and len(data) <= _OCR_MAX_BYTES
        ):
            try:
                # Hold the asyncio gate for the whole attempt. The thread lock
                # inside _ocr_pdf_bytes prevents pile-ups if wait_for cancels.
                async with _OCR_SEM:
                    ocr_text = await asyncio.wait_for(
                        asyncio.to_thread(_ocr_pdf_bytes, data),
                        timeout=_OCR_TIMEOUT_SEC,
                    )
                if ocr_text and len(ocr_text.strip()) > len((text or "").strip()):
                    text = ocr_text
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass
        return text
    try:
        html = data.decode("utf-8", "ignore")
        return _soup(html).get_text(" ", strip=True)
    except Exception:
        return None


def _ocr_zba_sections_from_image(img, pytesseract):
    """Crop and OCR ZONING BOARD OF APPEALS bands (colored headers often miss full-page OCR)."""
    try:
        from PIL import ImageOps, ImageEnhance
        # Scanned contact sheets use light colored headers; boost contrast first.
        work = ImageOps.grayscale(img)
        work = ImageEnhance.Contrast(work).enhance(2.0)
        data = pytesseract.image_to_data(work, output_type=pytesseract.Output.DICT)
    except Exception:
        try:
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            work = img
        except Exception:
            return ""
    words = []
    for i, raw in enumerate(data.get("text") or []):
        t = (raw or "").strip()
        if not t:
            continue
        words.append(
            {
                "text": t,
                "top": data["top"][i],
                "height": data["height"][i],
            }
        )
    header_ys = []
    for i, w in enumerate(words):
        low = w["text"].lower().strip(".:")
        if low not in {"zoning", "zba", "board", "appeals", "appeal"}:
            continue
        nearby = " ".join(x["text"].lower() for x in words[max(0, i - 3) : i + 5])
        if ("zoning" in nearby and "appeal" in nearby) or low == "zba":
            header_ys.append(w["top"])
    if not header_ys:
        return ""
    # Deduplicate nearby detections of the same header.
    header_ys = sorted(set(header_ys))
    merged = []
    for y in header_ys:
        if not merged or y - merged[-1] > 80:
            merged.append(y)
    w_img, h_img = work.size
    parts = []
    for yi, y0 in enumerate(merged):
        y1 = merged[yi + 1] - 20 if yi + 1 < len(merged) else min(h_img, y0 + int(h_img * 0.45))
        y0 = max(0, y0 - 20)
        if y1 <= y0 + 40:
            continue
        # Skip "Board of Assessment Appeals" bands — require zoning in the crop head.
        crop = work.crop((0, y0, w_img, y1))
        try:
            crop_text = pytesseract.image_to_string(crop, config="--psm 6") or ""
        except Exception:
            continue
        if not re.search(r"(?i)zoning\s+board\s+of\s+appeals|\bzba\b", crop_text[:400]):
            continue
        parts.append(crop_text)
    return "\n".join(parts)


def _ocr_pdf_bytes(data, max_pages=None):
    """OCR a PDF via pdf2image + tesseract. Returns '' on failure."""
    max_pages = max_pages or _OCR_MAX_PAGES
    # Non-blocking try: if another cancelled OCR thread still holds the lock,
    # skip rather than queue more CPU work behind a dead wait_for.
    if not _OCR_THREAD_LOCK.acquire(blocking=False):
        return ""
    try:
        try:
            import pytesseract
            from pdf2image import convert_from_bytes
        except Exception:
            return ""
        try:
            images = convert_from_bytes(
                data, first_page=1, last_page=max_pages, dpi=_OCR_DPI
            )
        except Exception:
            return ""
        parts = []
        zba_bits = []
        for img in images:
            try:
                parts.append(pytesseract.image_to_string(img) or "")
            except Exception:
                continue
            try:
                zba = _ocr_zba_sections_from_image(img, pytesseract)
                if zba and len(zba.strip()) > 40:
                    zba_bits.append(zba)
            except Exception:
                pass
        # Colored ZBA headers on multi-board contact sheets often need higher DPI.
        if not zba_bits and not re.search(
            r"(?i)zoning board of appeals", "\n".join(parts)
        ):
            try:
                hi = convert_from_bytes(
                    data, first_page=1, last_page=min(max_pages, 2), dpi=220
                )
                for img in hi:
                    zba = _ocr_zba_sections_from_image(img, pytesseract)
                    if zba and len(zba.strip()) > 40:
                        zba_bits.append(zba)
            except Exception:
                pass
        if zba_bits:
            parts.extend(zba_bits)
        return "\n".join(parts)
    finally:
        _OCR_THREAD_LOCK.release()


def _parse_contact_sheet_roster(text, county):
    """Parse Name+phone/address lines under a ZONING BOARD OF APPEALS header."""
    out = []
    if not text:
        return out
    m = re.search(
        r"(?is)zoning board of appeals\b(.*?)(?=\n\s*(?:library board|recreation|"
        r"planning commission|board of assessment|special tax|airport commission|"
        r"economic development|water\s*(?:&|and)\s*sewer)|$)",
        text,
    )
    if not m:
        return out
    section = m.group(1)
    for raw in section.splitlines():
        line = raw.strip()
        if not line or re.search(r"(?i)^(ord\.|vacant|members?\b|term\b|phone\b)", line):
            continue
        name = _clean_name(line)
        if name and _is_person(name):
            out.append(_member(county, name, "sitting", None, None, line[:200]))
    return out


async def fetch_matchboard_members(county):
    """Pull sitting ZBA members from MatchBoard when the county is mapped."""
    entity_id = MATCHBOARD_ENTITY_IDS.get(county)
    if not entity_id or session is None:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://app.matchboard.tech",
        "Referer": "https://app.matchboard.tech/",
        "Accept": "application/json",
    }
    list_url = f"https://api.matchboard.tech/app/boards?entityId={entity_id}"
    try:
        async with session.get(list_url, timeout=TIMEOUT, headers=headers) as resp:
            if resp.status != 200:
                return []
            payload = await resp.json(content_type=None)
    except Exception:
        return []
    boards = (payload.get("data") or {}).get("boards") or []
    target_ids = []
    for b in boards:
        if b.get("entity_id") != entity_id:
            continue
        name = b.get("name") or ""
        if re.search(r"(?i)assessment|building code|construction|housing|tax", name):
            continue
        if re.search(
            r"(?i)(?:board of zoning|zoning board|zoning and appeals|land use zoning)",
            name,
        ):
            target_ids.append(b["id"])
    # Also honor curated MatchBoard board URLs.
    for known in KNOWN_BOZA_URLS.get(county, []):
        m = re.search(r"api\.matchboard\.tech/app/boards/(\d+)", known)
        if m:
            bid = int(m.group(1))
            if bid not in target_ids:
                target_ids.append(bid)
    out = []
    for bid in target_ids:
        detail_url = f"https://api.matchboard.tech/app/boards/{bid}"
        try:
            async with session.get(detail_url, timeout=TIMEOUT, headers=headers) as resp:
                if resp.status != 200:
                    continue
                detail = await resp.json(content_type=None)
        except Exception:
            continue
        board = (detail.get("data") or {}).get("board") or {}
        positions = board.get("board_positions") or []
        if isinstance(positions, str):
            try:
                positions = json.loads(positions)
            except Exception:
                positions = []
        for p in positions:
            if not p.get("active", 1):
                continue
            fn = (p.get("first_name") or "").strip()
            ln = (p.get("last_name") or "").strip()
            raw_name = f"{fn} {ln}".strip(" ,")
            if not raw_name:
                continue
            name = _title_case_name(raw_name)
            # Keep generational suffixes from source ("Fleming, Jr.").
            if not _is_person(name.replace(",", "")):
                continue
            term_end = None
            exp = p.get("term_expiration") or ""
            ym = re.search(r"(20\d{2})", str(exp))
            if ym:
                term_end = ym.group(1)
            gender = (p.get("gender") or "").strip().lower() or None
            if gender in {"male", "m"}:
                gender = "male"
            elif gender in {"female", "f"}:
                gender = "female"
            else:
                gender = None
            # Trust MatchBoard's active flag over calendar year — holdover
            # appointees stay listed as active after term_expiration.
            status = "sitting" if p.get("active", 1) else _status_for(term_end)
            row = _member(
                county,
                name,
                status,
                None,
                term_end,
                f"MatchBoard {board.get('name') or 'ZBA'}; term_expiration={exp}"[:200],
            )
            if gender:
                row["gender"] = gender
            out.append(row)
    return out


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
    r"(?:,?\s*|^)(?:Vice[-\s]?Chair(?:man)?|Chairman|Chairperson|Chair|"
    r"Secretary|Commissioner|Member)\s*",
    re.I,
)
_ATTENDANCE_STOP = re.compile(
    r"(?i)\b(staff\s+present|staff\s*:|staff\b|notice:|call to order|called the meeting|"
    r"motion to approve|transcriptionist)\b"
)


def _fix_ocr_name_gaps(text):
    """Repair PDF extractions like 'Pad gett', 'Brad y', 'Chairm an'."""
    def _join(m):
        left, right = m.group(1), m.group(2)
        # Never glue prepositions/articles onto the prior word ("Board of").
        if right.lower() in {
            "of", "to", "in", "on", "or", "an", "as", "is", "at", "by", "be",
        }:
            return m.group(0)
        return left + right

    # Trailing 1-2 letter fragment: "Brad y" -> "Brady", "Chairm an" -> "Chairman"
    text = re.sub(r"\b([A-Za-z]{4,})\s+([a-z]{1,2})\b", _join, text)
    # Split surname fragment: "Pad gett" / "Dav ies" -> "Padgett" / "Davies"
    text = re.sub(r"\b([A-Z][a-z]{1,3})\s+([a-z]{2,4})\b", r"\1\2", text)
    return text


def _attendance_header(text):
    """Return the Members Present/Absent header, excluding staff lists."""
    if not text:
        return ""
    # Prefer the attendance block wherever it sits. Some counties (Aiken) put
    # CALL TO ORDER before the Members Present list, so a start-of-doc cut loses it.
    m = re.search(
        r"(?is)(?:members?\s+present|members?\s+absent|"
        r"(?:^|\n)\s*present\s*:|commission(?:ers?)?\s+present|"
        r"board members?\s*[–—:-]|MEMBERS\s+PRESENT)",
        text,
    )
    if m:
        window = text[m.start() : m.start() + 3000]
        cut = re.search(
            r"(?i)\b(also\s+present|staff\s+members?\s+present|staff\s+present|staff\s*:|"
            r"recognition of visitors|approval of (?:the\s+)?minutes|"
            r"new business|old business|adjournment)\b",
            window,
        )
        head = window[: cut.start()] if cut else window
        return _fix_ocr_name_gaps(head)
    # Prefer cutting at Staff Present; else at call-to-order / first motion.
    cut = _ATTENDANCE_STOP.search(text)
    head = text[: cut.start()] if cut else text[:2500]
    return _fix_ocr_name_gaps(head)


def _parse_attendance_names(section):
    """Pull person names out of a Present/Absent section body."""
    if not section:
        return []
    # Aiken-style: one member per line with optional "– Chairman" role suffix.
    lines = [ln.strip() for ln in section.splitlines() if ln.strip()]
    if len(lines) >= 2 and section.count(",") <= 1:
        names = []
        for ln in lines:
            ln = re.sub(
                r"\s*[–—-]\s*(?:Vice[-\s]?Chairman|Vice[-\s]?Chair|"
                r"Chairman|Chairwoman|Chairperson|Chair)\s*$",
                "",
                ln,
                flags=re.I,
            )
            names.extend(_parse_attendance_names_flat(ln))
        seen, out = set(), []
        for n in names:
            key = _norm_name_key(n)
            if key and key not in seen:
                seen.add(key)
                out.append(n)
        if out:
            return out
    return _parse_attendance_names_flat(section)


def _parse_attendance_names_flat(section):
    """Pull person names from a comma/and-delimited attendance fragment."""
    names = []
    if not section:
        return names
    # Soft-wraps in PDF text break names across lines ("Mickey\\nWalley").
    section = re.sub(r"\s*\n\s*", " ", section)
    # Drop section labels that may remain inline.
    section = re.sub(
        r"(?i)\b(members?\s+present|members?\s+absent|members?|present|absent|"
        r"commission(?:ers?)?\s+present)\s*:?\s*",
        " ",
        section,
    )
    role_words = {
        "chairman", "chairwoman", "chairperson", "chair", "vice", "secretary",
        "vicechairman", "vicechairwoman", "vicechair", "vice-chair",
        "vice-chairman", "vice-chairwoman", "commissioner", "member", "none",
        "mr", "ms", "mrs", "dr", "miss", "rev",
    }
    # Split first, then strip roles — never let role regex eat comma delimiters.
    parts = re.split(r"\s*[,;]\s*|\s+and\s+", section, flags=re.I)
    for part in parts:
        part = _fix_ocr_name_gaps(part)
        part = part.strip()
        part = re.sub(r"(?i)^(and|or)\s+", "", part)
        part = re.sub(
            r"(?i)^(?:(?:Mr|Ms|Mrs|Dr|Miss|Rev)\.?\s+)*"
            r"(?:Vice[-\s]?Chair(?:man|woman)?|Chairman|Chairwoman|Chairperson|Chair|"
            r"Secretary|Commissioner|Member)\s+",
            "",
            part,
        )
        part = re.sub(
            r"(?i)^(?:(?:Mr|Ms|Mrs|Dr|Miss|Rev)\.?\s+)+",
            "",
            part,
        )
        part = re.sub(
            r"(?i)\s*(?:Vice[-\s]?Chair(?:man|woman)?|Chairman|Chairwoman|Chairperson|Chair|"
            r"Secretary|Commissioner|Member)\s*$",
            "",
            part,
        )
        part = part.strip(" \t-–—,:;.")
        part = re.sub(r"\s+", " ", part)
        part = _title_case_name(part)
        if not part or not _is_person(part):
            continue
        low = part.lower()
        if low.replace(" ", "").replace("-", "") in role_words:
            continue
        if any(
            bad in low
            for bad in (
                "board of", "zoning", "appeals", "department", "county",
                "planning", "minutes", "agenda", "staff",
                "hardship", "variance", "applicant", "owner",
                "information", "signature", "property",
                "vacancy", "vacant", "none",
            )
        ):
            continue
        # Reject names that still carry vacancy/placeholder tokens.
        if re.search(r"(?i)\b(vacancy|vacant|none|\d+)\b", part):
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


def _parse_numbered_board_members_attendance(text):
    """Greenville minutes: 'Board Members:\\n1. Godfrey, Laura – Chairwoman'."""
    if not text:
        return []
    m = re.search(
        r"(?is)board members?\s*:?\s*\n((?:\s*\d+\.\s+.+\n?){3,})",
        text[:4000],
    )
    if not m:
        return []
    block = re.split(
        r"(?i)\n\s*(?:staff|new business|old business|call to order|"
        r"approval of|election of officers)",
        m.group(1),
    )[0]
    out = []
    seen = set()
    for line in block.splitlines():
        line = line.strip()
        mm = re.match(r"^\d+\.\s+(.+)$", line)
        if not mm:
            continue
        raw = mm.group(1).strip()
        attendance = "absent" if re.search(r"(?i)\babsent\b", raw) else "present"
        raw = re.sub(
            r"(?i)\s*[–—-]\s*(?:Vice[-\s]?Chair(?:man|woman)?|Chair(?:man|woman|person)?|"
            r"Absent|Arrived\b.*)\s*$",
            "",
            raw,
        )
        raw = raw.strip(" \t-–—,")
        if "," in raw:
            last, first = [p.strip() for p in raw.split(",", 1)]
            first = re.sub(
                r"(?i)\s+\b(?:Member|Chairman|Chairwoman|Chairperson|Chair|Vice)\b.*$",
                "",
                first,
            ).strip()
            name = _title_case_name(f"{first} {last}".strip())
        else:
            name = _title_case_name(raw)
        if not _is_person(name):
            continue
        key = _norm_name_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "attendance": attendance})
    return out


def parse_minutes_attendance(text):
    """Deterministically parse BZA Members Present/Absent from minutes text.

    Spartanburg (and many SC CivicPlus boards) put attendance at the top:
        Members <name>, Chairman
        Present: <names...>
        Members <names...>          # sometimes absent names after a second Members
        Absent: <names...>
        Staff Present: ...
    Chester-style minutes use:
        Present: Chairman Wallace Hayes, ...
        Absent: none.
    Sumter-style prose minutes use:
        Seven board members –Mr. A, Mr. B were present. Mr. C were absent.
    Returns a list of {name, attendance} dicts (attendance = present|absent|unknown).
    """
    numbered = _parse_numbered_board_members_attendance(text)
    if numbered:
        return numbered

    head = _attendance_header(text)
    if not head:
        return []

    # Sumter prose: "... board members – Name, Name were present. Name were absent."
    prose = re.search(
        r"(?is)board members?\s*[–—:-]\s*(.*?)\bwere\s+present\b"
        r"(?:\.|\s)+(.*?)\bwere\s+absent\b",
        head,
    )
    if prose:
        present_names = _parse_attendance_names(prose.group(1))
        absent_names = _parse_attendance_names(prose.group(2))
        present_keys = {_norm_name_key(n) for n in present_names}
        out = [{"name": n, "attendance": "present"} for n in present_names]
        for n in absent_names:
            if _norm_name_key(n) not in present_keys:
                out.append({"name": n, "attendance": "absent"})
        if out:
            return out

    # Beaufort two-column dump: "MEMBERS PRESENT MEMBERS ABSENT" with both
    # columns collapsed onto the same lines by PDF text extraction.
    two_col = re.search(
        r"(?is)MEMBERS\s+PRESENT\s+MEMBERS\s+ABSENT\s*"
        r"(.*?)(?=STAFF\s+PRESENT|ATTORNEY\s+PRESENT|CALL TO ORDER|\Z)",
        head,
    )
    if two_col:
        present_parts, absent_parts = [], []
        for line in two_col.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(
                r"(?i)^((?:Mr|Mrs|Ms|Dr|Miss)\.\s+.+?)"
                r"\s+((?:Mr|Mrs|Ms|Dr|Miss)\.\s+.+|VACANT(?:\s+\w+)?|One)\s*$",
                line,
            )
            if m:
                present_parts.append(m.group(1))
                right = m.group(2)
                if not re.search(r"(?i)^(?:VACANT|One)\b", right):
                    absent_parts.append(right)
            elif not re.search(r"(?i)^(?:VACANT|One)\s*$", line):
                present_parts.append(line)
        present_names, absent_names = [], []
        for p in present_parts:
            p = re.sub(r"(?i)\(\s*via\s+zoom\s*\)", " ", p)
            present_names.extend(_parse_attendance_names(p))
        for p in absent_parts:
            p = re.sub(r"(?i)\(\s*via\s+zoom\s*\)", " ", p)
            absent_names.extend(_parse_attendance_names(p))
        present_keys = {_norm_name_key(n) for n in present_names}
        out = [{"name": n, "attendance": "present"} for n in present_names]
        for n in absent_names:
            if _norm_name_key(n) not in present_keys:
                out.append({"name": n, "attendance": "absent"})
        if out:
            return out

    if not re.search(
        r"(?i)\bmembers?\b|(?:^|\n)\s*present\s*:|commission(?:ers?)?\s+present|"
        r"were\s+present",
        head,
    ):
        return []

    # Normalize label variants onto their own lines for simpler splitting.
    # Colon is optional (Aiken: "Members Present" without ":").
    norm = re.sub(r"(?i)\bmembers?\s*present\s*:?\s*", "\nMEMBERS_PRESENT:\n", head)
    norm = re.sub(r"(?i)\bcommission(?:ers?)?\s+present\s*:?\s*", "\nMEMBERS_PRESENT:\n", norm)
    norm = re.sub(r"(?i)\bpresent\s*:\s*", "\nMEMBERS_PRESENT:\n", norm)
    norm = re.sub(r"(?i)\bmembers?\s*absent\s*:?\s*", "\nMEMBERS_ABSENT:\n", norm)
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
    present_keys = {_norm_name_key(n) for n in present_names}
    out = [{"name": n, "attendance": "present"} for n in present_names]
    for n in absent_names:
        if _norm_name_key(n) not in present_keys:
            out.append({"name": n, "attendance": "absent"})
    return out


def _parse_board_members_list(text, county):
    """Agenda header roster: 'Board Members:\\nMr. A\\nMs. B, Chair'."""
    out = []
    m = re.search(
        r"(?is)\bboard members?\s*:?\s*\n(.*?)(?=\n\s*\d+\.\s|\n\s*call to order|"
        r"\n\s*approval of|\n\s*agenda\b|\Z)",
        text,
    )
    if not m:
        return out
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or re.search(r"(?i)^(staff|also present|members?\s+present)", line):
            continue
        line = re.sub(r"(?i)^(?:(?:Mr|Ms|Mrs|Dr|Miss|Rev)\.?\s+)+", "", line)
        line = re.sub(
            r"(?i)\s*,?\s*(?:Vice[-\s]?Chair(?:man|woman)?|Chairman|Chairwoman|"
            r"Chairperson|Chair|Secretary)\s*$",
            "",
            line,
        )
        name = _clean_name(line) or (
            _title_case_name(line) if _is_person(_title_case_name(line)) else None
        )
        if name and _is_person(name):
            out.append(_member(county, name, "sitting", None, None, raw.strip()[:200]))
    return out


def _parse_term_colon_roster(text, county):
    """Parse 'Name / Term: M/YYYY-M/YYYY' rosters (Edgefield ZBA Members.docx).

    Supports one- or two-column layouts where a name line is followed by a
    Term: line (columns aligned via tabs or 2+ spaces).
    """
    out = []
    if not text or not re.search(
        r"(?i)zoning board of appeals|board of zoning appeals|\bzba\b",
        text[:1500],
    ):
        # Still allow when the filename/title isn't in the body — require Term:.
        if not re.search(r"(?i)\bterm\s*:", text):
            return out
    lines = text.splitlines()
    role_re = re.compile(
        r"(?i)\s*[–—,-]?\s*(?:Vice[-\s]?Chair(?:man|woman)?|Chairman|Chairwoman|"
        r"Chairperson|Chair|Secretary)\s*$"
    )
    term_re = re.compile(
        r"(?i)^term\s*:\s*(\d{1,2})/(\d{4})\s*[-–—]\s*(\d{1,2})/(\d{4})\s*$"
    )

    def _split_cols(line):
        return [c.strip() for c in re.split(r"(?:\t+|\s{2,})", line) if c.strip()]

    for i, raw in enumerate(lines):
        name_cols = _split_cols(raw)
        if not name_cols or all(term_re.match(c) for c in name_cols):
            continue
        if i + 1 >= len(lines):
            continue
        term_cols = _split_cols(lines[i + 1])
        if not term_cols or not all(term_re.match(c) for c in term_cols):
            continue
        # Pair left-to-right; ignore address/phone residue columns.
        for name_raw, term_raw in zip(name_cols, term_cols):
            if term_re.match(name_raw):
                continue
            tm = term_re.match(term_raw)
            if not tm:
                continue
            cleaned = role_re.sub("", name_raw).strip()
            cleaned = re.sub(r"(?i)^(?:(?:Mr|Ms|Mrs|Dr|Miss|Rev)\.?\s+)+", "", cleaned)
            name = _clean_name(cleaned) or (
                _title_case_name(cleaned) if _is_person(_title_case_name(cleaned)) else None
            )
            if not name or not _is_person(name):
                continue
            term_start, term_end = tm.group(2), tm.group(4)
            tenure = f"{name_raw.strip()}; {term_raw.strip()}"[:200]
            out.append(
                _member(
                    county,
                    name,
                    _status_for(term_end),
                    term_start,
                    term_end,
                    tenure,
                )
            )
    return out


def parse_roster_from_text(text, county):
    """Apply text-roster parsers to plain document text (HTML-stripped or PDF)."""
    if not text:
        return []
    # Dedicated "Zoning Board of Appeals Members" + Term: docs (Edgefield .docx)
    # — prefer the term-colon parser and skip noisier address/chair heuristics.
    term_rows = _parse_term_colon_roster(text, county)
    if term_rows and re.search(
        r"(?i)zoning board of appeals members|board of zoning appeals members",
        text[:800],
    ):
        return term_rows
    # Numbered membership lists need the full multi-board PDF (Berkeley), so
    # run that parser before BZA-scoping truncates surrounding boards.
    members = []
    members.extend(_parse_numbered_membership_roster(text, county))
    members.extend(_parse_bza_members_lines(text, county))
    members.extend(_parse_board_members_list(text, county))
    members.extend(_parse_district_comma_roster(text, county))
    members.extend(term_rows)
    members.extend(_parse_contact_sheet_roster(text, county))
    text = _bza_scoped_text(text)
    members.extend(_parse_current_members_block(text, county))
    members.extend(_parse_district_roster_text(text, county))
    members.extend(_parse_lastname_comma_roster(text, county))
    members.extend(_parse_bza_members_lines(text, county))
    members.extend(_parse_board_members_list(text, county))
    members.extend(_parse_district_comma_roster(text, county))
    members.extend(_parse_term_colon_roster(text, county))
    members.extend(_parse_contact_sheet_roster(text, county))
    # Agenda header: "Chairman – Shasai S. Hendrix"
    for m in re.finditer(
        r"(?i)\b(?:chairman|vice[-\s]?chairman|chairperson)\s*[–—:-]\s*"
        r"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3})",
        text[:4000],
    ):
        name = _title_case_name(m.group(1).strip())
        if _is_person(name):
            members.append(_member(county, name, "sitting", None, None, m.group(0)[:200]))
    return members


def attendance_extract(county, documents, roster_keys=None, roster_locked=False):
    """Build member rows from attendance headers across dated minutes.

    documents: iterable of (text, source_year)
    Members not on the Stage-1 roster are marked historical. Appearance years
    stay in tenure only — they are not appointment terms.
    When roster_locked is True, recent attendance never promotes a non-roster
    name to sitting (official board pages are the sitting source of truth).
    """
    roster_keys = roster_keys or set()
    staff_keys = COUNTY_STAFF_EXCLUDE.get(county, set())
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
            if not key or key in staff_keys:
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
        on_roster = key in roster_keys
        # Still appearing in this year or last year's minutes → sitting
        # (rosters lag; Nov 2025 minutes are still the current board in 2026).
        # Absent-from-one-meeting is not a term-out.
        if on_roster:
            status = "sitting"
        elif roster_locked:
            status = "historical"
        elif years and years[-1] >= CURRENT_YEAR - 1:
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
            # Attendance years are not appointment terms.
            "term_start": None,
            "term_end": None,
            "gender": None,
            "tenure": sanitize_tenure("; ".join(tenure_bits) if tenure_bits else None),
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


async def find_locked_minutes_docs(county):
    """Collect minutes from curated indexes only — no homepage crawl."""
    seen_pages = set()
    index_urls = list(LOCKED_MINUTES_INDEX_URLS.get(county, []))
    for known in KNOWN_BOZA_URLS.get(county, []):
        if _is_binary_roster_url(known) or known in index_urls:
            continue
        if re.search(r"(?i)minute|agenda|directorylisting", known):
            index_urls.append(known)
    pages = []
    for url in index_urls:
        if url in seen_pages:
            continue
        seen_pages.add(url)
        html = await fetch(url, allow_render=True)
        if not html:
            continue
        pages.append((url, html))
        soup = _soup(html)
        for a in soup.find_all("a", href=True):
            target = urljoin(url, a["href"])
            if "DirectoryListingGC/Default.aspx" not in target:
                continue
            if "d=BZAAgendas" not in target or target in seen_pages:
                continue
            seen_pages.add(target)
            folder_html = await fetch(target, allow_render=True)
            if folder_html:
                pages.append((target, folder_html))

    seen_docs = set()
    docs = []
    for url, html in pages:
        docs.extend(_collect_docs_from_html(url, html, seen_docs, assume_bza=True))
    for known in KNOWN_BOZA_URLS.get(county, []):
        if _is_binary_roster_url(known) and known not in seen_docs:
            docs.append({
                "url": known,
                "text": known,
                "year": _doc_year(known),
                "is_minutes": True,
            })
            seen_docs.add(known)
    return _sample_docs_across_years(docs)


async def process_locked_county(county):
    """Official roster tables + minutes attendance. No homepage / LLM junk."""
    members = []
    for url in LOCKED_ROSTER_URLS.get(county, []):
        html = await fetch(url, allow_render=True)
        if html:
            members.extend(parse_locked_roster_tables(html, county))
    roster_keys = {
        _norm_name_key(m["name"])
        for m in members
        if m.get("name") and (m.get("status") or "") != "vacant"
    }
    docs = await find_locked_minutes_docs(county)
    attendance_docs = []
    scanned = 0
    seen = set()
    for doc in docs:
        if scanned >= MAX_DOCS_SCAN:
            break
        scanned += 1
        url = doc["url"]
        if url in seen:
            continue
        seen.add(url)
        content = await fetch_document(url)
        if not content or not _content_is_minutes(content):
            continue
        attendance_docs.append((content, doc.get("year")))
    if attendance_docs:
        members.extend(
            attendance_extract(
                county, attendance_docs, roster_keys, roster_locked=True
            )
        )
    return members


async def process_county(county):
    async with semaphore:
        members = []
        try:
            if county in LOCKED_ROSTER_URLS:
                return county, await process_locked_county(county)
            # MatchBoard sitting rosters (Clarendon, Darlington, …).
            members.extend(await fetch_matchboard_members(county))
            # Phase 3
            base, boza_url, boza_html = await find_boza_page(county)
            pages = []
            if boza_html and boza_url:
                pages.append((boza_url, boza_html))
            # Also crawl every curated URL for this county (rosters often split).
            for known in KNOWN_BOZA_URLS.get(county, []):
                if boza_url and known.rstrip("/") == boza_url.rstrip("/"):
                    continue
                if "api.matchboard.tech" in known.lower():
                    continue
                # PDFs / CivicPlus ViewFile binaries must go through fetch_document —
                # aiohttp text decode of binary PDF is truthy junk.
                known_low = known.lower()
                if _is_binary_roster_url(known):
                    content = await fetch_document(known)
                    if content:
                        members.extend(parse_roster_from_text(content, county))
                        # Feed attendance extract via a synthetic doc list later.
                        if _content_is_minutes(content):
                            members.extend(
                                attendance_extract(
                                    county, [(content, _doc_year(known))], set()
                                )
                            )
                    continue
                html = await fetch(known, allow_render=True)
                if html:
                    pages.append((known, html))

            seen_pages = set()
            early_attendance_docs = []
            for page_url, page_html in pages:
                if page_url in seen_pages:
                    continue
                seen_pages.add(page_url)
                if page_html.lstrip().startswith("%PDF"):
                    content = await fetch_document(page_url)
                    if content:
                        members.extend(parse_roster_from_text(content, county))
                        if _content_is_minutes(content):
                            early_attendance_docs.append((content, _doc_year(page_url)))
                    continue
                members.extend(parse_current_members(page_html, county))
                for sub_url in _member_subpages(page_url, page_html):
                    if sub_url in seen_pages:
                        continue
                    seen_pages.add(sub_url)
                    if _is_binary_roster_url(sub_url):
                        content = await fetch_document(sub_url)
                        if content:
                            members.extend(parse_roster_from_text(content, county))
                            if _content_is_minutes(content) and re.search(
                                r"(?i)zoning|appeal|\bbza\b|board of appeals",
                                content[:3000],
                            ):
                                early_attendance_docs.append(
                                    (content, _doc_year(sub_url, ""))
                                )
                        continue
                    sub_html = await fetch(sub_url, allow_render=True)
                    if sub_html:
                        members.extend(parse_current_members(sub_html, county))

            # Stage-1 roster becomes the "known participants" list for the LLM.
            roster_names = [m["name"] for m in members]
            roster_keys = {_norm_name_key(n) for n in roster_names if n}

            # Phase 4 + 5: year-by-year historic minutes + attendance parse + LLM.
            attendance_docs = list(early_attendance_docs)
            if base:
                docs = await find_minutes_docs(base, boza_url, boza_html)
                # Also harvest docs linked from curated minutes/agenda pages.
                for known in KNOWN_BOZA_URLS.get(county, []):
                    known_low = known.lower()
                    if _is_binary_roster_url(known):
                        continue
                    if not re.search(r"(?i)minute|agenda", known):
                        continue
                    known_html = await fetch(known, allow_render=True)
                    if known_html:
                        # Generic AgendaCenter hubs mix many boards — require
                        # per-link BZA labels instead of assume_bza.
                        assume = "agendacenter" not in known_low.rstrip("/")
                        docs.extend(
                            _collect_docs_from_html(
                                known, known_html, set(), assume_bza=assume
                            )
                        )
                # Attendance parsing is cheap — use the full year-sampled set so
                # term spans cover the whole archive (not just the oldest chunk).
                scanned = 0
                seen_att_urls = set()
                for doc in docs:
                    if scanned >= MAX_DOCS_SCAN:
                        break
                    scanned += 1
                    if doc["url"] in seen_att_urls:
                        continue
                    seen_att_urls.add(doc["url"])
                    content = await fetch_document(doc["url"])
                    if not content:
                        continue
                    # Skip variance applications / forms that slipped past URL filters.
                    if not _content_is_minutes(content):
                        # Still try agenda-header chair names as sitting roster cues.
                        if re.search(r"(?i)board of zoning appeals|zoning board of appeals|\bbza\b", content[:2000]):
                            members.extend(parse_roster_from_text(content, county))
                        continue
                    low = content.lower()
                    if not (
                        ("zoning" in low and "appeal" in low)
                        or "bza" in low
                        or "board of zoning appeals" in low
                        or "zoning board of appeals" in low
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

# Common given-name aliases so "Jerry Noe" merges with "Gerald Noe".
_FIRST_NAME_ALIASES = {
    "jerry": "gerald",
    "gerald": "gerald",
    "jim": "james",
    "jimmy": "james",
    "james": "james",
    "bob": "robert",
    "bobby": "robert",
    "robert": "robert",
    "bill": "william",
    "billy": "william",
    "will": "william",
    "william": "william",
    "tom": "thomas",
    "tommy": "thomas",
    "thomas": "thomas",
    "mike": "michael",
    "michael": "michael",
    "steve": "stephen",
    "steven": "stephen",
    "stephen": "stephen",
    "dick": "richard",
    "rick": "richard",
    "richard": "richard",
    "jack": "john",
    "johnny": "john",
    "john": "john",
}


def _canonical_first(name):
    first = (_first_name(name) or "").lower()
    return _FIRST_NAME_ALIASES.get(first, first)


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
    # Vacant seats are per-district, never merge two vacancies together.
    a_vacant = (a.get("status") or "").lower() == "vacant" or (a.get("name") or "") == VACANT_NAME
    b_vacant = (b.get("status") or "").lower() == "vacant" or (b.get("name") or "") == VACANT_NAME
    if a_vacant or b_vacant:
        return a_vacant and b_vacant and (a.get("tenure") or "") == (b.get("tenure") or "")
    ka, kb = _norm_name_key(a["name"]), _norm_name_key(b["name"])
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    sa, sb = _surname(a["name"]), _surname(b["name"])
    if sa and sb and sa.lower() == sb.lower():
        # Nickname-aware first-name match (Jerry ≈ Gerald).
        if _canonical_first(a["name"]) and _canonical_first(a["name"]) == _canonical_first(b["name"]):
            return True
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

    # Appointment years come from roster/official dates only.
    # Attendance-only years stay in tenure, not term_start/term_end.
    base_roster = bool(base.get("_from_roster"))
    other_roster = bool(other.get("_from_roster"))
    base_att = bool(base.get("_from_attendance")) and not base_roster
    other_att = bool(other.get("_from_attendance")) and not other_roster
    starts = []
    ends = []
    if not (base_att and not other_roster):
        starts.append(_year_value(base.get("term_start")))
        ends.append(_year_value(base.get("term_end")))
    if not (other_att and not base_roster):
        starts.append(_year_value(other.get("term_start")))
        ends.append(_year_value(other.get("term_end")))
    starts = [y for y in starts if y]
    ends = [y for y in ends if y]
    if starts:
        merged["term_start"] = str(min(starts))
    elif base_att and other_att:
        merged["term_start"] = None
    if ends:
        merged["term_end"] = str(max(ends))
    elif base_att and other_att:
        merged["term_end"] = None
    merged["tenure"] = sanitize_tenure(merged.get("tenure"))

    # Prefer tenure text that mentions minutes attendance when merging.
    b_ten, o_ten = base.get("tenure") or "", other.get("tenure") or ""
    if "minutes attendance" in o_ten.lower() and "minutes attendance" not in b_ten.lower():
        merged["tenure"] = o_ten if not b_ten else f"{b_ten} | {o_ten}"
    elif "minutes attendance" in b_ten.lower() and o_ten and o_ten not in b_ten:
        merged["tenure"] = f"{b_ten} | {o_ten}"

    # Status from evidence, not LLM vibes:
    # roster row or still current (term_end >= this year, or last year for
    # attendance-sourced rows) => sitting; else historical.
    end = _year_value(merged.get("term_end"))
    from_attendance = merged.get("_from_attendance") or other.get("_from_attendance") or base.get("_from_attendance")
    if merged.get("_from_roster") or (end is not None and end >= CURRENT_YEAR):
        if end is not None and end < CURRENT_YEAR:
            merged["status"] = "historical"
        else:
            merged["status"] = "sitting"
    elif from_attendance and end is not None and end >= CURRENT_YEAR - 1:
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
            if (member.get("status") or "").lower() == "vacant" or name == VACANT_NAME:
                member["name"] = VACANT_NAME
                member["status"] = "vacant"
                member["gender"] = None
                member["tenure"] = sanitize_tenure(member.get("tenure"))
                member.pop("_from_roster", None)
                member.pop("_from_attendance", None)
                member.pop("place_of_birth", None)
                member.pop("surname_origin", None)
                final.append(member)
                continue
            if name:
                raw_gender = (member.get("gender") or "").strip().lower()
                # Always normalize library/LLM codes; never leave `andy` in the CSV.
                if raw_gender in ("", "null", "andy", "mostly_male", "mostly_female", "unknown"):
                    member["gender"] = _guess_gender(detector, name)
                else:
                    member["gender"] = raw_gender
            member["tenure"] = sanitize_tenure(member.get("tenure"))
            if is_attendance_only_tenure(member.get("tenure") or "") and not member.get("_from_roster"):
                member["term_start"] = None
                member["term_end"] = None
            # Final status normalization from evidence (roster / term years).
            end = _year_value(member.get("term_end"))
            # Expired terms win over the roster flag (former-member pages),
            # except last-year attendance still counts as the current board.
            if end is not None and end < CURRENT_YEAR - 1:
                member["status"] = "historical"
            elif end is not None and end == CURRENT_YEAR - 1 and member.get("_from_attendance"):
                member["status"] = "sitting"
            elif end is not None and end < CURRENT_YEAR and not member.get("_from_attendance"):
                member["status"] = "historical"
            elif member.get("_from_roster") or (end is not None and end >= CURRENT_YEAR):
                member["status"] = "sitting"
            elif (member.get("status") or "").lower() == "historical":
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
    # A --county subset must not wipe the statewide CSV.
    if os.path.exists(OUTPUT_CSV) and set(counties) != set(COUNTIES):
        existing = pd.read_csv(OUTPUT_CSV, dtype=str).fillna("")
        keep = existing[~existing["county"].isin(counties)]
        df = pd.concat([keep, df], ignore_index=True)
        df = df.sort_values(["county", "status", "name"], kind="stable")
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
