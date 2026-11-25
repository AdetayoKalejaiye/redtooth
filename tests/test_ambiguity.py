"""Usage signals, the cross-encoder re-ranker (through ONNX Runtime with a tiny stand-in model), and
clarifying questions that say what differs."""
import os
import struct
import time

import numpy as np
import pytest

from redtooth.clarify import ambiguous, distinguish
from redtooth.files.embed import HashEmbedder
from redtooth.files.index import FileIndex
from redtooth.files.usage import Usage, lnk_target, norm_path
from redtooth.intent import parse
from redtooth.models import FileHit

DAY = 86400


def _lnk(path: str) -> bytes:
    """A minimal Windows shortcut with LinkInfo › LocalBasePath (ANSI + Unicode), as Explorer writes them."""
    header = struct.pack("<I16sI", 0x4C, b"\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46", 0x02 | 0x80) + b"\x00" * (0x4C - 24)
    ansi = path.encode("cp1252", "replace") + b"\x00"
    uni = path.encode("utf-16-le") + b"\x00\x00"
    hsize = 0x24
    base_off = hsize
    suffix_off = base_off + len(ansi)
    base_u = suffix_off + 1
    suffix_u = base_u + len(uni)
    body = ansi + b"\x00" + uni + b"\x00\x00"
    info = struct.pack("<9I", hsize + len(body), hsize, 0x01, 0, base_off, 0, suffix_off, base_u, suffix_u) + body
    return header + info


def test_windows_shortcut_target():
    assert lnk_target(_lnk(r"C:\Users\rober\Documents\School\HIST 201 Term Paper.docx")) == r"C:\Users\rober\Documents\School\HIST 201 Term Paper.docx"
    assert lnk_target(b"not a shortcut") is None


def test_usage_signals_decay_and_picks(tmp_path):
    u = Usage(tmp_path / "u.db")
    u.recent_source = lambda: {norm_path("/docs/opened.docx"): time.time() - 1 * DAY}
    u.record("/docs/sent.docx", "sent")
    u.record_pick("term paper", "/docs/final.docx")
    sig = u.signals(["/docs/sent.docx", "/docs/opened.docx", "/docs/final.docx", "/docs/never.docx"], "Term Papers")
    assert sig["/docs/never.docx"][:2] == (0.0, 0.0)
    assert sig["/docs/opened.docx"][0] > 0 and "opened recently" in sig["/docs/opened.docx"][2]
    assert sig["/docs/sent.docx"][0] > sig["/docs/opened.docx"][0]  # sending counts more than opening
    assert sig["/docs/final.docx"][1] > 0 and sig["/docs/final.docx"][2][0].startswith("you picked it")


def _index(tmp_path, docs):
    ix = FileIndex(tmp_path / "ix.db", HashEmbedder())
    for name, text, age in docs:
        p = tmp_path / name
        p.write_text(text)
        m = time.time() - age * DAY
        os.utime(p, (m, m))
        ch = [("Page 1", text)]
        ix.write_file(str(p), len(text), m, f"{len(text)}:{m}", ch, ix.embed_many([(name, ch)])[0])
    ix.commit()
    ix.usage = Usage(":memory:")
    ix.usage.recent_source = dict
    return ix


def test_recently_opened_breaks_a_tie(tmp_path):
    ix = _index(tmp_path, [("Term paper A.docx", "railroads", 30), ("Term paper B.docx", "railroads", 30)])
    fq = parse("send my term paper to Dan").file
    assert ix.search(fq)[0].name in ("Term paper A.docx", "Term paper B.docx")
    ix.usage.recent_source = lambda: {norm_path(str(tmp_path / "Term paper B.docx")): time.time() - 3600}
    ix.usage._recent = (0.0, {})
    hits = ix.search(fq)
    assert hits[0].name == "Term paper B.docx" and "opened recently" in hits[0].reasons


