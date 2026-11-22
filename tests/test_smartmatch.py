import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from heliograph import smartmatch as sm


def verdict(title, authors, cand_title, cand_authors=None, our_pages=None, **kw):
    cand = {"id": "x", "title": cand_title, "authors": cand_authors if cand_authors is not None else authors}
    cand.update(kw)
    r = sm.Matcher().evaluate(title, authors, [cand], pages=our_pages)[0]
    return r


def ok(*a, **kw): return verdict(*a, **kw)["ok"]
def blocked(*a, **kw): return verdict(*a, **kw)["blocked"]


def test_real_cases_from_a_library_cleanup():
    dekker, sanderson = ["Ted Dekker"], ["Brandon Sanderson"]
    # the service adds a subtitle Hardcover doesn't have: linked when something else agrees (pages), else suggested
    assert ok("Green", dekker, "Green: The Beginning and the End (The Circle, #0)", our_pages=390, pages=392)
    g = verdict("Black", dekker, "Black: The Birth of Evil")
    assert not g["ok"] and not g["blocked"] and g["confidence"] >= sm.SUGGEST
    assert ok("Red", dekker, "Red (Circle Book 2)")                 # only a series tag added: plain match
    # ...but never a collection of them
    assert blocked("Green", dekker, "The Complete Circle Series: Black/Red/White/Green") == "a box set / collection"
    assert not ok("Green", dekker, "Black/Red/White/Green")
    # Vol. / Volume / Book / # / roman numerals are the same thing; a different number never is
    assert ok("White Sand, Vol. 1", sanderson, "White Sand, Volume 1 (White Sand, #1)")
    assert ok("White Sand, Vol. 2", sanderson, "White Sand Volume II")
    assert "volume 2" in blocked("White Sand, Vol. 1", sanderson, "White Sand, Volume 2")
    assert blocked("Fullmetal Alchemist, Vol. 2", ["Hiromu Arakawa"], "Fullmetal Alchemist, Vol. 20 (Fullmetal Alchemist, #20)")
    assert blocked("Fullmetal Alchemist, Vol. 2", ["Hiromu Arakawa"], "Fullmetal Alchemist (3-in-1 Edition), Vol. 2")
    # series tags aren't volume numbers; a volume of something you have as a single book isn't it
    assert ok("Red Rising", ["Pierce Brown"], "Red Rising (Red Rising Saga, #1)")
    assert not ok("Dune", ["Frank Herbert"], "Dune: The Graphic Novel, Book 1")
    assert not ok("Dune", ["Frank Herbert"], "Dune Messiah")
    # the old rules' traps stay closed
    assert blocked("The Hobbit, or There and Back Again", ["J.R.R. Tolkien"], "The Hobbit (Part 1 and 2) Slipcase")
    assert ok("The Hobbit, or There and Back Again", ["J.R.R. Tolkien"], "The Hobbit")
    # noise, spelling and author problems
    assert ok("Project Hail Mary", ["Andy Weir"], "Project Hail Mary: A Novel")
    assert ok("Harry Potter and the Philosopher's Stone", ["J.K. Rowling"], "Harry Potter and the Sorcerer's Stone")
    assert blocked("The Lion, the Witch and the Wardrobe", ["C.S. Lewis"], "The Lion, the Witch and the Wardrobe",
                   ["frank herbert"]) == "a different author"
    assert blocked("Mere Christianity", ["C.S. Lewis"], "Mere Christianity: Study Guide") == "a study guide or summary"
    assert blocked("White Sand, Vol. 2", sanderson, "White Sand: Volume 2 [Dramatized Adaptation]") == "a dramatised adaptation"
    # one-book "collections" you actually have, and the ones whose title says several books
    assert ok("A Knight of the Seven Kingdoms", ["George R.R. Martin"], "A Knight of the Seven Kingdoms (The Tales of Dunk and Egg, #1-3)")
    assert ok("Vinland Saga Omnibus, Vol. 1", ["Makoto Yukimura"], "Vinland Saga (2-in-1 Omnibus), Vol. 1")
    # no author on the other side: the title has to be (nearly) exact
    assert ok("The Holy Bible: King James Version", ["Anonymous"], "The Holy Bible: King James Version", ["Unknown Author"])
    assert not ok("The Holy Bible", ["Anonymous"], "The Holy Bible: King James Version", ["Unknown Author"])


