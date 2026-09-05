"""Shared cleanup helpers for BZA member rows (scraper + CSV repair)."""
from __future__ import annotations

import re

VACANT_NAME = "(Vacant)"

_PHONE_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]\d{4}"
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\b", re.I)
_STREET_RE = re.compile(
    r"\b(?:P\.?\s*O\.?\s*Box\s+\d+|\d{1,6}\s+[A-Z0-9][A-Za-z0-9.'\- ]{1,40}\s+"
    r"(?:St|Street|Rd|Road|Ave|Avenue|Dr|Drive|Ln|Lane|Blvd|Boulevard|"
    r"Ct|Court|Cir|Circle|Hwy|Highway|Way|Pl|Place|Ter|Terrace|Trl|Trail|"
    r"Pkwy|Parkway|Ext|Box))\b\.?",
    re.I,
)
_SC_ZIP_RE = re.compile(
    r"\b[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+)*,?\s+SC\s+\d{5}(?:-\d{4})?\b"
)
_LAST_FIRST_RE = re.compile(
    r"^([A-Z][A-Za-z.'\-]+),\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z]\.?)?)\s*$"
)


def sanitize_tenure(text):
    """Strip phones, emails, and street addresses from tenure / roster paste."""
    if not text:
        return text
    cleaned = _EMAIL_RE.sub(" ", text)
    cleaned = _PHONE_RE.sub(" ", cleaned)
    cleaned = _STREET_RE.sub(" ", cleaned)
    cleaned = _SC_ZIP_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\(\s*[HB]\s*\)", " ", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\s*\|\s*", " | ", cleaned)
    cleaned = cleaned.strip(" \t|,;")
    return cleaned[:240] if cleaned else None


def invert_last_first(name):
    """Turn 'Baisch, Gregory' or leftover 'Baisch Gregory' + comma tenure into First Last."""
    if not name:
        return name
    name = name.strip()
    m = _LAST_FIRST_RE.match(name)
    if m:
        return f"{m.group(2)} {m.group(1)}".strip()
    return name


def invert_name_from_tenure(name, tenure):
    """If tenure starts with 'Last, First' and name is still Last First, invert."""
    if not name or not tenure:
        return name
    first_line = tenure.strip().splitlines()[0].strip()
    # 'Baisch, Gregory January 2012...' or 'Baisch, Gregory'
    m = re.match(
        r"^([A-Z][A-Za-z.'\-]+),\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z]\.)?)",
        first_line,
    )
    if not m:
        return name
    last, first = m.group(1), m.group(2)
    inverted = f"{first} {last}"
    compact = re.sub(r"\s+", " ", name).strip()
    if compact.lower() == inverted.lower():
        return inverted
    if compact.lower() == f"{last} {first}".lower():
        return inverted
    if compact.lower().startswith(last.lower()) and first.split()[0].lower() in compact.lower():
        return inverted
    return name


def is_attendance_only_tenure(tenure):
    if not tenure:
        return False
    low = tenure.lower()
    if "minutes attendance" not in low and "members present" not in low and "members absent" not in low:
        return False
    # Official roster cues mean the years may be real appointments.
    if re.search(r"(?i)appointment|term expire|matchboard|district \d", tenure):
        return False
    return True


def vacant_row(state, county, seat_label):
    return {
        "state": state,
        "county": county,
        "name": VACANT_NAME,
        "status": "vacant",
        "term_start": "",
        "term_end": "",
        "gender": "",
        "tenure": seat_label,
    }


def mccase_name(name):
    """Fix McX / MacX casing when the rest of the name is already title-case."""
    if not name:
        return name
    return re.sub(
        r"\bMc([a-z])",
        lambda m: "Mc" + m.group(1).upper(),
        name,
    )


def normalize_jr(name):
    if not name:
        return name
    return re.sub(r"\bJR\.?\b", "Jr.", name)
