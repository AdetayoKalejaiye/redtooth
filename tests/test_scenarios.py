import asyncio
import hashlib
from pathlib import Path

import pytest

from redtooth.demo import XM5_ADDR, build_demo
from redtooth.intent import parse

pytestmark = pytest.mark.asyncio


async def world(tmp_path, scenario="registered"):
    demo = await build_demo(str(tmp_path), daniel_auto_accept=0.2, embedder="hash", radio_delay=0.02)
    if scenario != "registered":
        await demo.set_scenario(scenario)
    return demo


async def wait_saved(agent, tid, timeout=20):
    for _ in range(int(timeout / 0.05)):
        t = agent.sender.transfers[tid]
        if t.status in ("saved", "failed", "declined", "cancelled"):
            return t
        await asyncio.sleep(0.05)
    raise AssertionError("transfer did not finish")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ---------------------------------------------------------------- connect
async def test_connect_registered_headphones(tmp_path):
    d = await world(tmp_path)
    r = await d.agent.ask("Hey, connect to my friend Daniel's headphones")
    assert r.ok and r.status == "Audio output", r.text
    assert d.world.output.address == XM5_ADDR  # verified as the active output
    assert any(x.id == "bt:xm5" for x in d.agent.store.remembered("+15550142000"))
    # Next time: remembered + paired -> no scan, no questions.
    await d.agent.bt.disconnect(r.device)
    r2 = await d.agent.ask("connect to Daniel's headphones")
    assert r2.ok and r2.question is None and d.world.output.address == XM5_ADDR


async def test_connect_without_app_asks_specific_question(tmp_path):
    d = await world(tmp_path, "no_app")
    r = await d.agent.ask("connect to my friend Daniel's headphones")
    assert r.question and r.question.prompt == "Are Daniel's the Sony XM5s?"
    r2 = await d.agent.ask("yes")
    assert r2.ok and d.world.output.address == XM5_ADDR
    assert "bt:xm5" in d.world.paired  # paired through the OS
    rem = d.agent.store.remembered("+15550142000")
    assert rem and rem[0].id == "bt:xm5"  # remembered for next time


async def test_connect_not_in_pairing_mode_nudges_and_keeps_scanning(tmp_path):
    d = await world(tmp_path, "not_pairing")
    q = d.agent.bus.subscribe()
    r = await d.agent.ask("connect to Daniel's headphones")
    assert "pairing mode" in r.text and "notification" in r.text
    kinds = []
    d.set_pairing_mode(True)  # Daniel holds the power button
    for _ in range(200):
        ev = await asyncio.wait_for(q.get(), 10)
        kinds.append(ev["kind"])
        if ev["kind"] == "reply":
            assert ev["data"]["status"] == "Audio output"
            break
    assert "peer_nudge" in kinds
    assert d.world.output.address == XM5_ADDR


async def test_connect_my_own_airpods(tmp_path):
    d = await world(tmp_path)
    r = await d.agent.ask("connect my airpods")
    assert r.ok and "AirPods" in r.text


# ---------------------------------------------------------------- files
async def test_send_deck_edited_this_morning_asks_which(tmp_path):
    d = await world(tmp_path)
    r = await d.agent.ask("Send Daniel the deck I edited this morning.")
    assert r.question and r.question.kind == "choose_file"
    names = {o.label for o in r.question.options}
    assert names == {"Q4 Board Update", "Sales Kickoff 2027"}
    assert r.person.name == "Daniel Ortiz"  # not Daniel Kim: friend, nearby, uses the app
    # Routes are being checked while the user picks.
    assert d.agent.router.inflight or d.agent.router.cache
    r2 = await d.agent.ask("the Q4 board one")
    assert r2.transfer_id and "Q4 Board Update.pptx" in r2.text and "iPhone" in r2.text
    t = await wait_saved(d.agent, r2.transfer_id)
    assert t.status == "saved"
    saved = tmp_path / "Daniel" / "Daniel's iPhone" / "Q4 Board Update.pptx"
    assert sha(saved) == sha(tmp_path / "Documents" / "Q4 Board Update.pptx")
    for k in ("accept_to_first_byte_ms", "first_byte_to_saved_ms"):
        assert k in t.timings


