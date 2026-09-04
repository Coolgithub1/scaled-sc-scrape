#!/usr/bin/env python3
"""Generate unique cold emails for each BZA buyer. No stop/opt-out line."""

import csv
import json
from pathlib import Path

GREETINGS = [
    "Hi,",
    "Hello,",
    "Hi there,",
    "Good afternoon,",
    "Hello —",
    "Hi —",
]

INTROS = [
    "My name is Adam Natsheh. I'm a data scientist and AI/ML researcher.",
    "I'm Adam Natsheh, a data scientist and AI/ML researcher.",
    "This is Adam Natsheh. I work as a data scientist and AI/ML researcher.",
    "Adam Natsheh here. I'm a data scientist and AI/ML researcher.",
    "I wanted to introduce myself: Adam Natsheh, data scientist and AI/ML researcher.",
]

SC_LINES = [
    "I assembled a structured directory of South Carolina county Board of Zoning Appeals members, including sitting and historic names.",
    "I recently finished a clean roster of South Carolina county zoning appeals board members, both current and former.",
    "I've been compiling who sits on South Carolina county Boards of Zoning Appeals, plus historic members where minutes exist.",
    "I have a structured SC county BZA/BOZA member file covering sitting members and historic ones.",
    "Over the last stretch I built a South Carolina county zoning-appeals board member index, current and historical.",
]

NATION_LINES = [
    "I'm now expanding that to every county in the United States.",
    "The next build is a nationwide corpus: every US county, sitting and historic members.",
    "I'm scaling the same pipeline to all US counties.",
    "A full US county corpus is what I'm producing next.",
    "After SC, the same method is going nationwide, county by county.",
]

WHY = {
    "Zoning PropTech": [
        "You already turn zoning codes into data. The missing layer is who actually sits on the appeals board.",
        "Your product already covers zoning text. Board-member identity is the decision-maker layer next to that.",
        "This would sit beside your zoning data as a simple official-roster add-on.",
    ],
    "CRE data": [
        "This is a civic overlay your SC research users do not get from parcel or comps data alone.",
        "For SC diligence, knowing the appeals board is a useful join on top of property records.",
        "It's a small official-data layer that makes SC market research more complete.",
    ],
    "CRE marketplace": [
        "You already added zoning to listings. Who sits on the county appeals board is the next local layer.",
        "For SC listings, board composition is a practical due-diligence add-on.",
    ],
    "PropTech data": [
        "This could append to a property or skip-trace feed as a local-official field.",
        "It's a clean civic attribute you could attach to SC records.",
    ],
    "Location data": [
        "This belongs next to address and boundary products as a public-official layer.",
        "A BZA member table is an easy civic add beside your location data.",
    ],
    "Property data": [
        "Board membership is a natural add next to tax and neighborhood layers.",
        "Civic board rosters fit the same stack as your other property attributes.",
    ],
    "GIS": [
        "This is a simple feature class: county, name, sitting vs historic.",
        "Planners already live in ArcGIS. A BZA member layer is an easy drop-in.",
    ],
    "Location intelligence": [
        "Planning models get better when they know who hears the variance.",
        "Local appeals-board makeup is useful context beside land-use models.",
    ],
    "Geospatial": [
        "Before a site gets flown or modeled, teams still ask who can grant the variance.",
        "Officials data is a useful companion to imagery in your government and CRE work.",
    ],
    "GovTech": [
        "You already handle boards, agendas, or permitting. This is structured BZA membership.",
        "A cleaned sitting-and-historic roster would slot into a boards-and-commissions product.",
    ],
    "Policy intelligence": [
        "County zoning-board members are local influentials your users already try to track.",
        "This extends official coverage below the usual legislative layer.",
    ],
    "Civic data": [
        "It's a cleaned public-official index, ready to ingest.",
        "This is sitting and historic BZA members, starting with South Carolina.",
    ],
    "SC land-use law": [
        "You appear before these boards. I have a current-plus-historic roster, county by county.",
        "This is meant as a practical brief before a variance or appeal hearing in SC.",
        "I thought your land-use group might want a clean county-by-county member list.",
    ],
    "Land-use law": [
        "For Carolinas entitlement work, this is who sits on the SC county boards.",
        "It's a diligence input: sitting vs historic members, by county.",
    ],
    "Planning engineering": [
        "This is who votes on the variance before you take a client in.",
        "Entitlement teams usually brief this by hand. I have it structured.",
        "Useful as a one-page county brief before a BZA hearing.",
    ],
    "Planning geospatial": [
        "Could live as a civic GIS layer on top of your SC planning work.",
        "A BZA member table is a clean geospatial join on county.",
    ],
    "SC homebuilder": [
        "County board makeup affects variances and community approvals across SC.",
        "Before you open a community, this is who is actually on the county board.",
    ],
    "Homebuilder": [
        "Variance-board turnover is an entitlement signal in the Carolinas.",
        "Sitting vs historic members help when you are opening SC communities.",
    ],
    "Multifamily developer": [
        "For new SC communities, county BZA names are local intelligence worth having.",
        "Charleston-to-Upstate, this is who hears the variance.",
    ],
    "Developer": [
        "Entitlements still go through county planning and BZA. This is the roster.",
        "A clean board list is useful before a special exception or variance.",
    ],
    "Retail developer": [
        "Tenant uses often need a special exception. This is the board that hears it.",
        "For SC retail sites, BZA composition is practical local intel.",
    ],
    "National developer": [
        "Southeast entitlement teams usually want local decision-maker context.",
        "This is the SC starting point for a US county board corpus.",
    ],
    "Site selection": [
        "Prospects ask how local approvals work. This is the BZA roster layer.",
        "Useful in a site brief: who sits on the county appeals board.",
    ],
    "CRE brokerage": [
        "Brokers get asked who can grant a variance. This answers that for SC counties.",
        "A local board roster is a simple differentiator on SC land deals.",
    ],
    "Title insurance": [
        "Appeals-board identity is a leftover land-use risk signal in diligence.",
        "Civic official data can sit beside other closing research.",
    ],
    "PropTech": [
        "Development modules get better with local-board context.",
        "County approval bodies matter when a new community is entitled.",
    ],
    "CRE analytics": [
        "Local approval-body data is a development-risk feature.",
        "Board composition is a useful SC risk attribute.",
    ],
    "Economic development": [
        "Prospects ask how local boards work. This is the BZA roster, starting with SC.",
        "A county member list is a ready research tool for site visits.",
    ],
    "SC trade group": [
        "Your members deal with variances all the time. This is the county roster.",
        "A statewide BZA member list is something builders actually use.",
    ],
    "Industry association": [
        "Members keep asking who decides local housing approvals. This is that layer for SC.",
        "A BZA directory is a research product your members could use.",
    ],
    "Officials data": [
        "County BZA members are a local-official expansion set beside your usual coverage.",
        "This is sitting and historic zoning-board members, starting in South Carolina.",
    ],
    "Planning association": [
        "Planners advise these boards. A clean member index is useful research.",
        "I thought APA members might want a structured BZA roster to start with.",
    ],
}

