import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cleanup import (
    invert_last_first,
    invert_name_from_tenure,
    is_attendance_only_tenure,
    sanitize_tenure,
)
from repair_csv import CALHOUN_PLANNING_COMMISSION, CHARLESTON_SITTING, GREENVILLE_SITTING, repair


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


def test_repair_drops_spartanburg_blobs():
    rows = [
        {
            "state": "South Carolina",
            "county": "Spartanburg",
            "name": "Director Of Building",
            "status": "historical",
            "term_start": "",
            "term_end": "",
            "gender": "",
            "tenure": "Minutes attendance 2013",
        },
        {
            "state": "South Carolina",
            "county": "Spartanburg",
            "name": "Kyle Atkins",
            "status": "historical",
            "term_start": "",
            "term_end": "",
            "gender": "",
            "tenure": "Minutes attendance 2010",
        },
        {
            "state": "South Carolina",
            "county": "Spartanburg",
            "name": "Glenda Brad Y",
            "status": "historical",
            "term_start": "",
            "term_end": "",
            "gender": "",
            "tenure": "Minutes attendance 2020-2025",
        },
    ]
    fixed = repair(rows)
    names = {r["name"] for r in fixed if r["county"] == "Spartanburg"}
    assert "Director Of Building" not in names
    assert "Kyle Atkins" in names
    assert "Glenda Brady" in names


def test_invert_g_ross_nelson():
    assert invert_last_first("Nelson, G. Ross") == "G. Ross Nelson"
    assert invert_name_from_tenure(
        "G. Nelson", "Nelson, G. Ross Member 23 May 2023 31 Dec 2026 Active"
    ) == "G. Ross Nelson"


def test_repair_locks_charleston_and_greenville():
    rows = [
        {
            "state": "South Carolina",
            "county": "Charleston",
            "name": "Jenny Werking",
            "status": "sitting",
            "term_start": "",
            "term_end": "",
            "gender": "",
            "tenure": "Planner IV",
        },
        {
            "state": "South Carolina",
            "county": "Charleston",
            "name": "Pay My Taxes",
            "status": "sitting",
            "term_start": "",
            "term_end": "",
            "gender": "",
            "tenure": "nav",
        },
        {
            "state": "South Carolina",
            "county": "Charleston",
            "name": "Samuel McConnell",
            "status": "sitting",
            "term_start": "2010",
            "term_end": "2020",
            "gender": "",
            "tenure": "old",
        },
        {
            "state": "South Carolina",
            "county": "Greenville",
            "name": "Alexander Ward",
            "status": "sitting",
            "term_start": "",
            "term_end": "2027",
            "gender": "",
            "tenure": "Alexander Ward 17 5/31/2027",
        },
        {
            "state": "South Carolina",
            "county": "Greenville",
            "name": "How Do I",
            "status": "sitting",
            "term_start": "",
            "term_end": "",
            "gender": "",
            "tenure": "nav",
        },
    ]
    fixed = repair(rows)
    ch = [r for r in fixed if r["county"] == "Charleston"]
    gv = [r for r in fixed if r["county"] == "Greenville"]
    ch_sitting = {r["name"] for r in ch if r["status"] == "sitting"}
    gv_sitting = {r["name"] for r in gv if r["status"] == "sitting"}
    assert ch_sitting == {name for name, *_ in CHARLESTON_SITTING}
    assert "Jenny Werking" not in {r["name"] for r in ch}
    assert "Pay My Taxes" not in {r["name"] for r in ch}
    sam = next(r for r in ch if r["name"] == "Samuel McConnell")
    assert sam["status"] == "historical"
    assert sam["term_start"] == ""
    assert gv_sitting == {name for name, *_ in GREENVILLE_SITTING}
    ward = next(r for r in gv if r["name"] == "Alexander Ward")
    assert ward["status"] == "historical"
    assert "How Do I" not in {r["name"] for r in gv}
    assert any(r["status"] == "vacant" and "District 28" in r["tenure"] for r in gv)
