import logging, sys, tempfile, os
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from heliograph.config import Config
from heliograph.engine import Engine
from heliograph.hardcover import Book, book_from_ub
from heliograph.providers.base import Provider, Match, AuthError
from heliograph.state import State


class FakeHC:
    def __init__(self): self.lib = {}
    def fetch_library(self): return list(self.lib.values())
    def fetch_updated_since(self, ts): return list(self.lib.values())
    def fetch_reading(self): return [b for b in self.lib.values() if b.status == "reading"]
    def hydrate(self, b): b.hydrated = True


class Fake(Provider):
    key, label = "goodreads", "Fake"
    def __init__(self, cfg, log): super().__init__(cfg, log); self.calls = []; self.fail = None; self.finds = 0
    def find(self, b, i=True, t=True): self.finds += 1; return Match("R" + str(b.ub_id), 300)
    def _maybe(self):
        if self.fail: raise self.fail
    def set_status(self, r, s, b): self._maybe(); self.calls.append(("status", r, s))
    def set_progress(self, r, b, by_pages, rp): self._maybe(); self.calls.append(("progress", r, b.pages if by_pages else b.percent, rp))
    def set_rating(self, r, v, b): self._maybe(); self.calls.append(("rating", r, v))


def make(tmp, initial="baseline", dry=False):
    cfg = Config(os.path.join(tmp, "c.json"))
    cfg["goodreads"]["enabled"] = True
    cfg["common"]["initial_sync"] = initial
    cfg["common"]["min_update_interval_seconds"] = 0
    hc, log = FakeHC(), logging.getLogger("t")
    p = Fake(cfg, log)
    return hc, p, Engine(cfg, [p], State(os.path.join(tmp, "s.db")), log, hardcover=hc, dry_run=dry)


def test_flow():
    with tempfile.TemporaryDirectory() as tmp:
        hc, p, eng = make(tmp)
        hc.lib[1] = Book(1, "Old", status="reading", pages=10, total_pages=100)
        assert eng.run_once() == 0 and p.calls == []          # baseline pushes nothing
        hc.lib[2] = Book(2, "New", status="reading", pages=0, total_pages=200)
        eng.run_once()
        assert p.calls == [("status", "R2", "reading")]       # pages==0 isn't pushed as progress
        p.calls.clear()
        hc.lib[2].pages = 50; eng.run_once()
        assert p.calls == [("progress", "R2", 50, 300)]       # remote edition pages travel with the call
        p.calls.clear()
        eng.config["common"]["progress_mode"] = "percent"
        hc.lib[2].pages = 100; eng.run_once()
        assert p.calls == [("progress", "R2", 50, 300)]
        p.calls.clear()
        hc.lib[2].status, hc.lib[2].rating = "read", 4.5; eng.run_once()
        assert p.calls == [("status", "R2", "read"), ("rating", "R2", 4.5)]
        p.calls.clear(); eng.run_once(); assert p.calls == []  # idempotent
        hc.lib[2].rating = 3.0; eng.run_once()
        assert p.calls == [("rating", "R2", 3.0)]


def test_failed_update_is_retried_even_outside_the_window():
    with tempfile.TemporaryDirectory() as tmp:
        hc, p, eng = make(tmp)
        hc.lib[1] = Book(1, "A", status="reading", pages=5, total_pages=100)
        eng.run_once()                                         # baseline
        before = eng.state.get("last_poll")
        hc.lib[1].status = "read"; p.fail = AuthError("cookie dead")
        eng.run_once()
        assert eng.state.get("last_poll") == before            # window held open: nothing lost
        p.fail = None; eng.run_once()
        assert p.calls == [("status", "R1", "read")]
        assert eng.state.get("last_poll") != before            # clean cycle moves it on


def test_per_book_error_does_not_block_others_and_holds_window():
    with tempfile.TemporaryDirectory() as tmp:
        hc, p, eng = make(tmp, "import_all")
        hc.lib[1] = Book(1, "A", status="read"); hc.lib[2] = Book(2, "B", status="read")
        orig = p.set_status
        def flaky(r, s, b):
            if r == "R1": raise RuntimeError("boom")
            orig(r, s, b)
        p.set_status = flaky
        eng.run_once()
        assert p.calls == [("status", "R2", "read")]


