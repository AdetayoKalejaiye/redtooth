import json
import subprocess
import sys
from pathlib import Path

from redtooth.contacts import ContactBook, ContactStore
from redtooth.phones import BY_ISO, country_for_phone, normalize, parse_country, set_country
from redtooth.util import phone_hash

ROOT = Path(__file__).resolve().parents[1]


def test_country_rules():
    assert normalize("(555) 014-2000", "US") == "+15550142000"
    assert normalize("020 7946 0958", "GB") == "+442079460958"
    assert normalize("06 1234 5678", "IT") == "+390612345678"  # Italy keeps its leading 0
    assert normalize("8 (912) 345-67-89", "RU") == "+79123456789"
    assert normalize("0011 44 20 7946 0958", "AU") == "+442079460958"
    assert normalize("+1 555 014 2000", "GB") == "+15550142000"  # "+" always wins


def test_same_person_same_hash_from_two_countries():
    """Alex in the US and Priya in the UK both save Daniel's UK mobile their own way."""
    us_way = normalize("011 44 7700 900123", "US")
    uk_way = normalize("07700 900123", "GB")
    assert us_way == uk_way and phone_hash(us_way) == phone_hash(uk_way)


def test_country_lookup_and_parsing():
    assert country_for_phone("+442079460958") == "GB"
    assert country_for_phone("+15550142000", prefer="CA") == "CA"
    assert parse_country("uk") == "GB" and parse_country("+49") == "DE" and parse_country("Germany") == "DE"
    assert parse_country("nowhere") is None


def test_store_rereads_numbers_when_country_changes(tmp_path):
    try:
        set_country("US")
        book = ContactBook.parse_vcard("BEGIN:VCARD\r\nFN:Amelia\r\nTEL:020 7946 0958\r\nEND:VCARD\r\n")
        assert book.contacts[0].phones == ["+102079460958"]  # read as American: wrong for a London number
        store = ContactStore(tmp_path / "c.json")
        store.save(book)
        set_country("GB")
        assert store.load().contacts[0].phones == ["+442079460958"]
    finally:
        set_country("US")


def test_fixtures_cover_every_country():
    out = subprocess.run([sys.executable, str(ROOT / "tools/make_phone_fixtures.py")], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    assert len(d["countries"]) == len(BY_ISO)
    assert all(c["e164"] == normalize(c["raw"], c["country"]) for c in d["cases"])
