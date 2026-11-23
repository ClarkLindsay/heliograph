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
    def fetch_one(self, ub_id): return self.lib.get(ub_id)
    def fetch_reviewed(self): return [b for b in self.lib.values() if b.review]


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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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


def test_failure_warning_only_when_something_failed():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc, p, eng = make(tmp)
        hc.lib[1] = Book(1, "A", status="reading", pages=5, total_pages=100)
        warned = []
        eng.log.warning = lambda msg, *a, **k: warned.append(msg)
        for reviews in (False, True):                          # with review syncing off or on
            eng.config["common"]["sync_reviews"] = reviews
            eng.run_once(); eng.run_once()
        assert not [m for m in warned if "Some updates failed" in m]
        hc.lib[1].status = "read"; p.fail = AuthError("cookie dead")
        eng.run_once()
        assert [m for m in warned if "Some updates failed" in m]
        del eng.log.warning


def test_per_book_error_does_not_block_others_and_holds_window():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc, p, eng = make(tmp, "import_all")
        eng.config["common"]["progress_step_pages"] = 10
        hc.lib[1] = Book(1, "A", status="reading", pages=5, total_pages=300)
        eng.run_once(); p.calls.clear()
        hc.lib[1].pages = 9; eng.run_once(); assert p.calls == []      # < 10 pages
        hc.lib[1].pages = 16; eng.run_once(); assert p.calls == [("progress", "R1", 16, 300)]


def test_import_all_and_want_filter():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc, p, eng = make(tmp, "import_all")
        eng.config["common"]["sync_want_to_read"] = False
        hc.lib[1] = Book(1, "A", status="want"); hc.lib[2] = Book(2, "B", status="read", rating=5)
        eng.run_once()
        assert p.calls == [("status", "R2", "read"), ("rating", "R2", 5)]


def test_new_service_gets_own_baseline():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc, p, eng = make(tmp)
        hc.lib[1] = Book(1, "A", status="reading", pages=3)
        eng.run_once()
        q = Fake(eng.config, eng.log); q.key = "storygraph"
        eng.config["storygraph"]["enabled"] = True
        eng.providers.append(q)
        eng.run_once()
        assert q.calls == [] and eng.state.get("baselined:storygraph") == "1"


def test_dry_run_changes_nothing():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc, p, eng = make(tmp, "import_all", dry=True)
        hc.lib[1] = Book(1, "A", status="read", rating=4)
        eng.run_once()
        assert p.calls == [] and eng.state.get("baselined:goodreads") is None and eng.state.link(1, "goodreads")["remote_id"] is None


def test_link_is_remembered_and_unmatched_is_throttled():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc, p, eng = make(tmp, "import_all")
        p.tracks_progress = False
        hc.lib[1] = Book(1, "A", status="reading", pages=5, total_pages=100)
        eng.run_once(); hc.lib[1].pages = 50; eng.run_once()
        assert p.calls == [("status", "R1", "reading")]
        assert p.state is eng.state                      # providers can persist what they learn


def test_stuck_service_does_not_block_the_others():
    """A Goodreads WAF challenge can hold Goodreads for minutes; StoryGraph and the cycle must carry on meanwhile."""
    import threading
    from heliograph import engine as engine_mod
    from heliograph.engine import Busy
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc, p, eng = make(tmp, "import_all")
        q = Fake(eng.config, eng.log); q.key, q.label = "storygraph", "StoryGraph"
        eng.config["storygraph"]["enabled"] = True
        eng.providers.append(q)
        hc.lib[1] = Book(1, "A", status="read")
        held, release = threading.Event(), threading.Event()

        def stuck():                                     # e.g. an import waiting on the cookie refresher
            with eng.busy("goodreads", timeout=1, why="the import"):
                p.waiting_on = "waiting for the cookie refresher"
                held.set(); release.wait(10)
                p.waiting_on = None
        t = threading.Thread(target=stuck); t.start(); held.wait(5)
        old_wait, engine_mod.CYCLE_WAIT = engine_mod.CYCLE_WAIT, 0.2
        try:
            with eng.busy("storygraph", timeout=0.5):    # other services are free
                pass
            try:
                with eng.busy("goodreads", timeout=0.1):
                    raise AssertionError("goodreads should be busy")
            except Busy as e:
                assert "Fake is busy with the import (waiting for the cookie refresher)" in str(e), str(e)
            eng.run_once()                               # cycle skips the busy service, still serves the rest
            assert q.calls == [("status", "R1", "read")] and p.calls == []
            assert eng.state.get("baselined:goodreads") is None   # not marked done, so Goodreads catches up later
        finally:
            engine_mod.CYCLE_WAIT = old_wait
            release.set(); t.join()
        eng.run_once()
        assert p.calls == [("status", "R1", "read")]


