# South Carolina County BZA/BOZA Data Holes and Anomalies

**Source of truth audited:** `origin/cursor/historic-county-data-a437` → `boza_scraper/boza_members.csv`  
**Snapshot:** 264 rows, 8 columns (`state`, `county`, `name`, `status`, `term_start`, `term_end`, `gender`, `tenure`)  
**Universe:** all 46 South Carolina counties  
**Counts:** 32 counties have at least one row · 14 counties are empty · 208 sitting · 56 historical  
**Date of audit:** 2026-09-04

This is a hole-and-anomaly report, not a roster. Every item below is backed by the current CSV and, where noted, by a live county page.

---

## 1. Empty counties (14 of 46)

No rows at all. These are the largest coverage holes.

| County | Configured source | What that source actually is |
| --- | --- | --- |
| Allendale | County administration staff page | Zoning Administrator only (Joe Mole). No BZA roster. |
| Bamberg | Building / planning-zoning functions | Department page, not a member list. |
| Barnwell | Zoning page | Department page, not a member list. |
| Cherokee | Planning Commission | Wrong board. No BZA URL. |
| Chesterfield | Building codes | Department page, not a member list. |
| Dillon | Building codes + boards & commissions | Generic boards page. No extracted members. |
| Hampton | Boards & commissions | Generic boards page. No extracted members. |
| Laurens | Planning department | Department page, not a member list. |
| Lee | Planning and zoning | Department page, not a member list. |
| Marion | Planning department | Department page, not a member list. |
| Marlboro | Planning / zoning / permits | Department page, not a member list. |
| Saluda | **Legislative Delegation** | Confirmed wrong page. Appointments are mentioned; no BZA roster. |
| Union | Building maintenance + boards & commissions | Generic pages. No extracted members. |
| Williamsburg | AgendaCenter only | Meeting list, no roster URL. |

These 14 are clustered in the rural/Pee Dee and lower-Savannah belt. Several look like **source-discovery failures** (wrong or generic URLs), not proof that the county has no board.

---

## 2. Thin, vacant, or contaminated sitting rosters

Typical SC county BZA is 5–9 sitting members, often 7.

### Confirmed wrong-board contamination

**Calhoun (10 sitting, 0 historic).**  
Live page is a combined Planning Commission & Board of Zoning Appeals. The scraper ingested both tables and marked all 10 as sitting BZA.

- Real BZA (5): David Stack, Robert Jeffcoat, Steve Turkvan, Herbert Edmond, Steve Tyson.
- Planning Commission wrongly included (5): Barry Hill, Ray Keitt, Josh Johnson, Josh Rabon, Tamesha Gilmore.
- Duplicate district labels (D2 ×3, D3 ×2, D4 ×2, D5 ×2) are the giveaway.

### Confirmed vacancies (not a scraper miss)

**Lexington (7 sitting, 0 historic).**  
Official page says the board has **nine** seats, one per council district. Districts **1 and 5 are Vacant**. The 7 named people match the filled seats. Tenure is still dirty (see §6).

### Too few sitting members

| County | Sitting | Historic | Why it is a hole |
| --- | --- | --- | --- |
| Edgefield | 1 | 2 | Only Calvin Jackson is sitting (term through 3/2026). Reuben Carver and Sheryl Champy end 3/2025 and are marked historical. A 5-member ZBA would still be missing 2 seats. |
| Jasper | 4 | 2 | Sitting names only, no terms. Below a standard 5-seat board. |
| McCormick | 4 | 0 | From a scanned multi-board contact sheet. Almost certainly incomplete. Tenure also leaks PII (see §7). |
| Abbeville | 5 | 0 | District seats 2, 4, 5, 6, 7 only. **Districts 1 and 3 missing.** |
| Anderson | 6 | 4 | District seats 1, 2, 3, 5, 6, 7. **District 4 missing.** Historic four have no years and blank tenure. |
| Florence | 7 | 0 | Districts 1–6 and 8. **District 7 missing.** No term dates. |
| Richland | 6 | 0 | Named roster with terms, but 6 is short of a typical 7-seat board. |
| Kershaw | 5 | 0 | Minutes attendance 2025 only. Terms are the attendance year, not appointments. |

### Too many sitting members

