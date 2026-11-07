"""Seed the database with demo listings (no network required).

Usage: python -m house_finder.seed
"""

from __future__ import annotations

import random

from .db import init_db, session_scope
from .pipeline import ingest
from .schemas import RawListing

DEMO = [
    ("rightmove", "RM1001", "6 bed terraced house", 2400, 6, "Oldfield Park", "BA2 3AB", True, True, "2026-07-01"),
    ("rightmove", "RM1002", "5 bed end of terrace", 2100, 5, "Twerton", "BA2 1DD", True, False, "Now"),
    ("zoopla", "ZP2001", "7 bed student house", 2800, 7, "Combe Down", "BA2 5JN", True, True, "2026-08-15"),
    ("zoopla", "ZP2002", "4 bed maisonette", 1750, 4, "Widcombe", "BA2 4LD", False, False, "2026-09-01"),
    ("onthemarket", "OTM3001", "6 bed house share", 2550, 6, "Bear Flat", "BA2 4QZ", True, True, "2026-07-15"),
    ("openrent", "OR4001", "6 bedroom house", 2300, 6, "Newbridge", "BA1 3LQ", True, False, "Now"),
    ("spareroom", "SR5001", "Room in 6 bed house", 650, 6, "City Centre", "BA1 1AA", True, True, "2026-06-20"),
    ("unihomes", "UH6001", "6 bed student property", 2650, 6, "Lansdown", "BA1 5RT", True, True, "2026-09-05"),
    ("unihomes", "UH6002", "5 bed student house", 2200, 5, "Southdown", "BA2 1DW", True, True, "2026-07-01"),
    ("rightmove", "RM1003", "8 bed HMO", 3200, 8, "Weston", "BA1 4AB", True, False, "2026-10-01"),
]


def main() -> None:
    init_db()
    raws = []
    for source, sid, title, price, beds, area, postcode, furnished, bills, available in DEMO:
        raws.append(
            RawListing(
                source=source,
                source_id=sid,
                url=f"https://example.com/{source}/{sid}",
                title=title,
                price_pcm=price,
                beds=beds,
                baths=random.randint(1, 3),
                property_type="House",
                address=f"{random.randint(1, 99)} {area}, Bath",
                area=area,
                postcode=postcode,
                furnished=furnished,
                bills_included=bills,
                available_from=available,
                tenancy_start=available,
                description=f"{title} in {area}, Bath. {beds} bedrooms. "
                + ("Bills included. " if bills else "")
                + ("Furnished." if furnished else "Unfurnished."),
            )
        )
    with session_scope() as session:
        stats = ingest(session, raws)
    print(f"Seeded {stats.found} listings ({stats.new} new).")


if __name__ == "__main__":
    main()
