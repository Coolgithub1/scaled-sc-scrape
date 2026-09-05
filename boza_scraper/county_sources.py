# county_sources.py — known BZA / ZBA landing pages for SC counties.
# Used as a first-pass override before heuristic URL discovery.

KNOWN_BOZA_URLS = {
    "Abbeville": [
        "https://abbevillecountysc.com/boards-commissions/",
    ],
    "Aiken": [
        "https://www.aikencountysc.gov/AgendaCenter",
        # Minutes packet filed as an Agenda ViewFile; embeds Members Present.
        "https://www.aikencountysc.gov/AgendaCenter/ViewFile/Agenda/_02122026-51",
    ],
    "Allendale": [
        "https://www.allendalecounty.com/government/county_government/administration.php",
    ],
    "Anderson": [
        "https://www.andersoncountysc.org/departments-a-z/board-of-zoning-appeals/",
        "https://www.andersoncountysc.gov/departments-a-z/board-of-zoning-appeals/",
    ],
    "Bamberg": [
        "https://www.bambergcounty.sc.gov/building-planning/planning-zoning-functions",
    ],
    "Barnwell": [
        "https://www.barnwellcountysc.gov/189/Zoning",
    ],
    "Beaufort": [
        "https://www.beaufortcountysc.gov/zoning-appeals-board/index.html",
        "https://www.beaufortcountysc.gov/zoning-appeals-board/former-members.html",
        "https://www.beaufortcountysc.gov/zoning-appeals-board/minutes/index.html",
    ],
    "Berkeley": [
        "https://www.berkeleycountysc.gov/planning/board-of-zoning-appeals",
        "https://berkeleycountysc.gov/wp-content/uploads/docs/MASTER-LIST-OF-BOARDS-COMM.pdf",
    ],
    "Calhoun": [
        "https://calhouncounty.sc.gov/planning-commission-board-zoning-appeals",
    ],
    "Charleston": [
        "https://www.charlestoncounty.gov/departments/zoning-planning/bza.php",
        "https://www.charlestoncounty.org/departments/zoning-planning/bza.php",
        "https://bm-public-charlestoncounty.escribemeetings.com/BoardDetails/BoardInformation/28",
        # Meeting summaries host Members Present when no separate roster page exists.
        "https://www.charlestoncounty.gov/departments/zoning-planning/bza-agenda/DRAFT-SUMMARY-OF-MARCH-2-2026-BZA-MEETING.pdf",
        "https://www.charlestoncounty.gov/departments/zoning-planning/bza-agenda/DRAFT-SUMMARY-OF-MARCH-2-2026-BZA-ANNUAL-BUSINESS-MEETING.pdf",
    ],
    "Cherokee": [
        "https://cherokeecountysc.gov/building-safety/planning-commission/",
    ],
    "Chester": [
        "https://www.chestercountysc.gov/boards/building-and-zoning/zoning-board-of-appeals/",
        "https://www.chestercountysc.gov/boards/zoning",
    ],
    "Chesterfield": [
        "https://www.chesterfieldcountysc.com/building-codes",
    ],
    "Clarendon": [
        "https://www.clarendoncountysc.gov/business-and-development/planning-and-zoning/",
        # MatchBoard hosts sitting ZBA members (entity 14 / board 363).
        "https://api.matchboard.tech/app/boards/363",
    ],
    "Colleton": [
        # MatchBoard Land Use Zoning Board of Appeal (entity 15 / board 341).
        "https://api.matchboard.tech/app/boards/341",
    ],
    "Darlington": [
        "https://www.darcosc.com/government/boards_commissions/index.php",
        # MatchBoard sitting BZA members (entity 101 / board 351).
        "https://api.matchboard.tech/app/boards/351",
    ],
    "Dillon": [
        "https://dilloncountysc.org/departments/services/building_code_enforcement_planning_zoning.php",
        "https://dilloncountysc.org/leadership/boards_commissions.php",
    ],
    "Dorchester": [
        # Live host is Akamai-blocked from many datacenter IPs; Wayback works.
        "https://web.archive.org/web/20260513195421/https://www.dorchestercountysc.gov/government/planning-development/planning-zoning/board-of-zoning-appeals",
        "https://www.dorchestercountysc.gov/government/planning-development/planning-zoning/board-of-zoning-appeals",
        "https://www.dorchestercounty.org/government/planning-development/planning-zoning/board-of-zoning-appeals",
    ],
    "Edgefield": [
        # Live host often 403 from datacenters; Wayback + direct docx work.
        "https://web.archive.org/web/20250215101124/https://edgefieldcounty.sc.gov/boards-and-commissions/",
        "https://edgefieldcounty.sc.gov/boards-and-commissions/",
        "https://edgefieldcounty.sc.gov/wp-content/uploads/2024/06/Zoning-Board-of-Appeals-Members.docx",
        "https://web.archive.org/web/20250215073029if_/https://edgefieldcounty.sc.gov/wp-content/uploads/2024/06/Zoning-Board-of-Appeals-Members.docx",
    ],
    "Fairfield": [
        "https://www.fairfieldsc.com/departments/planning_and_zoning/",
        "https://www.fairfieldsc.com/uploads/uploads/BZA_Agenda_January_20%2C_2026.pdf",
        "https://www.fairfieldsc.com/uploads/uploads/BZA_Meeting_Minutes_9-15-25.pdf",
        "https://www.fairfieldsc.com/uploads/uploads/BZA_Meeting_Minutes_8-18-25.pdf",
        "https://www.fairfieldsc.com/uploads/uploads/BZA_Meeting_Minutes_6-16-25.pdf",
        "https://www.fairfieldsc.com/uploads/uploads/BZA_Meeting_Minutes_4-21-25.pdf",
    ],
    "Florence": [
        "https://www.florenceco.org/planning/bza/members.php",
        "https://www.florenceco.org/planning/bza/",
        "https://www.florencecountysc.gov/planning/bza",
    ],
    "Georgetown": [
        "https://www.gtcountysc.gov/AgendaCenter",
        "https://www.gtcountysc.gov/177/Planning",
        # MatchBoard sitting ZBA members (entity 22 / board 383).
        "https://api.matchboard.tech/app/boards/383",
    ],
    "Greenville": [
        "https://www.greenvillecounty.org/apps/countycouncilboard/BoardDetails.aspx?id=76",
        "https://www.greenvillecounty.org/Zoning/BoardOfZoningAppeals.aspx",
    ],
    "Greenwood": [
        "https://www.greenwoodcounty-sc.gov/planning",
        "https://drive.google.com/drive/folders/1SYt_WFPyWucm-k5tdj9D4_KXdgeTvACt?usp=sharing",
    ],
    "Hampton": [
        "http://www.hamptoncountysc.org/316/Boards-Commissions",
    ],
    "Horry": [
        # Powers/duties page has no roster; members live on boards & commissions.
        "https://www.horrycountysc.gov/boards-and-commissions/zoning-board-of-appeals/",
        "https://www.horrycountysc.gov/departments/planning-and-zoning/board-of-zoning-appeals/",
    ],
    "Jasper": [
        "https://www.jaspercountysc.gov/government/boards-and-commissions/board-of-zoning-appeals/",
        "https://www.jaspercountysc.gov/government/boards-and-commissions/",
    ],
    "Kershaw": [
        # Live host Akamai-blocks datacenters; Wayback has BZA agendas/minutes.
        "https://web.archive.org/web/20250607205224/https://www.kershaw.sc.gov/government/departments-h-q/planning-zoning/board-of-zoning-appeals-planning-commission",
        "https://www.kershaw.sc.gov/government/departments-h-q/planning-zoning/board-of-zoning-appeals-planning-commission",
        # March 2025 agenda packet embeds May 2024 minutes with Members Present.
        "https://web.archive.org/web/20250607205224if_/https://www.kershaw.sc.gov/home/showpublisheddocument/16352/638757334123430000",
        "https://api.matchboard.tech/app/boards/318",
    ],
    "Lancaster": [
        "https://www.lancastercountysc.gov/447/Board-of-Zoning-Appeals",
        "https://www.lancastercountysc.gov/AgendaCenter/",
    ],
    "Laurens": [
        "https://laurenscountysc.gov/departments/planning/planning.php",
    ],
    "Lee": [
        "https://www.leecountysc.org/directory/departments___elected_officials/planning_and_zoning.php",
    ],
    "Lexington": [
        "https://lex-co.sc.gov/departments/community-development/boards-and-commissions/community-development-board-zoning-appeals",
    ],
    "Marion": [
        "https://www.marionsc.org/departments/planning/index.php",
    ],
    "Marlboro": [
        "https://marlborocounty.sc.gov/services/planning_zoning_permits.php",
    ],
    "McCormick": [
        "https://www.mccormickcountysc.org/how_do_i/boards-commissions.php",
        "https://www.mccormickcountysc.org/government/agendas___minutes.php",
        # Scanned multi-board contact sheet; ZBA section OCRs to named members.
        "https://cms5.revize.com/revize/mccormickcountysc/Document_Center/Government/2025%20Board%20%26%20Commissions.pdf",
        "https://cms5.revize.com/revize/mccormickcountysc/Agenda%20&%20Minutes/Board%20of%20Zoning%20Appeals/BZA%20agenda%20Nov.%2013.pdf",
        "https://cms5.revize.com/revize/mccormickcountysc/Agenda%20&%20Minutes/Zoning/4-23-26.pdf",
    ],
    "Newberry": [
        "https://www.newberrycounty.gov/boards-commissions/board-zoning-appeals",
    ],
    "Oconee": [
        "https://oconeesc.com/council-home/committees-and-commissions/boards-and-commissions/board-of-zoning-appeals",
    ],
    "Orangeburg": [
        "https://www.orangeburgcounty.org/200/Board-of-Zoning-Appeals",
        # MatchBoard sitting ZBA members (entity 38 / board 139). first_name "Hebert" is OCR/typo.
        "https://api.matchboard.tech/app/boards/139",
    ],
    "Pickens": [
        "https://www.co.pickens.sc.us/departments/planning/board_of_appeals_agendas___minutes/index.php",
        "https://www.co.pickens.sc.us/departments/planning/index.php",
        "https://cms5.revize.com/revize/pickenscountysc/document_center/Agendas%20&%20Minutes/Board%20of%20Appeals/2022/AGENDA%2004-25-2022.pdf",
        # May 4, 2026 minutes (scanned) list Members Present; live PDF often 404 from datacenters.
        "https://www.co.pickens.sc.us/departments/planning/board_of_appeals_agendas___minutes/May%204%20Minutes%20-%20Signed.pdf",
    ],
    "Richland": [
        "https://www.richlandcountysc.gov/Government/Get-Involved/Boards-and-Committees/Board-of-Zoning-Appeals",
    ],
    "Saluda": [
        "https://saludacounty.sc.gov/county-council",
        "https://saludacounty.sc.gov/sites/saludacounty/files/Documents/Saluda%20County%20Boards%20%26%20Comm.%20List%208-12-2025.pdf",
        "https://saludacounty.sc.gov/departments/legislative-delegation",
    ],
    "Sumter": [
        "https://www.sumtersc.gov/planning/board-zoning-appeals",
        "https://www.sumtersc.gov/sites/default/files/uploads/Departments/Planning/BoardsCommissions/PC/2026/April/sumter-chapter-a-draft-3.3-pc-presentation.pdf",
    ],
    "Spartanburg": [
        "https://www.spartanburgcounty.gov/371/Board-of-Zoning-Appeals",
    ],
    "Union": [
        "https://gearupunionsc.com/departments/building-maintenance/",
        "https://gearupunionsc.com/boards-commissions/",
    ],
    "Williamsburg": [
        "https://www.williamsburgcounty.sc.gov/AgendaCenter",
    ],
    "York": [
        "https://www.yorkcountysc.gov/1198/Board-of-Zoning-Appeals",
        "https://boardsandcommissions.yorkcountygov.com/Board.aspx?ID=42",
        "https://www.yorkcountysc.gov/AgendaCenter",
    ],
}

