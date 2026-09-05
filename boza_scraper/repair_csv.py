#!/usr/bin/env python3
"""Repair known holes in boza_members.csv without inventing people."""
from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

from cleanup import (
    VACANT_NAME,
    invert_name_from_tenure,
    is_attendance_only_tenure,
    mccase_name,
    normalize_jr,
    sanitize_tenure,
    vacant_row,
)
from counties import COUNTIES
from county_sources import COUNTY_COVERAGE_NOTES

try:
    import gender_guesser.detector as gender_guesser
except Exception:  # pragma: no cover
    gender_guesser = None

STATE = "South Carolina"
HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "boza_members.csv"
COVERAGE_PATH = HERE / "county_coverage.csv"

CALHOUN_PLANNING_COMMISSION = {
    "barry hill",
    "ray keitt",
    "josh johnson",
    "josh rabon",
    "tamesha gilmore",
}

NAME_FIXES = {
    ("Orangeburg", "hebert sellers"): "Herbert Sellers",
    ("Orangeburg", "yvonne johnson"): "Yvonne Gooden-Johnson",
    ("Charleston", "ad jordan"): "AD Jordan",
    ("Beaufort", "mark mcginnis"): "Mark McGinnis",
    ("Chester", "mike mcbrayer"): "Mike McBrayer",
    ("Pickens", "harry e. carson jr."): "Harry E. Carson Jr.",
    ("Beaufort", "stanley mack"): "Stanley Mack",
}

# Official published sitting names for counties we corrected from primary sources.
MARION_SITTING = [
    ("Mike Jackson", "Chair; Marion planning department roster"),
    ("David Owens", "Marion planning department roster"),
    ("Earl Watson", "Marion planning department roster"),
    ("John Farmer", "Marion planning department roster"),
    ("Will Causey", "Marion planning department roster"),
    ("Alice Legette", "Marion planning department roster"),
    ("Jerome Williamson", "Marion planning department roster"),
]

SUMTER_SITTING = {
    "william clay smith",
    "clay smith",
    "jason lee reddick",
    "jason reddick",
    "steven s. schumpert",
    "steven schumpert",
    "frank shuler",
    "todd champion",
    "gene weston",
    "tyler doc dunlap",
    "doc dunlap",
    "william t. bailey",
    "william bailey",
    "cassandra floyd",
}

CHARLESTON_TERMS = {
    "ad jordan": ("2023", "2026", "eScribe BoardInformation/28; appointed 23 May 2023"),
}

VACANT_SEATS = [
    ("Abbeville", "District 1 vacant"),
    ("Abbeville", "District 3 vacant"),
    ("Anderson", "District 4 vacant"),
    ("Chester", "District 1 vacant"),
    ("Chester", "At Large vacant"),
    ("Florence", "District 7 vacant"),
    ("Florence", "District 9 vacant"),
    ("Lexington", "Council District 1 vacant"),
    ("Lexington", "Council District 5 vacant"),
    ("Richland", "Unnamed seventh seat vacant"),
    ("McCormick", "Fifth seat unpublished"),
]


def _key(name):
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _guess_gender(detector, name):
    if not detector or not name or name == VACANT_NAME:
        return ""
    tokens = [t.strip(".,'") for t in name.replace(".", " ").split()]
    tokens = [t for t in tokens if t.isalpha() and len(t) > 1 and t.lower() not in {"jr", "sr", "ii", "iii", "iv"}]
    given = tokens[:-1] if len(tokens) >= 2 else tokens
    for token in given:
        raw = detector.get_gender(token)
        if raw in ("male", "female"):
            return raw
    return "unknown"


def load_rows(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_rows(path, rows):
    fields = ["state", "county", "name", "status", "term_start", "term_end", "gender", "tenure"]
    rows = sorted(rows, key=lambda r: (r["county"], r.get("status") or "", r.get("name") or ""))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) or "" for k in fields})