def test_steps_and_throttle():
    with tempfile.TemporaryDirectory() as tmp:
        hc, p, eng = make(tmp, "import_all")
        eng.config["common"]["progress_step_pages"] = 10
        hc.lib[1] = Book(1, "A", status="reading", pages=5, total_pages=300)
        eng.run_once(); p.calls.clear()
        hc.lib[1].pages = 9; eng.run_once(); assert p.calls == []      # < 10 pages
        hc.lib[1].pages = 16; eng.run_once(); assert p.calls == [("progress", "R1", 16, 300)]


def test_import_all_and_want_filter():
    with tempfile.TemporaryDirectory() as tmp:
        hc, p, eng = make(tmp, "import_all")
        eng.config["common"]["sync_want_to_read"] = False
        hc.lib[1] = Book(1, "A", status="want"); hc.lib[2] = Book(2, "B", status="read", rating=5)
        eng.run_once()
        assert p.calls == [("status", "R2", "read"), ("rating", "R2", 5)]


def test_new_service_gets_own_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        hc, p, eng = make(tmp)
        hc.lib[1] = Book(1, "A", status="reading", pages=3)
        eng.run_once()
        q = Fake(eng.config, eng.log); q.key = "storygraph"
        eng.config["storygraph"]["enabled"] = True
        eng.providers.append(q)
        eng.run_once()
        assert q.calls == [] and eng.state.get("baselined:storygraph") == "1"


def test_dry_run_changes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        hc, p, eng = make(tmp, "import_all", dry=True)
        hc.lib[1] = Book(1, "A", status="read", rating=4)
        eng.run_once()
        assert p.calls == [] and eng.state.get("baselined:goodreads") is None and eng.state.link(1, "goodreads")["remote_id"] is None


def test_link_is_remembered_and_unmatched_is_throttled():
    with tempfile.TemporaryDirectory() as tmp:
        hc, p, eng = make(tmp, "import_all")
        hc.lib[1] = Book(1, "A", status="reading", pages=3, total_pages=10)
        eng.run_once(); hc.lib[1].pages = 5; eng.run_once()
        assert p.finds == 1 and eng.state.link(1, "goodreads")["remote_pages"] == 300
        p.find = lambda b, i=True, t=True: None
        hc.lib[2] = Book(2, "B", status="read"); n = {"c": 0}
        orig = p.find
        def counting(b, i=True, t=True): n["c"] += 1; return None
        p.find = counting
        eng.run_once(); eng.run_once(); assert n["c"] == 1


def test_status_only_provider_gets_no_progress():
    with tempfile.TemporaryDirectory() as tmp:
        hc, p, eng = make(tmp, "import_all")
        p.tracks_progress = False
        hc.lib[1] = Book(1, "A", status="reading", pages=5, total_pages=100)
        eng.run_once(); hc.lib[1].pages = 50; eng.run_once()
        assert p.calls == [("status", "R1", "reading")]
        assert p.state is eng.state                      # providers can persist what they learn


def test_parse():
    ub = {"id": 9, "book_id": 4, "edition_id": 7, "status_id": 2, "rating": None,
          "book": {"title": "T", "pages": 300, "contributions": [
              {"contribution": None, "author": {"name": "Z"}}, {"contribution": "Narrator", "author": {"name": "N"}}]},
          "user_book_reads": [{"progress_pages": 30, "finished_at": "2026-01-02"}]}
    b = book_from_ub(ub)
    assert (b.status, b.pages, b.percent, b.authors, b.finished_on) == ("reading", 30, 10, ["Z"], "2026-01-02")
    assert Book(1, "x", pages=1, total_pages=8).percent == 13     # floor(x + 0.5), like the plugin


if __name__ == "__main__":
    for t in (test_flow, test_failed_update_is_retried_even_outside_the_window, test_per_book_error_does_not_block_others_and_holds_window,
              test_steps_and_throttle, test_import_all_and_want_filter, test_new_service_gets_own_baseline,
              test_dry_run_changes_nothing, test_link_is_remembered_and_unmatched_is_throttled, test_status_only_provider_gets_no_progress, test_parse):
        t()
    print("engine tests passed")