async def test_follow_up_to_his_laptop_instead(tmp_path):
    d = await world(tmp_path)
    await d.agent.ask("Send Daniel the deck I edited this morning")
    r = await d.agent.ask("2")
    first = r.files[0].name
    r2 = await d.agent.ask("Send it to his laptop instead.")
    assert r2.ok and "MacBook Air" in r2.text and first in r2.text and r2.text.endswith("instead.")
    t = await wait_saved(d.agent, r2.transfer_id)
    assert t.status == "saved" and t.device.kind == "laptop"


async def test_find_deck_about_customer_churn(tmp_path):
    d = await world(tmp_path)
    r = await d.agent.ask("Find the deck about customer churn.")
    assert r.files and r.files[0].name == "Retention Plan.pptx", [f.name for f in r.files]
    assert d.agent.ctx.file.name == "Retention Plan.pptx"
    r2 = await d.agent.ask("send it to Daniel")
    assert "Retention Plan.pptx" in r2.text


async def test_project_code_exact_match(tmp_path):
    d = await world(tmp_path)
    r = await d.agent.ask("send the PRJ-2041 checklist to Priya")
    assert r.transfer_id and "PRJ-2041 Launch Checklist.docx" in r.text
    assert (await wait_saved(d.agent, r.transfer_id)).status == "saved"


async def test_interrupted_transfer_resumes(tmp_path):
    d = await world(tmp_path)
    d.daniel_auto_accept = 0.05
    big = tmp_path / "Downloads" / "Product walkthrough.mp4"
    sel = await d.agent.select_file(str(big))
    assert sel.ok
    r = await d.agent.ask("send it to Daniel")
    assert r.transfer_id
    for _ in range(400):
        t = d.agent.sender.transfers[r.transfer_id]
        if t.status == "sending" and t.sent > 8 << 20:
            break
        await asyncio.sleep(0.005)
    for task in asyncio.all_tasks():
        if "Receiver._handle" in repr(task.get_coro()):
            task.cancel()
    t = await wait_saved(d.agent, r.transfer_id, timeout=40)
    assert t.status == "saved" and t.resumes >= 1
    assert sha(tmp_path / "Daniel" / "Daniel's iPhone" / big.name) == sha(big)


async def test_decline_is_reported(tmp_path):
    d = await world(tmp_path)
    d.daniel_auto_accept = 0
    q = d.agent.bus.subscribe()
    r = await d.agent.ask("send Daniel the retention plan")
    while True:
        ev = await asyncio.wait_for(q.get(), 5)
        if ev["kind"] == "peer_incoming" and ev["data"]["status"] == "waiting":
            for _ in range(100):
                if d.agent.sender.transfers[r.transfer_id].status == "waiting":
                    break
                await asyncio.sleep(0.01)
            assert d.agent.sender.transfers[r.transfer_id].label == "Waiting for Daniel"
            d.daniel_broker.resolve(ev["data"]["id"], False)
            break
    t = await wait_saved(d.agent, r.transfer_id)
    assert t.status == "declined"


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    "text,action,person,kind",
    [
        ("Hey, connect to my friend Daniel's headphones", "connect", "daniel", "headphones"),
        ("Send Daniel the deck I edited this morning.", "send", "daniel", None),
        ("Find the deck about customer churn.", "find", None, None),
        ("Send it to his laptop instead.", "retarget", "his", "laptop"),
        ("share the budget spreadsheet with daniel", "send", "daniel", None),
        ("play music on daniel's speaker", "connect", "daniel", "speaker"),
    ],
)
async def test_parse(text, action, person, kind):
    i = parse(text, ["daniel", "daniel ortiz", "priya"])
    assert (i.action, i.person, i.device_kind) == (action, person, kind)