def test_ranking_suggestions_and_reasons():
    m = sm.Matcher()
    cands = [{"id": "box", "title": "Skyward 4-Book Boxed Set", "authors": ["Brandon Sanderson"]},
             {"id": "sub", "title": "Skyward: Book One", "authors": ["Brandon Sanderson"]},
             {"id": "exact", "title": "Skyward (Skyward, #1)", "authors": ["Brandon Sanderson"], "isbn": "111"}]
    assert [c["id"] for c in m.ranked("Skyward", ["Brandon Sanderson"], cands, isbns=["111"])] == ["exact"]
    best = m.evaluate("Skyward", ["Brandon Sanderson"], cands, isbns=["111"])[0]
    assert best["cand"]["id"] == "exact" and "same ISBN" in best["reasons"] and best["confidence"] > 0.95
    # a near miss is suggested (never a blocked one)
    near = m.suggestion("The Way of Kings", ["Brandon Sanderson"],
                        [{"id": "b", "title": "The Way of Kings Boxed Set", "authors": ["Brandon Sanderson"]},
                         {"id": "n", "title": "Way of Kings: Prime", "authors": ["Brandon Sanderson"]}])
    assert near and near["cand"]["id"] == "n" and not near["ok"]
    assert m.suggestion("Dune", ["Frank Herbert"], [{"id": "z", "title": "Zebra", "authors": ["Someone Else"]}]) is None


def test_learning_from_your_choices_is_bounded():
    m = sm.Matcher()
    cands = [{"id": "a", "title": "Elantris", "authors": ["Brandon Sanderson"], "pages": 200},
             {"id": "b", "title": "Elantris", "authors": ["Brandon Sanderson"], "pages": 640}]
    before = dict(m.weights)
    for _ in range(200):                                    # you keep picking the one whose page count fits
        m.learn("Elantris", ["Brandon Sanderson"], cands, 1, pages=630)
    assert m.weights["pages"] > before["pages"]
    assert all(abs(m.weights[k] - sm.DEFAULT_WEIGHTS[k]) <= sm.MAX_DRIFT + 1e-9 for k in m.weights)
    assert m.ranked("Elantris", ["Brandon Sanderson"], cands, pages=630)[0]["id"] == "b"
    # a choice the rules forbid teaches nothing
    assert not sm.Matcher().learn("Skyward", ["Brandon Sanderson"],
                                  [{"id": "x", "title": "Skyward 4-Book Boxed Set", "authors": ["Brandon Sanderson"]}], 0)
    assert sm.Matcher({"pages": 9, "nonsense": 1}).weights["pages"] == 9 and "nonsense" not in sm.Matcher({"nonsense": 1}).weights


def test_helpers():
    assert sm.volume_numbers("Fullmetal Alchemist, Vol. 27") == {27}
    assert sm.volume_numbers("Critical Role: Vox Machina Origins Volume II") == {2}
    assert sm.volume_numbers("The Fellowship of the Ring, Book One") == {1}
    assert sm.volume_numbers("Batman: Year One") == set() and sm.volume_numbers("Fahrenheit 451") == set()
    assert sm.clean_title("The Well of Ascension (Mistborn, #2)") == "The Well of Ascension"
    assert sm.clean_title("Embers [Hardcover]") == "Embers"
    q = sm.query_variants("Green: The Beginning and the End", ["Ted Dekker"])
    assert q[0] == "Green: The Beginning and the End Ted Dekker" and "Green Dekker" in q and len(q) == len(set(q))