# MatchBoard entity IDs for SC counties that publish sitting board members.
MATCHBOARD_ENTITY_IDS = {
    "Beaufort": 7,
    "Calhoun": 9,
    "Clarendon": 14,
    "Colleton": 15,
    "Darlington": 101,
    "Georgetown": 22,
    "Greenwood": 24,
    "Kershaw": 28,
    "Orangeburg": 38,
}

# Honest coverage labels for counties that publish no BZA roster, or have no BZA.
# Used by repair_csv.py / county_coverage.csv — do not invent members for these.
COUNTY_COVERAGE_NOTES = {
    "Allendale": "no_public_roster",
    "Bamberg": "no_public_roster",  # ordinance creates a 5-member BZA; no names published
    "Barnwell": "no_public_roster",
    "Cherokee": "no_county_bza",  # county said it does not have zoning (2023); I-85 proposal pending
    "Chesterfield": "no_public_roster",
    "Dillon": "board_named_no_roster",
    "Hampton": "not_zoning_bza",  # published Board of Adjustments and Appeals is building-codes
    "Laurens": "no_county_bza",  # county has no zoning ordinance
    "Lee": "no_public_roster",
    "Marlboro": "no_public_roster",
    "Saluda": "no_county_bza",  # official boards list has no BZA
    "Union": "no_county_bza",  # no zoning; tax/building BOAs only
    "Williamsburg": "no_county_bza",  # 2026 agenda requested creating a BZA; not established
}