def test_editions_check_and_fix():
    import time as _t
    from heliograph.providers.base import Match
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc, p, eng = make(tmp, "import_all")
        q = Fake(eng.config, eng.log); q.key, q.label = "storygraph", "StoryGraph"
        eng.config["storygraph"]["enabled"] = True; eng.providers.append(q)
        hc.lib[1] = Book(1, "Dune", authors=["Frank Herbert"], status="read")
        hc.lib[2] = Book(2, "Fine", authors=["X"], status="read")
        eng.run_once()                                               # links R1 / R2 on both services
        langs = {"R1": ("German", "Paperback"), "R2": ("English", "Hardcover"), "EN1": ("English", "Paperback")}
        moved = []
        for prov in (p, q):
            prov.check_edition = lambda rid: langs[rid]
            prov.CHECKS_EDITIONS = True
        # StoryGraph-like: another edition of the same work, your entry moved there
        q.switch_edition = lambda a, b: moved.append((a, b))
        q.finalize = lambda cand: Match("EN1", 500, switch_from=cand["id"], note="x") if cand["id"] == "R1" else None
        # Goodreads-like: no switching; a fresh search finds the English edition
        p.find = lambda b, i=True, t=True: Match("EN1", 400) if b.ub_id == 1 else None

        def run(fix):
            eng.start_edition_check(fix)
            for _ in range(200):
                if not eng.ed_job["running"]: break
                _t.sleep(0.02)
            return eng.ed_job
        p.calls.clear(); q.calls.clear()
        job = run(False)                                             # preview: nothing changes
        assert job["total"] == 4 and len(job["items"]) == 2 and moved == [] and p.calls == [] and q.calls == []
        assert {(i["service"], i["new"], i["moves_entry"]) for i in job["items"]} == {("goodreads", "EN1", False), ("storygraph", "EN1", True)}
        assert eng.state.link(1, "storygraph")["remote_id"] == "R1"
        job = run(True)
        assert moved == [("R1", "EN1")] and q.calls == []            # entry moved, nothing re-sent on StoryGraph
        assert p.calls == [("status", "EN1", "read")]                 # Goodreads: status sent to the new edition
        assert eng.state.link(1, "storygraph")["remote_id"] == "EN1" and eng.state.link(1, "goodreads")["remote_id"] == "EN1"
        assert all(i["result"].startswith("fixed") for i in job["items"])
        assert len(run(False)["items"]) == 0                         # all fine now


