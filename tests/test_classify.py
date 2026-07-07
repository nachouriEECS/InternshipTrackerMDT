"""Tests for is_non_us / categorize in tracker.py.

Run with ``pytest tests/`` or ``python tests/test_classify.py``.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tracker import categorize, is_non_us


def test_clearly_foreign_locations_dropped():
    foreign = [
        "Bengaluru, Karnātaka, India",
        "Sydney, Australia",
        "Australia-Fortitude Valley",
        "Canada-Mississauga (Indal)",
        "Ottawa, Ontario",
        "Mirabel, CAN",
        "Vilnius, LT",
        "GBR - London, UK",
        "Bristol, Gloucestershire, UK",
        "Paris, France",
        "Hamburg Area",
        "Immenstaad am Bodensee",
        "Manching",
        "Stade",
        "Getafe Area",
        "Rivalta di Torino, Torino, Italy",
        "Prague, Praha, Czechia",
        "Istanbul, Türkiye",
        "Queretaro, Querétaro, Mexico",
        "Suzhou, Jiangsu, China",
        "Beijing Area",
        "Tianjin Area",
        "Guangzhou (TTMT)",
        "Hong Kong",
        "KOR - Seoul, South Korea",
        "Singapore, Central Singapore, Singapore",
        "Subang, Selangor, Malaysia",
        "Abu Dhabi",
        "Dubai",
        "Dubai, United Arab Emirates",
        "Al Muntazah Signal, C-Ring Road,Doha",
        "Riyadh, Riyadh",
        "Casablanca, Morocco",
        # Slug-derived title used as location fallback when location is empty
        "Civil Engineering Intern Utilities Wet Utilities Drainage "
        "Al Muntazah Signal C Ring Road Doha",
    ]
    for loc in foreign:
        assert is_non_us(loc), f"should be non-US: {loc!r}"


def test_clearly_us_locations_kept():
    us = [
        "Arlington, Virginia, United States",
        "Albany,Oregon,United States",
        "Windsor, Maryland, United States of America",
        "Barberton, OH, US",
        "Atlanta, GA",
        "Syracuse-W, NY",
        "Washington, D.C.",
        "Washington, DC",
        "US-CA-Brea (Nuclear)",
        "6314 Remote/Teleworker US",
        "Boston, Massachusetts",
        "San Juan, Puerto Rico",
        # Ontario the Californian city, not the Canadian province
        "Ontario, California",
        "Ontario, CA",
        # US towns that share a name with foreign cities
        "Hamburg, NY",
        "Vienna, VA",
        "Rome, NY",
        "Paris, TX",
    ]
    for loc in us:
        assert not is_non_us(loc), f"should be US: {loc!r}"


def test_ambiguous_locations_kept():
    ambiguous = [
        "",
        "2 Locations",
        "5 Locations",
        "Remote",
        "California",
        "Texas",
        "Flexible - Any SpaceX Site",
    ]
    for loc in ambiguous:
        assert not is_non_us(loc), f"ambiguous should be kept: {loc!r}"


def test_software_wins_over_engineering():
    software = [
        "Software Engineering Intern (HIL) - Fall 2026",
        "2026 Fall Co Op Software Engineer Crewed Land Slidell La",
        "Flight Software Engineering Intern - Fall 2026",
        "Privacy and Civil Liberties Software Engineer, Internship",
        "Front-End Developer Intern",
        "Python Developer Intern",
        "Generative Ai Engineering Intern Graduate Undefined Undefined",
        "Intern (d/f/m) in AI driven software development",
        "Machine Learning Intern",
        "Cybersecurity Intern",
        "Data Analyst Intern",
        "Aircraft Engines Prognostics and Health Management Data Analyst "
        "Engineer Intern",
        "IT Intern",
        "Stage Automne 2026 Technologie De Linformation 2026 Fall Internship "
        "Information Technology Mirabel Qc",
        "Computer Science Intern Summer 2026",
        "Firmware Intern",
    ]
    for title in software:
        assert categorize(title, ()) == "software", title


def test_engineering_titles():
    engineering = [
        "Mechanical Engineering Intern",
        "2027 Electrical Engineer Intern",
        "Avionics Electrical Engineering Intern - Fall 2026",
        "Civil Engineering Intern - Highway",
        "Chemical Engineering Intern Summer 2026",
        "Chemistry Intern",
        "Manufacturing Intern Nozzle",
        "Metallurgy Co Op Muskegon Mi",
        "Lynn Welder Trainee Co Op",
        "Lynn CNC Trainee Co Op",
        "Gear Cut Grind Machinist Intern Dsc 1St Shift",
        "GNC Engineering Intern (Controls) - Fall 2026",
        "Propulsion Engineer Intern - Fall 2026",
        "Intern Thermal Analysis Fall Intern Huntsville",
        "Structural Engineering Intern",
        "Quality Intern 2026",
        "Aerospace Intern Cosmos Contract Temporary Internship Houston",
        "Intern, Systems Engineering",
        "Engineering Intern",
        "Fall 2026 Engineering Internship/Co-op",
        # No keywords at all -> defaults to engineering
        "Intern",
        "Internship",
        "Co-Op (Fall Term)",
        "Qualified intern",
        "FLIGHT DECK Intern",
    ]
    for title in engineering:
        assert categorize(title, ()) == "engineering", title


def test_disciplines_inform_category():
    # Title alone is uninformative; the discipline tag decides.
    assert (
        categorize("Naval Architect Co-op - Winter 2027",
                   ("Maritime & Maneuver Dominance : Heavy Metal - "
                    "Engineering & Operations",))
        == "engineering"
    )
    assert categorize("2027 Intern", ("Software",)) == "software"


def test_non_engineering_keywords():
    non_eng = [
        "Business Intern",
        "Finance Intern",
        "HR Admin Intern",
        "2026 Fall Co Op Hr Piney Flats Tn",
        "2026 Fall Co Op Resource Analyst Piney Flats Tn",
        "Corporate Legal Intern Hybrid",
        "Pricing Analyst Intern",
        "Community Outreach Intern Summer 2026 Newark",
        "Purchasing & Supply Chain Intern (6-month assignment)",
        "EHS Intern",
        "Deployment Strategist, Internship",
        "Facilities Intern",
        "Maintenance Intern",
        "Flight Test Operations Pilot Intern - Fall 2026",
        "Human Resources Intern - One Year Term (Year-Round)",
        "Contracts Administration Pipeline Intern And Entry Level Reston",
        "Customer Compliance Intern Summer Fall 2026",
        "Job Title Health Policy Analysis Graduate Intern McLean or Baltimore",
    ]
    for title in non_eng:
        assert categorize(title, ()) == "non-engineering", title


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
