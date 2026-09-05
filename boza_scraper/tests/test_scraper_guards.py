import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup

from main import (
    _drop_leading_planning_commission,
    _table_is_non_bza_board,
    parse_current_members,
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