def test_books_removed_from_hardcover():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc, p, eng = make(tmp, "import_all")
        p.CAN_REMOVE = True; removed = []; fail = {"on": False}
        def rm(rid):
            if fail["on"]: raise RuntimeError("site down")
            removed.append(rid); return True
        p.remove_book = rm
        p.find = lambda b, i=True, t=True: Match("SHARED" if b.ub_id in (3, 4) else "R" + str(b.ub_id), 300)
        lib = {1: Book(1, "Want", status="want"), 2: Book(2, "Read", status="read"), 3: Book(3, "Vol 1", status="want"),
               4: Book(4, "Omnibus", status="want")}
        hc.lib.update(lib)
        eng.run_once()
        assert eng.state.link(3, "goodreads")["remote_id"] == "SHARED"

        for k in (1, 2, 3): hc.lib.pop(k)                         # removed from Hardcover
        eng.config["common"]["remove_from_services"] = "off"
        eng.check_removed(force=True)
        assert removed == [] and eng.state.get_book(1) is not None  # off: untouched, so turning it on later still works
        eng.config["common"]["remove_from_services"] = "want"
        fail["on"] = True
        eng.check_removed(force=True)
        assert removed == [] and eng.state.get_book(1) is not None  # failed: kept for the next check
        fail["on"] = False
        eng.check_removed(force=True)
        assert removed == ["R1"]                                    # want only; the read book and the shared entry stay
        assert all(eng.state.get_book(k) is None for k in (1, 2, 3)) and eng.state.get_book(4) is not None

        hc.lib.update({k: lib[k] for k in (1, 2)}); eng.run_once(); removed.clear()
        eng.config["common"]["remove_from_services"] = "all"
        for k in (1, 2): hc.lib.pop(k)
        eng.check_removed(force=True)
        assert sorted(removed) == ["R1", "R2"]

        # many gone at once: nothing happens until you confirm
        for i in range(10, 17): hc.lib[i] = Book(i, f"B{i}", status="want")
        eng.run_once(); removed.clear()
        saved = dict(hc.lib); hc.lib.clear(); hc.lib[4] = saved[4]
        eng.check_removed(force=True)
        assert removed == [] and len(eng.pending_removals()["books"]) == 7, (removed, eng.pending_removals(), eng.state.get("removal_pending"))
        assert eng.resolve_removals(remove=True)["removed"] == 7 and len(removed) == 7
        assert eng.pending_removals()["books"] == [] and eng.state.get_book(10) is None

        hc.lib.clear(); removed.clear()                             # Hardcover returns nothing: never "all removed"
        eng.check_removed(force=True)
        assert removed == [] and eng.state.get_book(4) is not None


def test_profile_links_are_refreshed_weekly():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc, p, eng = make(tmp)
        p.HOME = "https://www.goodreads.com/"; asked = []
        p.profile_url = lambda: asked.append(1) or "https://www.goodreads.com/user/show/9"
        eng.run_once()
        assert asked == [1] and p.home_url() == "https://www.goodreads.com/user/show/9"
        eng.run_once(); assert asked == [1]                          # not again for a week
        p.profile_url = lambda: "https://elsewhere.example/me"
        eng.refresh_profile_links(force=True)
        assert p.home_url() == "https://www.goodreads.com/user/show/9"        # a link off the service's site is ignored
        eng.config["goodreads"]["home_url"] = "https://www.goodreads.com/mine"
        p.profile_url = lambda: asked.append(2)
        eng.refresh_profile_links(force=True); assert 2 not in asked and p.home_url().endswith("/mine")   # yours wins
        eng.config["goodreads"]["home_url"] = ""
        p.profile_url = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        eng.refresh_profile_links(force=True); assert p.home_url().endswith("/user/show/9")              # failure keeps it


def test_reviews():
    class Reviews(Fake):
        supports_reviews = True
        def __init__(self, cfg, log): super().__init__(cfg, log); self.there = {}
        def get_review(self, r, b): return self.there.get(r)
        def set_review(self, r, text, sp, b): self._maybe(); self.there[r] = text; self.calls.append(("review", r, text, sp))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = Config(os.path.join(tmp, "c.json"))
        cfg["goodreads"]["enabled"] = True
        cfg["common"].update(initial_sync="baseline", min_update_interval_seconds=0)
        hc, log = FakeHC(), logging.getLogger("t")
        p = Reviews(cfg, log)
        eng = Engine(cfg, [p], State(os.path.join(tmp, "s.db")), log, hardcover=hc)
        hc.lib[1] = Book(1, "Read", status="read", rating=4.0, review="Loved it.")
        hc.lib[2] = Book(2, "Theirs", status="read", rating=3.0, review="Hardcover words")
        hc.lib[3] = Book(3, "Same", status="read", rating=5.0, review="It\u2019s  great")
        eng.run_once()                                        # baseline: reviews off, nothing sent
        assert p.calls == []
        cfg["common"]["sync_reviews"] = True
        p.there = {"R2": "My own Goodreads review", "R3": "It's great"}
        for i in (1, 2, 3):
            eng.state.save_link(i, "goodreads", remote_id=f"R{i}")
        eng.run_once()                                        # the catch-up pass picks up reviews written earlier
        assert p.calls == [("review", "R1", "Loved it.", False)], p.calls
        assert eng.state.link(2, "goodreads")["review"].startswith("kept:")    # your own review there: left alone
        assert eng.state.link(3, "goodreads")["review"] == eng.review_sig(hc.lib[3])   # already there: nothing sent
        assert eng.state.get("reviews_caught_up") == "1"
        p.calls.clear(); eng.run_once()
        assert p.calls == []                                  # nothing changed: nothing sent, nothing re-checked
        hc.lib[1].review, hc.lib[1].review_spoilers = "Loved it. Edited.", True
        eng.run_once()
        assert p.calls == [("review", "R1", "Loved it. Edited.", True)]
        p.calls.clear()
        hc.lib[2].review = "Hardcover words, edited"          # a review you kept: still kept after an edit on Hardcover
        eng.run_once()
        assert p.calls == []
        cfg["common"]["overwrite_reviews"] = True
        hc.lib[2].review = "Hardcover words, edited again"
        eng.run_once()
        assert p.calls == [("review", "R2", "Hardcover words, edited again", False)]
        p.calls.clear()
        hc.lib[1].review = None                               # deleting a review on Hardcover never deletes it there
        eng.run_once()
        assert p.calls == [] and p.there["R1"] == "Loved it. Edited."