# ---------------------------------------------------------------- the re-ranker, through ONNX Runtime
@pytest.fixture
def fake_cross_encoder(tmp_path):
    """A tiny ONNX graph with the real model's inputs/outputs (input_ids, attention_mask -> logits[B,1]).
    Each token carries a weight; the logit is the sum over the pair. Good enough to check the plumbing:
    pair tokenization, padding, masking, sigmoid, and how the scores are used."""
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper, numpy_helper
    from tokenizers import Tokenizer, models, pre_tokenizers

    words = ["<pad>", "<unk>", "term", "paper", "terms", "termination", "graph", "mid", "report", "railroad", "final", "draft", "docx", "pdf"]
    weight = {"term": 1.5, "paper": 1.5, "railroad": 0.5, "terms": -1.0, "termination": -1.0, "graph": -2.0, "mid": -1.0, "report": -1.0}
    d = tmp_path / "ce"
    d.mkdir()
    tok = Tokenizer(models.WordLevel({w: i for i, w in enumerate(words)}, unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(d / "tokenizer.json"))
    E = np.array([[weight.get(w, 0.0)] for w in words], np.float32)
    nodes = [
        helper.make_node("Gather", ["E", "input_ids"], ["g"]),
        helper.make_node("Cast", ["attention_mask"], ["mf"], to=TensorProto.FLOAT),
        helper.make_node("Unsqueeze", ["mf", "ax2"], ["m3"]),
        helper.make_node("Mul", ["g", "m3"], ["gm"]),
        helper.make_node("ReduceSum", ["gm", "ax1"], ["logits"], keepdims=0),
    ]
    g = helper.make_graph(nodes, "ce", [helper.make_tensor_value_info("input_ids", TensorProto.INT64, [None, None]),
                                        helper.make_tensor_value_info("attention_mask", TensorProto.INT64, [None, None])],
                          [helper.make_tensor_value_info("logits", TensorProto.FLOAT, [None, 1])],
                          [numpy_helper.from_array(E, "E"), numpy_helper.from_array(np.array([2], np.int64), "ax2"),
                           numpy_helper.from_array(np.array([1], np.int64), "ax1")])
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)])
    m.ir_version = 8
    onnx.save(m, str(d / "model.onnx"))
    from redtooth.files.rerank import CrossEncoder, RerankerHolder

    h = RerankerHolder()
    h.model = CrossEncoder(d)
    return h


def test_reranker_puts_the_real_match_first_and_is_confident(tmp_path, fake_cross_encoder):
    ix = _index(tmp_path, [
        ("mid_Term_report.docx", "mid report", 200),
        ("DocuSign_d9a0.pdf", "terms termination paper", 90),
        ("graphpaper.pdf", "graph", 60),
        ("HIST 201 Term Paper - final.docx", "railroad", 3),
    ])
    ix.reranker = fake_cross_encoder
    hits = ix.search(parse("send my term paper to my phone").file)
    assert hits[0].name == "HIST 201 Term Paper - final.docx", [h.name for h in hits]
    assert hits[0].confidence is not None and hits[0].confidence > 0.9
    assert not ambiguous(hits, True)  # confident and well ahead: no question


# ---------------------------------------------------------------- clarifying questions
def _hit(name, days, path=None, conf=None, score=1.0, reasons=()):
    return FileHit(1, path or f"/Users/r/Documents/{name}", name, "document", os.path.splitext(name)[1], 1, time.time() - days * DAY, score,
                   confidence=conf, reasons=list(reasons))


def test_question_names_the_versions():
    q, phr = distinguish([_hit("HIST 201 Term Paper - final.docx", 2), _hit("HIST 201 Term Paper - draft.docx", 50)])
    assert q.startswith("Which one: the final (edited") and "or the draft (" in q, q


def test_question_names_the_format():
    q, _ = distinguish([_hit("Term Paper.docx", 2), _hit("Term Paper.pdf", 2)])
    assert q == "Which one: the Word doc or the PDF?", q


def test_question_names_what_only_each_file_has():
    q, _ = distinguish([_hit("Retention Plan.pptx", 3), _hit("Pricing Change Proposal.pptx", 9)])
    assert "Retention Plan" in q and "Pricing Change Proposal" in q, q


def test_question_by_folder_for_the_same_file_in_two_places():
    q, _ = distinguish([_hit("Budget.xlsx", 5, "/Users/r/Documents/Work/Budget.xlsx"), _hit("Budget.xlsx", 5, "/Users/r/Downloads/Budget.xlsx")])
    assert "Work" in q and "Downloads" in q, q


def test_when_to_ask():
    assert not ambiguous([_hit("a.docx", 1, conf=0.95, score=1.0), _hit("b.docx", 1, conf=0.2, score=0.4)], True)
    assert ambiguous([_hit("a.docx", 1, conf=0.8, score=1.0), _hit("b.docx", 1, conf=0.75, score=0.93)], True)
    assert not ambiguous([_hit("a.docx", 1, conf=0.8, score=1.0, reasons=["you picked it for this before"]), _hit("b.docx", 1, conf=0.8, score=0.99)], True)
    assert ambiguous([_hit("a.docx", 1), _hit("b.docx", 1)], False)  # type/time only
