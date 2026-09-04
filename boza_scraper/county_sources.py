# county_sources.py — known BZA / ZBA landing pages for SC counties.
# Used as a first-pass override before heuristic URL discovery.

KNOWN_BOZA_URLS = {
    "Abbeville": [
        "https://abbevillecountysc.com/boards-commissions/",
    ],
    "Aiken": [
        "https://www.aikencountysc.gov/AgendaCenter",
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
        "https://app.matchboard.tech/boards?entityId=7&entityState=SC&name=Beaufort&type=county&boardId=37",
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
    ],
    "Cherokee": [
        "https://cherokeecountysc.gov/building-safety/planning-commission/",
    ],
    "Chester": [
        "https://www.chestercountysc.gov/boards/zoning",
    ],
    "Chesterfield": [
        "https://www.chesterfieldcountysc.com/building-codes",
    ],
    "Clarendon": [
        "https://www.clarendoncountysc.gov/business-and-development/planning-and-zoning/",
    ],
    "Darlington": [
        "https://www.darcosc.com/government/boards_commissions/index.php",
    ],
    "Dillon": [
        "https://dilloncountysc.org/departments/services/building_code_enforcement_planning_zoning.php",
    ],
    "Dorchester": [
        "https://www.dorchestercounty.org/government/planning-development/planning-zoning/board-of-zoning-appeals",
        "https://www.dorchestercountysc.gov/government/planning-development/planning-zoning/board-of-zoning-appeals",
    ],
    "Edgefield": [
        "https://edgefieldcounty.sc.gov/building-permits-applications-and-fees",
    ],
    "Fairfield": [
        "https://www.fairfieldsc.com/departments/county-council/boards-commissions",
    ],
    "Florence": [
        "https://www.florenceco.org/planning/bza/members.php",
        "https://www.florenceco.org/planning/bza/",
        "https://www.florencecountysc.gov/planning/bza",
    ],
    "Georgetown": [
        "https://www.gtcountysc.gov/AgendaCenter",
        "https://www.gtcountysc.gov/177/Planning",
    ],
    "Greenville": [
        "https://www.greenvillecounty.org/apps/countycouncilboard/BoardDetails.aspx?id=76",
        "https://www.greenvillecounty.org/Zoning/BoardOfZoningAppeals.aspx",
    ],
    "Greenwood": [
        "https://www.greenwoodcounty-sc.gov/planning",
    ],
    "Hampton": [
        "http://www.hamptoncountysc.org/316/Boards-Commissions",
    ],
    "Horry": [
        "https://www.horrycountysc.gov/departments/planning-and-zoning/board-of-zoning-appeals/",
        "https://horrycounty.granicus.com/ViewPublisher.php?view_id=3",
    ],
    "Jasper": [
        "https://www.jaspercountysc.gov/government/boards-and-commissions/",
    ],
    "Kershaw": [
        "https://www.kershaw.sc.gov/government/departments-h-q/planning-zoning/board-of-zoning-appeals-planning-commission",
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
    ],
    "Newberry": [
        "https://www.newberrycounty.gov/boards-commissions/board-zoning-appeals",
    ],
    "Oconee": [
        "https://oconeesc.com/council-home/committees-and-commissions/boards-and-commissions/board-of-zoning-appeals",
    ],
    "Pickens": [
        "https://www.co.pickens.sc.us/departments/planning/board_of_appeals_agendas___minutes/index.php",
        "https://www.co.pickens.sc.us/government/county_council/boards___commissions.php",
    ],
    "Richland": [
        "https://www.richlandcountysc.gov/Government/Get-Involved/Boards-and-Committees/Board-of-Zoning-Appeals",
    ],
    "Saluda": [
        "https://saludacounty.sc.gov/departments/legislative-delegation",
    ],
    "Sumter": [
        "https://www.sumtersc.gov/planning/board-zoning-appeals",
    ],
    "Union": [
        "https://gearupunionsc.com/departments/building-maintenance/",
    ],
    "Williamsburg": [
        "https://www.williamsburgcounty.sc.gov/AgendaCenter",
    ],
    "York": [
        "https://www.yorkcountysc.gov/1198/Board-of-Zoning-Appeals",
        "https://yorkcountygov.granicus.com/boards/w/83853d22a8adcc01/boards/42668",
    ],
}