def test_parse():
    ub = {"id": 9, "book_id": 4, "edition_id": 7, "status_id": 2, "rating": None,
          "book": {"title": "T", "pages": 300, "contributions": [
              {"contribution": None, "author": {"name": "Z"}}, {"contribution": "Narrator", "author": {"name": "N"}}]},
          "user_book_reads": [{"progress_pages": 30, "finished_at": "2026-01-02"}]}
    b = book_from_ub(ub)
    assert (b.status, b.pages, b.percent, b.authors, b.finished_on) == ("reading", 30, 10, ["Z"], "2026-01-02")
    assert Book(1, "x", pages=1, total_pages=8).percent == 13     # floor(x + 0.5), like the plugin


def test_audiobook_progress_is_sent_as_percent():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc, p, eng = make(tmp)
        hc.lib[1] = Book(1, "Listen", status="reading", audio=True, seconds=0, total_seconds=36000)
        eng.run_once()                                         # baseline
        hc.lib[1].seconds = 9000; eng.run_once()               # 25% in, even in pages mode
        assert p.calls == [("progress", "R1", 25, 300)]
        p.calls.clear()
        hc.lib[1].seconds = 9100; eng.run_once()               # still 25%: below the 1% step
        assert p.calls == []
        hc.lib[1].seconds = 18000; eng.run_once()
        assert p.calls == [("progress", "R1", 50, 300)]
        assert eng.state.get_book(1).seconds == 18000


def test_parse_audiobook():
    ub = {"id": 9, "book_id": 4, "edition_id": 7, "status_id": 2, "rating": None,
          "book": {"title": "T", "pages": 300, "contributions": []},
          "edition": {"reading_format_id": 2, "audio_seconds": 40000},
          "user_book_reads": [{"progress_pages": None, "progress_seconds": 10000}]}
    b = book_from_ub(ub)
    assert (b.audio, b.timed, b.percent, b.position) == (True, True, 25, 25)
    b = Book(1, "x", audio=True, pages=30, total_pages=300)            # no time recorded: pages as before
    assert (b.timed, b.position, b.percent) == (False, 30, 10)


if __name__ == "__main__":
    for t in (test_reviews, test_audiobook_progress_is_sent_as_percent, test_parse_audiobook, test_flow, test_failed_update_is_retried_even_outside_the_window, test_failure_warning_only_when_something_failed, test_per_book_error_does_not_block_others_and_holds_window,
              test_steps_and_throttle, test_import_all_and_want_filter, test_new_service_gets_own_baseline,
              test_dry_run_changes_nothing, test_link_is_remembered_and_unmatched_is_throttled, test_status_only_provider_gets_no_progress, test_stuck_service_does_not_block_the_others,
              test_editions_check_and_fix, test_books_removed_from_hardcover, test_profile_links_are_refreshed_weekly, test_parse):
        t()
    print("engine tests passed")
