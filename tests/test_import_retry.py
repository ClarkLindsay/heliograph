import csv, io, logging, os, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))
import heliograph.engine as E
from heliograph.config import Config
from heliograph.engine import Engine
from heliograph.hardcover import Book
from heliograph.providers.base import Provider, Match, AuthError
from heliograph.state import State

E.IMPORT_RETRY_DELAYS = E.IMPORT_AUTH_DELAYS = (0, 0)
E.IMPORT_ROUND_WAIT = 0
LOG = logging.getLogger("heliograph")
LOG.setLevel(logging.INFO)


class FakeHC:
    def __init__(self, books): self.lib = {b.ub_id: b for b in books}
    def fetch_library(self): return list(self.lib.values())
    def fetch_reading(self): return []
    def hydrate(self, b): b.hydrated = True


class Svc(Provider):
    key, label = "goodreads", "Goodreads"
    def __init__(self, cfg, log, fail_first=0, exc=RuntimeError("timeout"), matches=True, auth_ok=True):
        super().__init__(cfg, log)
        self.fail_first, self.exc, self.matches, self.auth_ok, self.calls, self.n = fail_first, exc, matches, auth_ok, [], 0
    def check_auth(self):
        if not self.auth_ok: raise AuthError("refresher not answering")
        return "ok"
    def find(self, b, i=True, t=True): return Match("R%d" % b.ub_id, 300) if self.matches else None
    def read_state(self, rid): return None
    def set_status(self, r, s, b):
        self.n += 1
        if self.n <= self.fail_first: raise self.exc
        self.calls.append((r, s))
    def set_progress(self, *a): pass
    def set_rating(self, *a): self.calls.append(("rating",))


def make(tmp, books, **kw):
    c = Config(os.path.join(tmp, "c.json")); c["goodreads"]["enabled"] = True
    p = Svc(c, LOG, **kw)
    eng = Engine(c, [p], State(os.path.join(tmp, "s.db")), LOG, hardcover=FakeHC(books))
    eng.import_dir = Path(tmp) / "imports"
    return eng, p


def wait(eng):
    for _ in range(600):
        if not eng.job["running"]: return dict(eng.job)
        time.sleep(0.05)
    raise AssertionError("import did not finish")


def bk(i, status="reading"): return Book(i, "Book %d" % i, authors=["A"], status=status, pages=5, total_pages=100)


def test_transient_error_is_retried_and_logged():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        eng, p = make(tmp, [bk(1)], fail_first=1)
        eng.start_import(); job = wait(eng)
        assert job["errors"] == 0 and p.calls == [("R1", "reading")] and job["pushed"] >= 1
        txt, name, ctype = eng.export_log("txt")
        assert b"retrying in 0s" in txt and name.endswith(".txt") and ctype.startswith("text/plain")
        assert (Path(tmp) / "imports" / (job["log_name"] + ".log")).read_text().count("retrying") == 1   # also saved to disk


def test_failure_survives_main_pass_then_succeeds_in_a_retry_round():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        eng, p = make(tmp, [bk(1)], fail_first=3)                  # first item: 1 try + 2 retries all fail
        eng.start_import(); job = wait(eng)
        assert job["errors"] == 0 and p.calls == [("R1", "reading")]
        assert any("retry round 1" in l for l in eng.job_entries)
        rows = list(csv.reader(io.StringIO(eng.export_log("csv")[0].decode("utf-8-sig"))))
        assert rows[0][:3] == ["time", "service", "book"] and [r[5] for r in rows[1:]] == ["error", "updated"]


def test_persistent_failure_is_listed_and_retry_failed_reruns_only_it():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        eng, p = make(tmp, [bk(1), bk(2)], fail_first=10**6, auth_ok=False)
        E.IMPORT_ROUNDS = 1
        try:
            eng.start_import(); job = wait(eng)
            assert job["errors"] == 2 and {i["id"] for i in job["failed_items"]} == {1, 2}
            assert "still failing" in job["message"] and all("timeout" in i["error"] for i in job["failed_items"])
            p.fail_first, p.auth_ok = 0, True                       # the service recovers
            eng.retry_failed(); job = wait(eng)
            assert job["errors"] == 0 and job["retry_only"] and sorted(r for r, _ in p.calls) == ["R1", "R2"]
            try: eng.retry_failed(); assert False
            except ValueError: pass                                # nothing left to retry
        finally:
            E.IMPORT_ROUNDS = 2


def test_login_circuit_breaker_pauses_a_dead_service():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        books = [bk(i) for i in range(1, 9)]
        eng, p = make(tmp, books, fail_first=10**6, exc=AuthError("cookie dead"), auth_ok=False)
        eng.start_import(); job = wait(eng)
        assert p.n == E.IMPORT_BREAKER * 3                          # 5 books x (1 + 2 retries), the other 3 never touched
        assert job["errors"] == 8 and any("not attempted" in i["error"] for i in job["failed_items"])
        assert eng.state.get("baselined:goodreads") in (None, "")  # a dead login isn't recorded as imported


def test_unmatched_books_are_flagged_even_in_a_preview():
    from heliograph.web import link_view
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        eng, p = make(tmp, [bk(1), bk(2, "read")], matches=False)
        eng.start_import(dry=True); job = wait(eng)
        assert job["nomatch"] == 2 and job["errors"] == 0 and {i["id"] for i in job["unmatched"]} == {1, 2}
        assert p.calls == []                                        # preview wrote nothing to the service
        assert eng.state.get_book(1) is not None                    # ...but the book is now listed
        st = eng.state.link(1, "goodreads")
        assert link_view(st, p)["state"] == "nomatch"
        # linking it by hand clears the flag
        eng.link_candidate(1, "goodreads", ref="55")
        assert link_view(eng.state.link(1, "goodreads"), p)["state"] == "linked"


if __name__ == "__main__":
    for t in (test_transient_error_is_retried_and_logged, test_failure_survives_main_pass_then_succeeds_in_a_retry_round,
              test_persistent_failure_is_listed_and_retry_failed_reruns_only_it, test_login_circuit_breaker_pauses_a_dead_service,
              test_unmatched_books_are_flagged_even_in_a_preview):
        try: t()
        except Exception: print("FAILED", t.__name__); raise
    print("import-retry tests passed")
