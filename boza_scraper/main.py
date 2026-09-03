# main.py
import asyncio
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
)
from counties import COUNTIES
from cache import cache

CURRENT_YEAR = datetime.now().year
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# aiohttp session is created in main() and shared by all workers.
session: aiohttp.ClientSession = None

COLUMNS = [
    "state", "county", "name", "status", "term_start", "term_end",
    "place_of_birth", "gender", "surname_origin", "tenure",
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
        f"https://www.{base}county.org",
        f"https://{base}county.sc.gov",
        f"https://www.{base}county.com",
        f"https://www.{county.lower()}.sc.gov",
    ]


# ---------------------------------------------------------------------------
# PHASE 2: async HTTP client (with diskcache-backed persistence)
# ---------------------------------------------------------------------------
async def fetch(url):
    """Fetch text for url. Checks cache first (key = url), then retries up to 3x."""
    if url in cache:
        return cache[url]
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
    low = html.lower()
    return "zoning" in low and "appeal" in low


async def _reachable_bases(county):
    """Probe all candidate roots concurrently; return [(base, root_html), ...]."""
    urls = candidate_urls(county)
    roots = await asyncio.gather(*(fetch(u) for u in urls))
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
        paths_html = await asyncio.gather(*(fetch(base + p) for p in BOZA_PATHS))
        for path, html in zip(BOZA_PATHS, paths_html):
            if html and _looks_like_boza(html):
                return base, base + path, html

        # 2. Homepage navigation links.
        for link in _homepage_boza_links(base, root_html):
            html = await fetch(link)
            if html and _looks_like_boza(html):
                return base, link, html

        # 3. Site-search fallback.
        search_url = base + "/search?q=Board+of+Zoning+Appeals"
        html = await fetch(search_url)
        if html:
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].lower()
                if "zoning" in href and "appeal" in href:
                    target = urljoin(base, a["href"])
                    page = await fetch(target)
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
    "land", "use", "development", "services", "district",
}


def _is_person(name):
    if not name:
        return False
    tokens = [t.strip(".").lower() for t in name.split()]
    if any(t in _NON_PERSON_WORDS for t in tokens):
        return False
    alpha = [t for t in tokens if t.isalpha() and len(t) >= 2]
    return len(tokens) >= 2 and len(alpha) >= 1


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
    # Drop quoted nicknames like "Ray" that otherwise break the name.
    raw = re.sub(r"[\"\u201c\u201d\u2018\u2019'][^\"\u201c\u201d\u2018\u2019']*[\"\u201c\u201d\u2018\u2019']", " ", raw)
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
        "place_of_birth": None,
        "gender": None,
        "surname_origin": None,
        "tenure": tenure,
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
# PHASE 4: find historical meeting minutes
# ---------------------------------------------------------------------------
MINUTES_PATHS = [
    "/agendas",
    "/minutes",
    "/government/agendas-minutes",
    "/council/agendas",
]
PORTALS = ["legistar.com", "granicus.com", "iqm2.com"]
DOC_KEYWORDS = ["appointed", "zoning", "bza", "board of zoning appeals"]
MAX_DOCS_PER_COUNTY = 50


async def find_minutes_docs(base):
    doc_urls = []
    seen = set()
    pages = await asyncio.gather(*(fetch(base + p) for p in MINUTES_PATHS))
    for html in pages:
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            full = urljoin(base, a["href"])
            link_text = (a.get_text(" ", strip=True) or "").lower()
            haystack = full.lower() + " " + link_text
            is_portal = any(portal in full.lower() for portal in PORTALS)
            if is_portal or any(kw in haystack for kw in DOC_KEYWORDS):
                if full not in seen:
                    seen.add(full)
                    doc_urls.append(full)
            if len(doc_urls) >= MAX_DOCS_PER_COUNTY:
                return doc_urls[:MAX_DOCS_PER_COUNTY]
    return doc_urls[:MAX_DOCS_PER_COUNTY]


async def fetch_document(url):
    low = url.lower()
    if low.endswith(".pdf"):
        data = await fetch_bytes(url)
        if not data or pdfplumber is None:
            return None
        try:
            parts = []
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages[:20]:
                    parts.append(page.extract_text() or "")
            return "\n".join(parts)
        except Exception:
            return None
    html = await fetch(url)
    if not html:
        return None
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


# ---------------------------------------------------------------------------
# PHASE 5: extract appointments using an LLM
# ---------------------------------------------------------------------------
PROMPT_TEMPLATE = (
    "Extract all Board of Zoning Appeals members mentioned in the following text. \n"
    "Return a JSON list of objects with keys: name, status (\"sitting\" or \"historical\"), "
    "term_start (YYYY-MM-DD or YYYY), term_end, place_of_birth, gender, surname_origin, tenure (free text).\n"
    "If a field is not mentioned, omit it or set to null.\n"
    "Only return valid JSON, no other text.\n"
    "Text: {text}"
)


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