CTAS = [
    "If this interests you, let me know and I can send a sample.",
    "If this is useful, let me know and I'll send a sample.",
    "If this interests you, I can send a sample. Just let me know.",
    "Let me know if this interests you and I can send a sample.",
    "If you'd like to see it, let me know and I'll send a sample.",
    "If this is relevant, let me know and I can send a sample over.",
    "Happy to send a sample if this interests you. Just say the word.",
    "If this interests you at all, let me know and I'll send a sample.",
]

SIGS = [
    "Adam Natsheh\nData Scientist and AI/ML Researcher",
    "Thanks,\nAdam Natsheh\nData Scientist and AI/ML Researcher",
    "Best,\nAdam Natsheh\nData Scientist and AI/ML Researcher",
    "Regards,\nAdam Natsheh\nData Scientist and AI/ML Researcher",
    "Adam Natsheh\nData scientist and AI/ML researcher",
]

SUBJECTS = [
    "SC zoning appeals board members",
    "county BZA member directory",
    "zoning board roster, South Carolina",
    "sitting and historic BZA members",
    "who sits on the county appeals board",
    "South Carolina BOZA members",
    "a BZA member file you might want",
    "county zoning board names",
    "SC board of zoning appeals roster",
    "historic and sitting zoning board members",
    "US county BZA corpus, starting with SC",
    "zoning appeals decision-makers in SC",
    "structured BZA member data",
    "county-level zoning board directory",
    "SC variance board members",
]


def why_line(segment: str, i: int) -> str:
    options = WHY.get(segment) or WHY["CRE data"]
    return options[i % len(options)]


def build(row: dict, i: int) -> dict:
    company = row["company"]
    segment = row["segment"]
    greet = GREETINGS[i % len(GREETINGS)]
    intro = INTROS[i % len(INTROS)]
    sc = SC_LINES[i % len(SC_LINES)]
    nation = NATION_LINES[i % len(NATION_LINES)]
    why = why_line(segment, i)
    cta = CTAS[i % len(CTAS)]
    sig = SIGS[i % len(SIGS)]
    first = company.split()[0].replace(",", "")
    subject_bank = [
        f"{first}: SC zoning appeals board members",
        f"county BZA roster that may fit {first}",
        f"sitting and historic zoning board names ({first})",
        f"quick note for {first} on county BZA data",
        f"{first} / who sits on the SC appeals board",
        f"South Carolina BOZA members, note for {first}",
        f"{first} and a US county BZA corpus",
        f"structured zoning-board members for {first}",
    ]
    subject = subject_bank[i % len(subject_bank)]

    # Three body layouts so Gmail does not see one fingerprint.
    layout = i % 3
    if layout == 0:
        body = f"{greet}\n\n{intro}\n\n{sc} {nation}\n\n{why}\n\n{cta}\n\n{sig}"
    elif layout == 1:
        body = f"{greet}\n\n{intro} {sc}\n\n{nation}\n\n{why} {cta}\n\n{sig}"
    else:
        body = f"{greet}\n\n{intro}\n\n{why}\n\n{sc} {nation}\n\n{cta}\n\n{sig}"

    return {
        "rank": int(row["rank"]),
        "company": company,
        "to": row["email"],
        "subject": subject,
        "body": body,
        "segment": segment,
    }


def main() -> None:
    src = Path("/workspace/buyer_prospects/sc_bza_buyer_list.csv")
    rows = list(csv.DictReader(src.open()))
    emails = [build(r, i) for i, r in enumerate(rows)]
    subjects = [e["subject"] for e in emails]
    bodies = [e["body"] for e in emails]
    assert len(emails) == 100
    assert len(set(e["to"] for e in emails)) == 100
    assert len(set(bodies)) == 100
    out = Path("/workspace/buyer_prospects/outreach_queue.json")
    out.write_text(json.dumps(emails, indent=2))
    print(f"wrote {len(emails)} emails, unique subjects={len(set(subjects))}, unique bodies={len(set(bodies))}")


if __name__ == "__main__":
    main()