| County | Sitting | Why it is an anomaly |
| --- | --- | --- |
| Calhoun | 10 | Planning Commission mixed in (above). |
| Sumter | 10 | All from 2025–2026 minutes attendance, not a published roster. Includes Steven Schumpert, seen only as absent. Official BZA page exists but was not used as the roster. |

---

## 3. Sitting vs historic coverage gaps

Historic coverage is the weakest part of the file.

- **56 historical rows** vs **208 sitting**.
- Only **10 of 32** populated counties have any historical row.
- **22 populated counties have zero historic members:** Abbeville, Aiken, Calhoun, Charleston, Clarendon, Darlington, Dorchester, Fairfield, Florence, Georgetown, Greenville, Greenwood, Horry, Kershaw, Lancaster, Lexington, McCormick, Oconee, Richland, Spartanburg, Sumter, York.

### Counties with any historic depth

| County | Historic | Year span | Quality |
| --- | --- | --- | --- |
| Beaufort | 22 | 1998–2022 | Best in the file. From the official former-members page, plus minutes. Names are often still `Last First` (see §5). |
| Pickens | 13 | 2011–2022 | Minutes/OCR. Bob Fetterly is mis-labeled historical (see §6). |
| Chester | 6 | 2025 or blank | Five have blank tenure and no years. Melvin Jackson is a duplicate of a sitting member. |
| Anderson | 4 | none | Blank tenure, no terms. Unverifiable. |
| Berkeley | 3 | 2010–2024 | Expired 2024 seats from a master board PDF. Tenure leaks addresses. |
| Edgefield | 2 | 2022–2025 | Recent term-outs, may still be vacant rather than truly historic. |
| Jasper | 2 | 2018–2023 | Minutes only. |
| Newberry | 2 | none | Sam Ziady, Torchia Werts. Blank tenure, no terms. |
| Colleton | 1 | 2021–2025 | Douglas Mixson, Jr. still appears in 2025 minutes; may still be sitting. |
| Orangeburg | 1 | none | William Weathers. Blank tenure, no terms. |

### Spartanburg historic miss (high-confidence scraper hole)

Spartanburg was the historic-minutes pilot county. The live roster page is complete (9 sitting, matches the CSV). The CSV still has **zero historical rows**. The CivicPlus AgendaCenter archive was supposed to produce former members (Kyle Atkins, Marion Gramling, Michael Padgett were cited as examples). That crawl did not land in the file.

Structural cause in `config.py`: `MAX_DOCS_PER_YEAR = 2`, `MAX_DOCS_KEEP = 20`, `MAX_DOCS_SCAN = 80`. That is too thin for a real historic corpus.

---

## 4. Missing or invalid term dates

- **82 / 264 rows (31%)** have both `term_start` and `term_end` blank.
- **182 / 264** have at least one year.
- No unparseable year strings. No `start > end`.

### Sitting members with no term dates at all

| County | Sitting missing both terms |
| --- | --- |
| Anderson | 6 / 6 |
| Calhoun | 10 / 10 |
| Dorchester | 7 / 7 |
| Florence | 7 / 7 |
| Greenwood | 7 / 7 |
| Jasper | 4 / 4 |
| Lexington | 7 / 7 |
| McCormick | 4 / 4 |
| Oconee | 7 / 7 |
| Pickens | 6 / 6 |
| Orangeburg | 3 / 6 |
| Fairfield | 1 / 8 (Adam Smith) |

### Same-year “terms” that are really attendance years (37 rows)

The scraper wrote the minutes year into both `term_start` and `term_end`. That is not an appointment term.

- Aiken: 9 (all 2025–2025)
- Charleston: 9 (all 2026–2026)
- Fairfield: 7 (all 2025–2025)
- Kershaw: 5 (all 2025–2025)
- Chester: 3 (including the 2028–2028 bug below)
- plus Beaufort / Jasper / Pickens / Sumter one-offs

### Expired sitting (31 rows)

Marked `sitting` but `term_end` is before 2026. Most are the 2025 attendance-year rows above. Real expiry cases:

- **Beaufort (7 sitting):** every sitting member has `term_end=2025` from minutes. Status may be stale.
- **Georgetown (MatchBoard):** Adam Hall, Kathy Besse, Will Moody expire `2025-03-15` and are still sitting.
- **Edgefield:** Carver and Champy correctly flipped to historical at 3/2025; the board is then short.

