import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cleanup import (
    invert_last_first,
    invert_name_from_tenure,
    is_attendance_only_tenure,
    sanitize_tenure,
)
from repair_csv import CALHOUN_PLANNING_COMMISSION, repair


def test_sanitize_strips_phone_email_address():
    raw = (
        "James Gray 706-814-0437 376 Jefferson St., McCormick, SC 29835 "
        "jaygray2448@gmail.com"
    )
    cleaned = sanitize_tenure(raw)
    assert "706" not in cleaned
    assert "@" not in cleaned
    assert "Jefferson" not in cleaned
    assert "James Gray" in cleaned


def test_invert_last_first_comma():
    assert invert_last_first("Baisch, Gregory") == "Gregory Baisch"


def test_invert_from_tenure():
    assert invert_name_from_tenure(
        "Baisch Gregory", "Baisch, Gregory January 2012 - March 2016"
    ) == "Gregory Baisch"
    assert invert_name_from_tenure(
        "Beil Peter F.", "Beil, Peter F. September 1998 - April 2001"
    ) == "Peter F. Beil"


def test_attendance_only_tenure():
    assert is_attendance_only_tenure("Minutes attendance 2025; seen as present")
    assert not is_attendance_only_tenure("District 5 06/2025-06/2029")


def test_repair_drops_calhoun_planning_and_adds_marion():
    rows = [
        {
            "state": "South Carolina",
            "county": "Calhoun",
            "name": "Barry Hill",
            "status": "sitting",
            "term_start": "",
            "term_end": "",
            "gender": "",
            "tenure": "Barry Hill (Chairman) District 3",
        },
        {
            "state": "South Carolina",
            "county": "Calhoun",
            "name": "David Stack",
            "status": "sitting",
            "term_start": "",
            "term_end": "",
            "gender": "",
            "tenure": "David Stack District 5",
        },
        {
            "state": "South Carolina",
            "county": "Pickens",
            "name": "Bob Fetterly",
            "status": "historical",
            "term_start": "",
            "term_end": "",
            "gender": "",
            "tenure": "Members Absent; May 4 2026 BOA minutes OCR",
        },
    ]
    assert "barry hill" in CALHOUN_PLANNING_COMMISSION
    fixed = repair(rows)
    calhoun = [r for r in fixed if r["county"] == "Calhoun"]
    assert [r["name"] for r in calhoun if r["status"] == "sitting"] == ["David Stack"]
    fetterly = next(r for r in fixed if r["name"] == "Bob Fetterly")
    assert fetterly["status"] == "sitting"
    marion = [r for r in fixed if r["county"] == "Marion" and r["status"] == "sitting"]
    assert len(marion) == 7
    vacant = [r for r in fixed if r["status"] == "vacant"]
    assert any(r["county"] == "Lexington" for r in vacant)