def _engine(tmp, results):
    import logging
    from heliograph.config import Config
    from heliograph.engine import Engine
    from heliograph.hardcover import Book
    from heliograph.providers.base import Provider, Match
    from heliograph.state import State

    class Svc(Provider):
        key, label = "storygraph", "Svc"
        def __init__(self, cfg, log): super().__init__(cfg, log); self.queries = []; self.calls = []
        def search(self, q): self.queries.append(q); return results.get(q, [])
        def set_status(self, r, s, b): self.calls.append(("status", r, s))
        def set_progress(self, *a): pass
        def set_rating(self, *a): pass

    cfg = Config(os.path.join(tmp, "c.json")); cfg["storygraph"]["enabled"] = True
    cfg["common"]["initial_sync"] = "import_all"
    log = logging.getLogger("t"); p = Svc(cfg, log)
    class HC:
        lib = {}
        def fetch_library(self): return list(self.lib.values())
        def fetch_updated_since(self, ts): return list(self.lib.values())
        def fetch_reading(self): return []
        def fetch_one(self, ub): return self.lib.get(ub)
    hc = HC()
    return Engine(cfg, [p], State(os.path.join(tmp, "s.db")), log, hardcover=hc), p, hc, Book


def test_engine_searches_again_suggests_and_learns():
    import tempfile
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        results = {"Green: The Beginning and the End Ted Dekker": [],              # the first search finds nothing
                   "Green Dekker": [{"id": "G1", "title": "Green", "authors": ["Ted Dekker"]}],
                   "Black Ted Dekker": [{"id": "B9", "title": "Black: The Birth of Evil", "authors": ["Ted Dekker"]},
                                        {"id": "BX", "title": "The Circle Series Boxed Set", "authors": ["Ted Dekker"]}]}
        eng, p, hc, Book = _engine(tmp, results)
        hc.lib[1] = Book(1, "Green: The Beginning and the End", authors=["Ted Dekker"], status="read")
        hc.lib[2] = Book(2, "Black", authors=["Ted Dekker"], status="read")
        eng.run_once()
        assert p.queries[:2] == ["Green: The Beginning and the End Ted Dekker", "Green Dekker"]
        assert eng.state.link(1, "storygraph")["remote_id"] == "G1"                 # found by the second search
        st = eng.state.link(2, "storygraph")                                        # "Black: The Birth of Evil": suggested
        assert not st["remote_id"] and st["link_failed_at"]
        import json
        sug = json.loads(st["suggest"])
        assert sug["id"] == "B9" and sug["confidence"] >= 0.5 and "same title apart from a subtitle" in sug["reasons"]
        from heliograph.web import link_view
        assert link_view(st, p)["suggest"]["id"] == "B9"
        # you use "Find edition…" and pick it: linked, suggestion cleared, and the matcher learns
        res = eng.search_candidates(2, "storygraph")
        assert [r["id"] for r in res][0] == "B9" and res[1]["match"]["blocked"] == "a box set / collection"
        eng.link_candidate(2, "storygraph", cand=res[0]["raw"])
        st = eng.state.link(2, "storygraph")
        assert st["remote_id"] == "B9" and st["suggest"] is None
        assert eng.matching_status()["learned_from"] == 1
        assert eng.reset_matching()["learned_from"] == 0 and eng.matcher.weights == sm.DEFAULT_WEIGHTS
        assert p.matcher is eng.matcher


ALL = [test_real_cases_from_a_library_cleanup, test_ranking_suggestions_and_reasons, test_learning_from_your_choices_is_bounded,
       test_helpers, test_engine_searches_again_suggests_and_learns]

if __name__ == "__main__":
    for t in ALL:
        try:
            t()
        except Exception:
            print("FAILED:", t.__name__); raise
    print("smartmatch tests passed")
