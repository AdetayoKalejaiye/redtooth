"""Your own headphones: honest connect results, asking which, scanning and pairing new ones, marking as yours."""
import asyncio

import pytest

from redtooth.demo import build_demo
from redtooth.models import Device

pytestmark = pytest.mark.asyncio


async def world(tmp_path):
    d = await build_demo(str(tmp_path), daniel_auto_accept=0.2, embedder="hash", radio_delay=0.01)
    w = d.world
    # Three of your own paired headphones: only the Redmi Buds are switched on.
    for dev_id, name, on in (("bt:redmi", "Redmi Buds 5", True), ("bt:ch720", "WH-CH720N", False), ("bt:azzaro", "AZZARO HEADPHONES", False)):
        w.add(Device(dev_id, name, "headphones", address=f"10:20:30:40:50:{len(w.radios):02X}"), in_range=on)
        w.paired.add(dev_id)
    w.paired.add("bt:airpods")
    w.radios["bt:airpods"].in_range = False
    return d


async def test_paired_but_off_is_not_reported_connected(tmp_path):
    d = await world(tmp_path)
    r = await d.agent.ask("connect to my WH-CH720N")
    assert not r.ok and "didn't answer" in r.text and "not connected" in r.text, r.text
    st = await d.agent.state()
    assert not any(x["connected"] for x in st["audio_devices"]), st["audio_devices"]


async def test_several_of_yours_asks_which(tmp_path):
    d = await world(tmp_path)
    r = await d.agent.ask("connect to my headphones")
    assert r.question and r.question.prompt == "Which headphones?", r.text
    labels = [o.label for o in r.question.options]
    assert "Redmi Buds 5" in labels and labels[-1] == "None of these"
    redmi = next(o for o in r.question.options if o.label == "Redmi Buds 5")
    r2 = await d.agent.answer(r.question.id, redmi.id)
    assert r2.ok and "Redmi Buds 5" in r2.text and d.world.output.name == "Redmi Buds 5", r2.text


async def test_scan_then_pair_new_headphones(tmp_path):
    d = await world(tmp_path)
    found = await d.agent.scan_nearby(1)
    names = [x.name for x in found]
    assert "JBL Flip 6" in names
    st = await d.agent.state()
    assert any(n["name"] == "JBL Flip 6" for n in st["nearby_devices"])
    r = await d.agent.pair_device("bt:jbl")
    assert r.ok and "bt:jbl" in d.world.paired and d.world.output.name == "JBL Flip 6", r.text


async def test_mark_as_yours_with_a_name(tmp_path):
    d = await world(tmp_path)
    pushed = []

    async def publish():
        pushed.append([x.id for x in d.agent.shared_devices])

    d.agent.publish_devices = publish
    await d.agent.set_mine("bt:redmi", True, "Alex's earbuds")
    mine = {x["id"]: x for x in await d.agent.my_devices()}
    assert mine["bt:redmi"]["mine"] and mine["bt:redmi"]["label"] == "Alex's earbuds"
    assert not mine["bt:ch720"]["mine"] and pushed[-1] == ["bt:redmi"]
    await d.agent.set_mine("bt:redmi", False)
    assert pushed[-1] == [] and not (await d.agent.my_devices())[0]["mine"]


async def test_send_to_my_phone_over_bluetooth(tmp_path):
    d = await world(tmp_path)
    d.world.add(Device("bt:s24", "Alex's S24", "phone", address="AA:BB:CC:00:11:22"))
    d.world.paired.add("bt:s24")
    r = await d.agent.ask("send the Q4 board update to my phone")
    assert r.ok and r.transfer_id and "over Bluetooth" in r.text and "S24" in r.text, r.text
    t = d.agent.sender.transfers[r.transfer_id]
    for _ in range(100):
        if t.status in ("saved", "failed", "declined"):
            break
        await asyncio.sleep(0.02)
    assert t.status == "saved" and t.via == "bluetooth" and t.label == "Saved to your S24", (t.status, t.error, t.label)


async def test_phone_is_not_an_audio_output(tmp_path):
    d = await world(tmp_path)
    d.world.add(Device("bt:s24", "Alex's S24", "phone", address="AA:BB:CC:00:11:22"))
    d.world.paired.add("bt:s24")
    r = await d.agent.connect_device("bt:s24")
    assert not r.ok and "is a phone" in r.text and "audio is playing" not in r.text.lower(), r.text


async def test_iphone_gets_a_straight_answer(tmp_path):
    d = await world(tmp_path)
    d.world.add(Device("bt:ip", "Alex's iPhone", "phone", address="AA:BB:CC:00:11:33"))
    d.world.paired.add("bt:ip")
    r = await d.agent.ask("send the Q4 board update to my phone")
    assert not r.ok and "iPhones don't accept Bluetooth file transfers" in r.text, r.text


async def test_audio_claim_only_after_reading_the_output(tmp_path):
    """Connected but the OS kept playing elsewhere -> say so; output unreadable -> claim nothing."""
    from redtooth.bt.base import AudioOutput

    d = await world(tmp_path)
    d.world.radios["bt:redmi"].auto_output = False
    d.agent.bt.set_audio_output = lambda dev: _false()  # Windows refuses to switch
    r = await d.agent.ask("connect to my Redmi Buds 5")
    assert not r.ok and "still playing through" in r.text and "Audio is playing" not in r.text, r.text

    d2 = await world(tmp_path / "b")

    async def unreadable():
        return AudioOutput("an output Redtooth can't read", None, "other", known=False)

    d2.agent.bt.audio_output = unreadable
    r = await d2.agent.ask("connect to my Redmi Buds 5")
    assert "can't confirm" in r.text and "Audio is playing" not in r.text, r.text


async def _false():
    return False
