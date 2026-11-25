"""Your own headphones: honest connect results, asking which, scanning and pairing new ones, marking as yours."""
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