### Broken term parse

- **Chester / Melvin B. Jackson:** `term_start=2028`, `term_end=2028`. Tenure text says reappointed 03-2025, ends 12-2028. Start year is wrong.
- **Chester / Wallace Hayes:** `2026–2026` even though tenure says appointment ends 12-2026 (start should be the 01-2023 reappointment).
- **Clarendon / Johnny Wilson:** `term_end=2033` with no start. Unusually long vs the other Clarendon 2029 expirations.
- **Colleton:** Anthony Bunton and Mark Wysong expire 2030 with no start.
- **MatchBoard rows** generally have `term_end` only (Clarendon, Darlington, Georgetown, Orangeburg). No `term_start`.
- **Greenville:** tenure embeds extra integers (`Alexander Ward 17 5/31/2027`) that look like years-of-service or district codes, not parsed into `term_start`.

---

## 5. Name-quality issues

No empty names, no single-token names, no all-caps living names, no exact county+name duplicates. The problems are inversion, OCR, and board-mix.

### Beaufort historic: Last-First not inverted

The former-members page is `Last, First`. Some rows were flipped correctly (`Brad Samuel`, `Sue Olsen`). These were left as Last First:

Baisch Gregory; Beil Peter F.; Bootle Charles W.; Boysen Bruce H.; Chmelik Diane J.; DeMartin James L.; Gasparini Thomas A.; Ladson William; LeRoy Phillip; Mack Stanley; Rivers Bernard.

### Likely OCR / misspelling

| County | As stored | Likely issue |
| --- | --- | --- |
| Aiken | Jason Whinghter | OCR; possibly Whittington / Wingate |
| Aiken | Doug Engebrethson | Likely Engebretson |
| Orangeburg | Hebert Sellers | Likely Herbert |
| Charleston | Ad Jordan | Possibly A.D. or Ed |
| Richland | Anette Nelson | Possibly Annette |
| Darlington | Williams Jackson | Possible Last-First inversion |
| Greenwood | Richardson Thomason | Ambiguous first vs last |
| Pickens | Harry E. Carson JR. | Inconsistent Jr. casing |
| Beaufort | Mark Mcginnis | McGinnis casing |
| Chester | Mike Mcbrayer | McBrayer casing |

### Unusual but possibly real

Billy Sunday Joy (Berkeley), Doc Dunlap (Sumter), Cook Young (Kershaw), Torchia Werts (Newberry), La'Jessica Stringfellow (Richland), Marlia Barker (McCormick). Not flagged as errors; they need a human pass.

### Suffix / punctuation (not errors)

Robert Fleming, Jr.; Douglas Mixson, Jr.; Ralph Shaffer, Jr; William “Billy” Drawdy; Jack D. Gowan Jr.; James David Langford Jr.

---

## 6. Status inconsistencies

- Status values are only `sitting` and `historical`. No blank status.
- **Pickens / Bob Fetterly:** source is “Members Absent; May 4 2026 BOA minutes OCR” and status is `historical`. Absent from one 2026 meeting is not a term-out. He is probably still sitting.
- **Colleton / Douglas Mixson, Jr.:** historical, but minutes attendance runs through 2025. May still be sitting.
- **Edgefield / Carver & Champy:** historical at 3/2025 is consistent with the term dates, but the board then has one sitting member.
- **Aiken, Charleston, Kershaw, Fairfield, Sumter:** `sitting` is inferred from being present (or absent) in recent minutes, not from a current roster. That over-states certainty.
- **Calhoun Planning Commission members:** sitting on the wrong board (see §2).
- **Chester / Melvin Jackson (historical, 2025) vs Melvin B. Jackson (sitting):** same person, split across statuses.

---

## 7. Gender, tenure, and PII

### Gender

| Value | Rows |
| --- | --- |
| male | 136 |
| BLANK | 80 |
| female | 29 |
| unknown | 19 |

Entire counties with **all gender blank:** Abbeville (5), Aiken (9), Greenwood (7), Lancaster (5), Oconee (7), Richland (6), Spartanburg (9), York (7).  
Beaufort `unknown` is concentrated on the un-inverted Last-First historic names, because the gender guesser saw a surname first.

