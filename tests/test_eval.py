from redtooth.files.embed import HashEmbedder
from redtooth.files.index import FileIndex
from redtooth.search_eval.corpus import CORPUS, build_corpus
from redtooth.search_eval.run import evaluate, load_queries, summarize


def test_bundled_eval_runs_and_keyword_categories_hold(tmp_path):
    root = build_corpus(tmp_path / "corpus")
    assert len(list(root.iterdir())) == len(CORPUS)
    ix = FileIndex(tmp_path / "eval.db", HashEmbedder())
    ix.scan([root])
    queries = load_queries()
    assert len(queries) >= 50 and all(set(q) >= {"q", "expect", "category"} for q in queries)
    names = {n for n, _, _ in CORPUS}
    assert all(e in names for q in queries for e in q["expect"])  # every label points at a real file
    s = summarize(evaluate(ix, queries))["by_category"]
    # Keyword-shaped requests must not regress, whatever the embedder.
    for cat in ("exact name", "project code", "type + time"):
        assert s[cat]["hit@1"] == 1.0, (cat, s[cat])