def repair(rows):
    detector = gender_guesser.Detector(case_sensitive=False) if gender_guesser else None
    out = []
    for row in rows:
        county = (row.get("county") or "").strip()
        name = (row.get("name") or "").strip()
        key = _key(name)
        tenure = row.get("tenure") or ""

        if county == "Calhoun" and key in CALHOUN_PLANNING_COMMISSION:
            continue

        name = NAME_FIXES.get((county, key), name)
        name = invert_name_from_tenure(name, tenure)
        name = mccase_name(name)
        name = normalize_jr(name)
        key = _key(name)

        tenure = sanitize_tenure(tenure) or ""
        # Lexington tenure concatenated council member + BZA member.
        if county == "Lexington":
            tenure = re.sub(r"^\d+\s+", "", tenure)
            if name and name in tenure:
                tenure = f"Zoning Board Member; {name}"

        status = (row.get("status") or "").strip().lower()
        start = (row.get("term_start") or "").strip()
        end = (row.get("term_end") or "").strip()

        if is_attendance_only_tenure(tenure):
            start, end = "", ""

        if county == "Chester" and key in {"melvin b. jackson", "melvin jackson"}:
            name = "Melvin B. Jackson"
            key = "melvin b. jackson"
            start, end = "2025", "2028"
            status = "sitting"
            tenure = "District 3; Reappointment: 03-2025; Appointment Ends: 12-2028"

        if county == "Chester" and key == "wallace hayes":
            start, end = "2023", "2026"

        if county == "Pickens" and key == "bob fetterly":
            status = "sitting"
            start, end = "", ""
            if "members absent" in tenure.lower() and "sitting" not in tenure.lower():
                tenure = "Members Absent on May 4 2026 BOA minutes; still a current seat"

        if county == "Orangeburg" and key == "william weathers":
            status = "sitting"
            tenure = "Orangeburg County BZA District 2"

        if county == "Orangeburg" and key == "herbert sellers":
            tenure = "Orangeburg County BZA District 1"

        if county == "Sumter" and key == "claude wheeler":
            status = "historical"
            tenure = "Jan 2026 minutes (absent); not on April 2026 official 9-member list"

        if county == "Charleston" and key in CHARLESTON_TERMS:
            start, end, note = CHARLESTON_TERMS[key]
            tenure = note

        if county == "Colleton" and key.startswith("douglas mixson"):
            # Still in 2025 minutes; keep historical only if we have no 2026 sighting.
            if "2025" in (tenure or "") and "2026" not in (tenure or ""):
                status = "sitting"
                tenure = (tenure + " | still appearing in 2025 minutes").strip(" |")

        gender = (row.get("gender") or "").strip().lower()
        if gender in {"", "null", "andy", "mostly_male", "mostly_female", "unknown"}:
            gender = _guess_gender(detector, name)

        out.append({
            "state": STATE,
            "county": county,
            "name": name,
            "status": status,
            "term_start": start,
            "term_end": end,
            "gender": gender,
            "tenure": tenure,
        })

    # Merge remaining Chester Melvin duplicates after rename.
    merged = []
    seen_melvin = False
    for row in out:
        if row["county"] == "Chester" and _key(row["name"]) == "melvin b. jackson":
            if seen_melvin:
                continue
            seen_melvin = True
            row["status"] = "sitting"
            row["term_start"] = "2025"
            row["term_end"] = "2028"
        merged.append(row)
    out = merged

    existing = {(r["county"], _key(r["name"]), r.get("tenure") or "") for r in out}
    for county, seat in VACANT_SEATS:
        marker = vacant_row(STATE, county, seat)
        key = (county, _key(VACANT_NAME), seat)
        if key not in existing:
            out.append(marker)
            existing.add(key)

    have_marion = any(r["county"] == "Marion" and r["status"] == "sitting" for r in out)
    if not have_marion:
        for name, tenure in MARION_SITTING:
            out.append({
                "state": STATE,
                "county": "Marion",
                "name": name,
                "status": "sitting",
                "term_start": "",
                "term_end": "",
                "gender": _guess_gender(detector, name),
                "tenure": tenure,
            })

    return out


def write_coverage(rows):
    by_county = defaultdict(list)
    for row in rows:
        by_county[row["county"]].append(row)
    fields = [
        "county", "rows", "sitting", "historical", "vacant",
        "coverage", "notes",
    ]
    out = []
    for county in COUNTIES:
        recs = by_county.get(county, [])
        sitting = sum(1 for r in recs if r["status"] == "sitting")
        hist = sum(1 for r in recs if r["status"] == "historical")
        vacant = sum(1 for r in recs if r["status"] == "vacant")
        note = COUNTY_COVERAGE_NOTES.get(county, "")
        if note:
            coverage = note
        elif sitting and hist:
            coverage = "sitting_and_historic"
        elif sitting:
            coverage = "sitting_only"
        else:
            coverage = "empty"
        extras = []
        if vacant:
            extras.append(f"{vacant} vacant seat(s) recorded")
        if county == "Calhoun":
            extras.append("Planning Commission rows removed")
        if county == "Edgefield":
            extras.append("official board size is 3")
        if county == "Jasper":
            extras.append("published roster is 4 sitting")
        if county == "Sumter":
            extras.append("official board size is 9")
        out.append({
            "county": county,
            "rows": len(recs),
            "sitting": sitting,
            "historical": hist,
            "vacant": vacant,
            "coverage": coverage,
            "notes": "; ".join(extras),
        })
    with COVERAGE_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(out)
    return out


def main(argv=None):
    argv = argv or sys.argv[1:]
    src = Path(argv[0]) if argv else CSV_PATH
    rows = repair(load_rows(src))
    write_rows(src, rows)
    coverage = write_coverage(rows)
    sitting = sum(1 for r in rows if r["status"] == "sitting")
    hist = sum(1 for r in rows if r["status"] == "historical")
    vacant = sum(1 for r in rows if r["status"] == "vacant")
    counties = {r["county"] for r in rows}
    print(
        f"Wrote {len(rows)} rows ({sitting} sitting, {hist} historical, "
        f"{vacant} vacant) across {len(counties)} counties to {src}"
    )
    empty = [c["county"] for c in coverage if c["sitting"] == 0 and c["vacant"] == 0]
    print("Counties with no people:", ", ".join(empty))


if __name__ == "__main__":
    main()