Blank vs `unknown` is itself inconsistent. Same pipeline, two missing-value encodings.

### Tenure / source mix

| Tenure signal | Rows |
| --- | --- |
| other / raw roster text | 88 |
| minutes attendance | 76 |
| district text | 58 |
| MatchBoard | 22 |
| blank | 12 |
| named roster | 8 |

Blank tenure is almost all low-quality historic: Anderson (4), Chester (5), Newberry (2), Orangeburg (1).

### PII leaked into `tenure`

Do not treat `tenure` as a public display field.

- **McCormick (4):** phones, street addresses, and emails. At least two emails look OCR-broken (`gmailcom`, `outloo.com`).
- **Spartanburg (9):** home/business phones and street addresses copied off the official roster table.
- **Berkeley (sitting + historic):** street addresses from the master boards PDF.

### Lexington tenure concatenation (confirmed)

Official table is `Council District | Council Member | Zoning Board Member`. Tenure stuffed both names into one string, e.g. `9 M. Todd Cullum Carl Sherwood`, `2 Larry Brigham, Jr. Charles Caughman`. The `name` column kept the BZA member, so the people are right; the tenure field is not.

---

## 8. Duplicates

- **Exact county + name duplicates:** 0.
- **Cross-county same name:** 0.
- **Near-duplicate, same person:** Chester `Melvin B. Jackson` (sitting, broken 2028–2028 terms) and `Melvin Jackson` (historical, 2025 minutes). One person, two rows.
- False near-dup from suffix handling: Spartanburg `Jack D. Gowan Jr.` vs `James David Langford Jr.` (different people; `Jr.` was treated as a last name).

---

## 9. Source and pipeline problems

These are the reasons the holes above exist.

1. **14 empty counties** are pointed at generic department, planning-commission, AgendaCenter, or (Saluda) legislative-delegation pages. There is no BZA roster URL for those counties.
2. **Calhoun’s known URL is a combined PC + BZA page.** No table-type filter, so Planning Commission members became BZA sitting.
3. **Historic crawl did not fan out.** 22 of 32 populated counties have no historic rows. Spartanburg, the named pilot, has none.
4. **Doc caps are too small** (`MAX_DOCS_PER_YEAR=2`, `MAX_DOCS_KEEP=20`) for a sitting+historic corpus.
5. **Minutes-as-roster** (Aiken, Charleston, Fairfield, Kershaw, Sumter, parts of Beaufort/Pickens) writes attendance years as terms and marks attendees sitting.
6. **MatchBoard is mapped for Beaufort, Calhoun, Greenwood, and Kershaw** in `county_sources.py` but those counties were not actually filled from MatchBoard sitting rosters. Georgetown / Clarendon / Darlington / Orangeburg were, and those rows have stale or missing expirations.
7. **Cherokee’s only URL is the Planning Commission.** Same wrong-board class as Calhoun, except it produced zero rows.
8. **Gender fill is uneven.** Historic LLM rows often have gender; live roster rows from the same county often do not.
9. **PII passthrough.** Roster OCR / HTML tables were dumped into `tenure` without stripping phones, emails, or addresses.

---

## 10. County-by-county scorecard

