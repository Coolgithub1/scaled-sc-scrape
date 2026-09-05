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
    ("Charleston", "a.d. jordan"): "AD Jordan",
    ("Charleston", "ross nelson"): "G. Ross Nelson",
    ("Charleston", "g. nelson"): "G. Ross Nelson",
    ("Charleston", "g nelson"): "G. Ross Nelson",
    ("Charleston", "william ray"): "William H. Ray",
    ("Charleston", "william h. ray jr"): "William H. Ray",
    ("Charleston", "sammuel mcconnell"): "Samuel McConnell",
    ("Charleston", "john e. bevon jr"): "John E. Bevon Jr.",
    ("Charleston", "tonnia switzer- smalls"): "Tonnia Switzer-Smalls",
    ("Charleston", "mare marchant"): "Marc Marchant",
    ("Charleston", "ross d. nelson"): "G. Ross Nelson",
    ("Charleston", "sr dino manos"): "Dino Manos",
    ("Charleston", "thomas goldstein"): "Thomas R. Goldstein",
    ("Greenville", "james akers"): "James Akers Jr.",
    ("Greenville", "james akers, jr."): "James Akers Jr.",
    ("Greenville", "brittney farrar"): "Brittany Farrar",
    ("Beaufort", "mark mcginnis"): "Mark McGinnis",
    ("Chester", "mike mcbrayer"): "Mike McBrayer",
    ("Pickens", "harry e. carson jr."): "Harry E. Carson Jr.",
    ("Beaufort", "stanley mack"): "Stanley Mack",
    ("Spartanburg", "glenda brad y"): "Glenda Brady",
    ("Spartanburg", "louise rake s"): "Louise Rakes",
    ("Spartanburg", "jr. marion gramling"): "Marion Gramling Jr.",
    ("Spartanburg", "tracy mccall"): "Tracy McCall",
}