def llm_extract(county, documents):
    """Extract members from document texts. Batches up to 3 documents per call."""
    client, model = _llm_client()
    if client is None or not documents:
        return []

    extracted = []
    for i in range(0, len(documents), 3):  # group up to 3 documents per call
        batch = documents[i:i + 3]
        combined = "\n\n---\n\n".join(batch)
        combined = combined[:12000]  # keep total tokens well under the limit
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(text=combined)}],
                temperature=0,
            )
            content = _strip_json_fences(resp.choices[0].message.content)
            data = json.loads(content)
        except Exception:
            continue
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            extracted.append({
                "state": STATE,
                "county": county,
                "name": item.get("name"),
                "status": item.get("status"),
                "term_start": item.get("term_start"),
                "term_end": item.get("term_end"),
                "place_of_birth": item.get("place_of_birth"),
                "gender": item.get("gender"),
                "surname_origin": item.get("surname_origin"),
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
                    sub_html = await fetch(sub_url)
                    if sub_html:
                        members.extend(parse_current_members(sub_html, county))

            # Phase 4 + 5
            if base:
                doc_urls = await find_minutes_docs(base)
                documents = []
                for durl in doc_urls:
                    content = await fetch_document(durl)
                    if content and any(kw in content.lower() for kw in DOC_KEYWORDS):
                        documents.append(content)
                members.extend(llm_extract(county, documents))
        except Exception as exc:  # log any errors but continue
            print(f"error [{county}]: {exc}")

        return county, members


# ---------------------------------------------------------------------------
# PHASE 7: deduplication & augmentation
# ---------------------------------------------------------------------------
SURNAME_ORIGIN = {
    "smith": "English", "johnson": "English", "williams": "Welsh",
    "brown": "English", "jones": "Welsh", "garcia": "Spanish",
    "miller": "German/English", "davis": "Welsh", "rodriguez": "Spanish",
    "martinez": "Spanish", "wilson": "English/Scottish", "anderson": "Scandinavian",
    "taylor": "English", "thomas": "Welsh", "moore": "English/Irish",
    "jackson": "English", "white": "English", "harris": "English",
    "clark": "English", "lewis": "Welsh", "robinson": "English",
    "walker": "English", "young": "English/Scottish", "king": "English",
    "wright": "English", "hill": "English", "green": "English",
    "murphy": "Irish", "kelly": "Irish", "cohen": "Hebrew",
    "nguyen": "Vietnamese", "patel": "Indian", "singh": "Indian/Punjabi",
    "lee": "English/Korean/Chinese", "chen": "Chinese", "wang": "Chinese",
}


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


def _filled_count(member):
    return sum(1 for v in member.values() if v not in (None, "", "null"))


def augment_and_dedupe(all_members):
    detector = gender_guesser.Detector(case_sensitive=False)
    keyed = {}
    for member in all_members:
        if not member.get("name"):
            continue
        key = (member.get("name"), member.get("term_start"))
        current = keyed.get(key)
        if current is None or _filled_count(member) > _filled_count(current):
            keyed[key] = member

    final = []
    for member in keyed.values():
        name = member.get("name") or ""
        if name:
            if not member.get("gender"):
                first = _first_name(name)
                member["gender"] = detector.get_gender(first) if first else "unknown"
            if not member.get("surname_origin"):
                surname = _surname(name)
                if surname:
                    member["surname_origin"] = SURNAME_ORIGIN.get(surname.lower())
        # place_of_birth stays blank unless it was extracted upstream.
        final.append(member)
    return final


# ---------------------------------------------------------------------------
# PHASE 8: output CSV + summary  /  PHASE 6.3: run all counties
# ---------------------------------------------------------------------------
async def main():
    global session
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT, ssl=False)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; boza-scraper/1.0)"}
    async with aiohttp.ClientSession(connector=connector, headers=headers) as sess:
        session = sess
        tasks = [process_county(county) for county in COUNTIES]
        results = []
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="counties"):
            results.append(await coro)

    all_members = []
    summary = {}
    for county, members in results:
        summary[county] = len(members)
        all_members.extend(members)

    final = augment_and_dedupe(all_members)

    df = pd.DataFrame(final, columns=COLUMNS)
    df.to_csv(OUTPUT_CSV, index=False)

    for county in COUNTIES:
        print(f"{county}: {summary.get(county, 0)} members")
    print(f"TOTAL: {len(final)} unique members written to {OUTPUT_CSV}")


if __name__ == "__main__":
    asyncio.run(main())
