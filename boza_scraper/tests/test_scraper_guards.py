import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup

from main import (
    _collect_docs_from_html,
    _drop_leading_planning_commission,
    _is_document_link,
    _table_is_non_bza_board,
    parse_current_members,
    parse_locked_roster_tables,
    parse_minutes_attendance,
)


CALHOUN_HTML = """
<html><body>
<h1>Planning Commission & Board of Zoning Appeals</h1>
<table>
<tr><th>Planning Commission</th><th>Zoning District</th></tr>
<tr><td>Barry Hill (Chairman)</td><td>District 3</td></tr>
<tr><td>Josh Johnson</td><td>District 1</td></tr>
</table>
<table>
<tr><th>Board of Zoning Appeals</th><th>Zoning District</th></tr>
<tr><td>David Stack</td><td>District 5</td></tr>
<tr><td>Robert Jeffcoat</td><td>District 3</td></tr>
</table>
</body></html>
"""


def test_skip_planning_commission_table():
    soup = BeautifulSoup(CALHOUN_HTML, "html.parser")
    tables = soup.find_all("table")
    assert _table_is_non_bza_board(tables[0]) is True
    assert _table_is_non_bza_board(tables[1]) is False


def test_drop_leading_planning_commission():
    text = (
        "Planning Commission\nBarry Hill\nDistrict 3\n"
        "Board of Zoning Appeals\nZoning District\nDavid Stack\nDistrict 5\n"
    )
    scoped = _drop_leading_planning_commission(text)
    assert "Barry Hill" not in scoped
    assert "David Stack" in scoped


def test_parse_calhoun_keeps_only_bza():
    members = parse_current_members(CALHOUN_HTML, "Calhoun")
    names = {m["name"] for m in members if m.get("status") != "vacant"}
    assert "David Stack" in names
    assert "Robert Jeffcoat" in names
    assert "Barry Hill" not in names
    assert "Josh Johnson" not in names


CHARLESTON_SUMMARY = """
CHARLESTON COUNTY
BOARD OF ZONING APPEALS (BZA)
SUMMARY OF MAY 4, 2026 MEETING
Members Present
Chair, Mr. William Ray, Vice Chair, Mr. Ross Nelson, Mr. AD Jordan, Mr. Roy Neal, Ms. Jessica Smith, and Mr. Doug
Truslow
Members Absent
Mr. Brad Brown, Mr. Robert Siedell, and Ms. Shana Smith
Staff Members Present
Mr. Kelvin Huger, BZA Attorney; Sally Brooks, Planner IV; Jenny Werking, Planner IV
"""

GREENVILLE_MINUTES = """
Greenville County Board of Zoning Appeals
Meeting Minutes
December 14, 2022
Board Members:
1. Barber, Teresa
2. Farrar, Brittany
3. Godfrey, Laura – Vice Chairwoman
4. Hamilton, Paul
5. Hattendorf, Mark – Chairman
6. Hollingshad, Nicholas
7. Matesevac, Kenneth – Absent
Staff Present:
Jane Doe, Planner
New Business
"""

GREENVILLE_ROSTER_HTML = """
<html><body>
<table>
<tr><th>Name</th><th>District</th><th>Term Expires</th></tr>
<tr><td>Lisa Bracewell</td><td>17</td><td>5/31/2028</td></tr>
<tr><td>James Akers, Jr.</td><td>20</td><td>5/31/2026</td></tr>
<tr><td>Vacant</td><td>28</td><td>5/31/2027</td></tr>
</table>
<div>How Do I? Pay My Taxes Find Elected Officials Follow Us</div>
</body></html>
"""

CHARLESTON_OPTIONS_HTML = """
<html><body>
<select>
<option value="https://www.charlestoncounty.gov/departments/zoning-planning/bza-minutes/archived/2024/06-03-2024.pdf">June 03, 2024</option>
<option value="https://www.charlestoncounty.gov/departments/zoning-planning/bza-minutes/archived/2018/jun-2018.pdf">June 2018</option>
</select>
</body></html>
"""


def test_charleston_minutes_exclude_staff():
    rows = parse_minutes_attendance(CHARLESTON_SUMMARY)
    names = {r["name"] for r in rows}
    assert "William Ray" in names or "William H. Ray" in names
    assert "Ross Nelson" in names
    assert "Doug Truslow" in names
    assert "Shana Smith" in names
    assert "Jenny Werking" not in names
    assert "Sally Brooks" not in names
    assert "Kelvin Huger" not in names


def test_greenville_numbered_board_members():
    rows = parse_minutes_attendance(GREENVILLE_MINUTES)
    names = {r["name"] for r in rows}
    assert "Teresa Barber" in names
    assert "Laura Godfrey" in names
    assert "Kenneth Matesevac" in names
    assert "New Business" not in names
    absent = {r["name"] for r in rows if r["attendance"] == "absent"}
    assert "Kenneth Matesevac" in absent


def test_locked_roster_tables_skip_nav():
    members = parse_locked_roster_tables(GREENVILLE_ROSTER_HTML, "Greenville")
    names = {m["name"] for m in members if m.get("status") != "vacant"}
    assert "Lisa Bracewell" in names
    assert "How Do I" not in names
    assert "Pay My Taxes" not in names
    assert any(m.get("status") == "vacant" for m in members)


def test_collect_charleston_option_pdfs():
    docs = _collect_docs_from_html(
        "https://www.charlestoncounty.gov/departments/zoning-planning/bza.php",
        CHARLESTON_OPTIONS_HTML,
        set(),
        assume_bza=True,
    )
    urls = {d["url"] for d in docs}
    assert any("2024/06-03-2024.pdf" in u for u in urls)
    assert any("2018/jun-2018.pdf" in u for u in urls)


def test_greenville_details_aspx_is_document():
    url = (
        "https://www.greenvillecounty.org/apps/DirectoryListingGC/Details.aspx"
        "?d=BZAAgendas&f=D%3a%5cMinutes%5c2025%5c01+January+08%2c+2025.pdf"
    )
    assert _is_document_link(url, "01 January 08, 2025.pdf") is True