DROP_NAME_KEYS = {
    "director of building",
    "jr. angela viney",
    "jr. jason patrick louise rakes marion gramling john harris",
    "jr. marion gramling jason patrick louise rakes",
    "jr. thomas davies kae fleming jason patrick joan holliday",
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

CHARLESTON_SITTING = [
    ("Brad Brown", "2025", "2028", "eScribe BoardInformation/28; term 1 Jan 2025-31 Dec 2028"),
    ("AD Jordan", "2023", "2026", "eScribe BoardInformation/28; appointed 23 May 2023"),
    ("Roy Neal", "2023", "2026", "eScribe BoardInformation/28; appointed 25 Apr 2023"),
    ("G. Ross Nelson", "2023", "2026", "eScribe BoardInformation/28; appointed 23 May 2023"),
    ("William H. Ray", "2025", "2028", "eScribe BoardInformation/28; term 1 Jan 2025-31 Dec 2028"),
    ("Robert Siedell", "2023", "2026", "eScribe BoardInformation/28; appointed 23 May 2023"),
    ("Jessica Smith", "2025", "2028", "eScribe BoardInformation/28; appointed 25 Feb 2025"),
    ("Shana Smith", "2025", "2028", "eScribe BoardInformation/28; term 1 Jan 2025-31 Dec 2028"),
    ("Doug Truslow", "2023", "2026", "eScribe BoardInformation/28; appointed 23 May 2023"),
]

GREENVILLE_SITTING = [
    ("Lisa Bracewell", "", "2028", "District 17; expires 5/31/2028; BoardDetails id=76"),
    ("James Akers Jr.", "", "2026", "District 20; expires 5/31/2026; BoardDetails id=76"),
    ("Laura Godfrey", "", "2026", "District 21; expires 11/30/2026; BoardDetails id=76"),
    ("Brennan Stonerock", "", "2027", "District 21; expires 5/31/2027; BoardDetails id=76"),
    ("Christopher Winters", "", "2027", "District 21; expires 5/31/2027; BoardDetails id=76"),
    ("Josh Hakala", "", "2026", "District 22; expires 11/30/2026; BoardDetails id=76"),
    ("John Boyanoski", "", "2027", "District 23; expires 5/31/2027; BoardDetails id=76"),
    ("Michael Roth", "", "2026", "District 24; expires 5/31/2026; BoardDetails id=76"),
]

GREENVILLE_VACANT = "District 28 vacant; expires 5/31/2027"

# Historic names taken from dated BZA minutes attendance / Board Members lists.
CHARLESTON_HISTORIC = [
    ("Samuel McConnell", "Minutes attendance 2010-2020"),
    ("Laura Khare", "Minutes attendance 2010-2015"),
    ("Charles Baker", "Minutes attendance 2010"),
    ("Robert Woodul", "Minutes attendance 2010-2020"),
    ("T. Jackson Bender", "Minutes attendance 2010"),
    ("Clyde J. Smalls", "Minutes attendance 2011-2012"),
    ("Thomas R. Goldstein", "Minutes attendance 2011-2019"),
    ("Robert A. Pickard", "Minutes attendance 2011-2014"),
    ("John R. Hope", "Minutes attendance 2011"),
    ("Dino Manos", "Minutes attendance 2011-2016"),
    ("Leonard Blank", "Minutes attendance 2012"),
    ("Terri Craven", "Minutes attendance 2012-2018"),
    ("John E. Bevon Jr.", "Minutes attendance 2013-2018"),
    ("Cheryl Cromwell", "Minutes attendance 2013-2016"),
    ("Joel Evans", "Minutes attendance 2011-2018"),
    ("Lauri Lechner", "Minutes attendance 2017-2020"),
    ("Ronald Ladson", "Minutes attendance 2018"),
    ("Megan Martino", "Minutes attendance 2018-2019"),
    ("Joseph A. Boykin", "Minutes attendance 2019-2022"),
    ("H. Bernard Freeman", "Minutes attendance 2019-2022"),
    ("Keane Steele", "Minutes attendance 2019"),
    ("Marc Marchant", "Minutes attendance 2021-2024"),
    ("Jesse Williams", "Minutes attendance 2021-2022"),
    ("Savanah Murray", "Minutes attendance 2021-2022"),
    ("Morgan Asbell", "Minutes attendance 2022"),
    ("Tonnia Switzer-Smalls", "Minutes attendance 2022"),
]
GREENVILLE_HISTORIC = [
    ("Teresa Barber", "Minutes attendance 2022-2024"),
    ("Brittany Farrar", "Minutes attendance 2022-2024"),
    ("Paul Hamilton", "Minutes attendance 2022-2024"),
    ("Mark Hattendorf", "Minutes attendance 2022-2024"),
    ("Nicholas Hollingshad", "Minutes attendance 2022-2023"),
    ("Kenneth Matesevac", "Minutes attendance 2022-2023"),
    ("Michelle Shuman", "Minutes attendance 2022-2024"),
    ("Yolanda Brockman", "Minutes attendance 2024-2025"),
    ("Angelica Hall", "Minutes attendance 2024-2025"),
    (
        "Alexander Ward",
        "Former BoardDetails appointee (District 17, listed through 5/31/2027); "
        "not on the current official 9-seat roster",
    ),
]

CHARLESTON_STAFF_KEYS = {
    "kelvin huger", "jr. kelvin huger", "sally brooks", "win carlisle",
    "genesis clark", "niki grimball", "cole hair", "riley hays",
    "andrea melocik-white", "andrea melocik white", "andrea pietras",
    "karie vasche", "karie vasché", "jenny werking", "joyce mcgrew",
    "stephen risse", "lee gastley", "jennifer stiles", "joshua downey",
    "joel evans",  # appears in staff-mixed OCR years after 2018
}

CHARLESTON_JUNK_KEYS = {
    "for the bza", "state regulations",
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
        if key in DROP_NAME_KEYS or key.startswith("jr. ") and " " in key[4:] and len(key.split()) >= 5:
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

        if county == "Colleton" and key.startswith("douglas mixson"):
            # Still in 2025 minutes; keep historical only if we have no 2026 sighting.
            tenure = re.sub(
                r"(?:\s*\|\s*still appearing in 2025 minutes)+",
                "",
                tenure or "",
            ).strip(" |")
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

    out = _lock_official_county(
        out, detector, "Charleston", CHARLESTON_SITTING, CHARLESTON_HISTORIC, [],
        extra_drop=CHARLESTON_STAFF_KEYS | CHARLESTON_JUNK_KEYS,
    )
    out = _lock_official_county(
        out, detector, "Greenville", GREENVILLE_SITTING, GREENVILLE_HISTORIC,
        [GREENVILLE_VACANT],
    )
    return out


def _lock_official_county(
    rows, detector, county, sitting, historic, vacant_tenures, extra_drop=None,
):
    """Overwrite sitting from the official roster; keep/add minutes historic."""
    extra_drop = extra_drop or set()
    sitting_map = {_key(name): (name, start, end, note) for name, start, end, note in sitting}
    historic_map = {_key(name): (name, note) for name, note in historic}
    kept = []
    seen_keys = set()
    for row in rows:
        if row["county"] != county:
            kept.append(row)
            continue
        name = row.get("name") or ""
        key = _key(name)
        if name == VACANT_NAME or key in extra_drop:
            continue
        if key in sitting_map:
            official_name, start, end, note = sitting_map[key]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            kept.append({
                "state": STATE,
                "county": county,
                "name": official_name,
                "status": "sitting",
                "term_start": start,
                "term_end": end,
                "gender": _guess_gender(detector, official_name),
                "tenure": note,
            })
            continue
        if key not in historic_map:
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        hist_name, hist_note = historic_map[key]
        kept.append({
            "state": STATE,
            "county": county,
            "name": hist_name,
            "status": "historical",
            "term_start": "",
            "term_end": "",
            "gender": _guess_gender(detector, hist_name),
            "tenure": hist_note,
        })
    for key, (name, start, end, note) in sitting_map.items():
        if key in seen_keys:
            continue
        seen_keys.add(key)
        kept.append({
            "state": STATE,
            "county": county,
            "name": name,
            "status": "sitting",
            "term_start": start,
            "term_end": end,
            "gender": _guess_gender(detector, name),
            "tenure": note,
        })
    for key, (name, note) in historic_map.items():
        if key in seen_keys:
            continue
        seen_keys.add(key)
        kept.append({
            "state": STATE,
            "county": county,
            "name": name,
            "status": "historical",
            "term_start": "",
            "term_end": "",
            "gender": _guess_gender(detector, name),
            "tenure": note,
        })
    existing_vacant = {
        (r["county"], r.get("tenure") or "")
        for r in kept
        if r["county"] == county and r["status"] == "vacant"
    }
    for seat in vacant_tenures:
        if (county, seat) not in existing_vacant:
            kept.append(vacant_row(STATE, county, seat))
    return kept


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
        if county == "Charleston":
            extras.append("sitting from eScribe BoardInformation/28; historic from minutes OCR")
        if county == "Greenville":
            extras.append("sitting from BoardDetails id=76; District 28 vacant")
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