| County | Rows | Sit | Hist | Verdict |
| --- | --- | --- | --- | --- |
| Abbeville | 5 | 5 | 0 | Missing D1 and D3; no historic; no gender. |
| Aiken | 9 | 9 | 0 | Minutes-only; 2025–2025 fake terms; OCR names; no gender. |
| Allendale | 0 | 0 | 0 | Empty. Admin page only. |
| Anderson | 10 | 6 | 4 | Missing D4; sitting have no terms; historic unverifiable. |
| Bamberg | 0 | 0 | 0 | Empty. |
| Barnwell | 0 | 0 | 0 | Empty. |
| Beaufort | 29 | 7 | 22 | Best historic file; sitting terms stale at 2025; many Last-First names. |
| Berkeley | 8 | 5 | 3 | Address PII in tenure; 2024 seats marked historical. |
| Calhoun | 10 | 10 | 0 | **5 Planning Commission + 5 BZA.** No terms. |
| Charleston | 9 | 9 | 0 | Minutes-only 2026 attendance as terms. |
| Cherokee | 0 | 0 | 0 | Empty. Planning Commission URL only. |
| Chester | 11 | 5 | 6 | Missing D1; Melvin Jackson split; start-year 2028 bug; historic mostly blank. |
| Chesterfield | 0 | 0 | 0 | Empty. |
| Clarendon | 5 | 5 | 0 | MatchBoard; no starts; Wilson ends 2033. |
| Colleton | 6 | 5 | 1 | Mixson may still be sitting; two 2030 ends, no starts. |
| Darlington | 5 | 5 | 0 | MatchBoard only; possible `Williams Jackson` inversion. |
| Dillon | 0 | 0 | 0 | Empty. |
| Dorchester | 7 | 7 | 0 | District roster, no terms, no historic. |
| Edgefield | 3 | 1 | 2 | Board effectively 1 sitting after 3/2025. |
| Fairfield | 8 | 8 | 0 | Minutes 2025 terms; Adam Smith has no dates; 3 gender=unknown. |
| Florence | 7 | 7 | 0 | Missing D7; no terms; no historic. |
| Georgetown | 6 | 6 | 0 | MatchBoard; 3 of 6 expired Mar 2025 still sitting. |
| Greenville | 9 | 9 | 0 | Ends only; extra integers in tenure; no historic. |
| Greenwood | 7 | 7 | 0 | Names only. No terms, gender, districts, or historic. |
| Hampton | 0 | 0 | 0 | Empty. |
| Horry | 9 | 9 | 0 | Ends only; no historic. |
| Jasper | 6 | 4 | 2 | Thin sitting; no sitting terms. |
| Kershaw | 5 | 5 | 0 | Minutes 2025 only. |
| Lancaster | 5 | 5 | 0 | Ends only; no gender; no historic. |
| Laurens | 0 | 0 | 0 | Empty. |
| Lee | 0 | 0 | 0 | Empty. |
| Lexington | 7 | 7 | 0 | D1 and D5 vacant on the live page; tenure concatenates council + BZA names. |
| Marion | 0 | 0 | 0 | Empty. |
| Marlboro | 0 | 0 | 0 | Empty. |
| McCormick | 4 | 4 | 0 | Thin; PII in tenure; no terms. |
| Newberry | 9 | 7 | 2 | Historic unverifiable; sitting ends only. |
| Oconee | 7 | 7 | 0 | District roster, no terms, no gender, no historic. |
| Orangeburg | 7 | 6 | 1 | 3 sitting lack expirations; Hebert Sellers; historic blank. |
| Pickens | 19 | 6 | 13 | Best-ish historic after Beaufort; Fetterly mis-labeled; sitting have no terms. |
| Richland | 6 | 6 | 0 | Possibly short a seat; Anette/Annette; no gender; no historic. |
| Saluda | 0 | 0 | 0 | Empty. Wrong source page. |
| Spartanburg | 9 | 9 | 0 | Sitting matches live roster; **historic pilot produced nothing**; PII in tenure. |
| Sumter | 10 | 10 | 0 | Minutes attendance oversized; official roster unused. |
| Union | 0 | 0 | 0 | Empty. |
| Williamsburg | 0 | 0 | 0 | Empty. AgendaCenter only. |
| York | 7 | 7 | 0 | Clean sitting roster; no gender; no historic. |

---

## 11. Highest-priority holes

If the next scrape pass can only fix a few things, these change the file the most:

1. Fill or honestly mark the **14 empty counties** (new roster URLs, FOIA, or `no_public_roster`).
2. Split **Calhoun** Planning Commission out of the BZA file.
3. Re-run historic minutes without the 2-docs-per-year cap, starting with **Spartanburg**.
4. Stop writing minutes attendance years as `term_start`/`term_end` (**Aiken, Charleston, Kershaw, Fairfield, Sumter**).
5. Strip phones, emails, and addresses out of `tenure` (**McCormick, Spartanburg, Berkeley**).
6. Invert remaining Beaufort `Last, First` historic names.
7. Merge Chester `Melvin Jackson` / fix the 2028 start year.
8. Relabel Pickens `Bob Fetterly` as sitting.
9. Refresh stale MatchBoard expirations (**Georgetown**).
10. Record Lexington D1/D5 as vacant seats, not missing data.
