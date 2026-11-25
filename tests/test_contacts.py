"""Contacts: parsing real-world exports, merging, the panel import API, and never crashing on a missing file."""
import json

from aiohttp.test_utils import TestClient, TestServer

from redtooth.contacts import ContactBook, ContactStore, find_exports, load_all
from redtooth.demo import build_demo
from redtooth.server import ContactsService, make_app

APPLE_VCF = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Ortiz;Daniel;;;\r\nFN:Daniel Ortiz\r\n"
    "item1.TEL;type=CELL;type=pref:(555) 014-2000\r\nNICKNAME:Dan\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:2.1\r\nN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Garc=C3=ADa;Mar=C3=ADa;;;\r\n"
    "TEL;CELL:+34 600 123 456\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nFN:No Phone\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:4.0\r\nFN:Priya\r\n  Shah\r\nTEL;VALUE=uri:tel:+1-555-018-8000\r\nEND:VCARD\r\n"
)
GOOGLE_CSV = (
    "Name,Given Name,Family Name,Nickname,Phone 1 - Type,Phone 1 - Value\n"
    "Daniel Ortiz,Daniel,Ortiz,Dan,Mobile,+1 555-014-2000 ::: (555) 014-2001\nMom,,,,Mobile,555 019 9999\n"
)
OUTLOOK_CSV = "First Name,Middle Name,Last Name,Mobile Phone,Home Phone\nPriya,,Shah,(555) 018-8000,\n"


def test_parses_iphone_android_google_outlook_exports():
    v = ContactBook.parse_vcard(APPLE_VCF)
    assert [(c.name, c.phones) for c in v.contacts] == [
        ("Daniel Ortiz", ["+15550142000"]), ("María García", ["+34600123456"]), ("Priya Shah", ["+15550188000"])]
    assert v.contacts[0].nicknames == ["Dan"]
    g = ContactBook.parse_csv(GOOGLE_CSV)
    assert g.contacts[0].phones == ["+15550142000", "+15550142001"] and g.contacts[1].name == "Mom"
    assert ContactBook.parse_csv(OUTLOOK_CSV).contacts[0].name == "Priya Shah"
    assert ContactBook.from_bytes("c.vcf", APPLE_VCF.encode("utf-16")).contacts[0].name == "Daniel Ortiz"


def test_merge_dedupes_by_phone_and_name():
    book = ContactBook.parse_csv(GOOGLE_CSV)
    assert book.merge(ContactBook.parse_vcard(APPLE_VCF)) == 2  # Daniel already there; María and Priya are new
    assert len(book.contacts) == 4


def test_missing_contacts_file_is_skipped(tmp_path):
    store = ContactStore(tmp_path / "contacts.json")
    book = load_all(store, [str(tmp_path / "nope.vcf")], outlook=False)
    assert book.contacts == []


def test_find_exports_in_downloads(tmp_path):
    (tmp_path / "contacts.vcf").write_text(APPLE_VCF)
    (tmp_path / "budget.csv").write_text("a,b\n1,2\n")
    found = find_exports([tmp_path])
    assert [(f["name"], f["count"]) for f in found] == [("contacts.vcf", 3)]


async def test_panel_import_and_add_apply_without_restart(tmp_path):
    demo = await build_demo(str(tmp_path / "demo"), embedder="hash", radio_delay=0.01)
    agent = demo.agent
    svc = ContactsService(agent, ContactStore(tmp_path / "contacts.json"), outlook=False)
    svc.load()
    assert agent.book.contacts == []
    r = await agent.ask("send Daniel the deck I edited this morning")
    assert "don't have your contacts yet" in r.text

    client = TestClient(TestServer(make_app(agent, contacts=svc)))
    await client.start_server()
    try:
        H = {"X-Redtooth": "1"}
        st = await (await client.get("/api/contacts")).json()
        assert st["count"] == 0 and st["managed"]
        from aiohttp import FormData

        fd = FormData()
        fd.add_field("file", APPLE_VCF.encode(), filename="contacts.vcf", content_type="text/vcard")
        res = await (await client.post("/api/contacts/import", data=fd, headers=H)).json()
        assert res == {"ok": True, "added": 3, "count": 3}
        res = await (await client.post("/api/contacts/add", json={"name": "Mom", "phone": "555 019 9999"}, headers=H)).json()
        assert res["ok"] and res["count"] == 4
        bad = await (await client.post("/api/contacts/add", json={"name": "X", "phone": "12"}, headers=H)).json()
        assert not bad["ok"]
        p = tmp_path / "google.csv"
        p.write_text(GOOGLE_CSV)
        res = await (await client.post("/api/contacts/import", json={"path": str(p)}, headers=H)).json()
        assert res["ok"] and res["added"] == 0 and res["count"] == 4  # same people, one extra number for Daniel
    finally:
        await client.close()
    saved = json.loads((tmp_path / "contacts.json").read_text())
    assert {c["name"] for c in saved} == {"Daniel Ortiz", "María García", "Priya Shah", "Mom"}
    assert "Daniel" in agent.known_names()
