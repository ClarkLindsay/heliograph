import json, logging, os, sys, tempfile, time
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))
from heliograph.config import Config
from pathlib import Path
import heliograph.engine as engine_mod
from heliograph.engine import Engine
from heliograph.hardcover import Book
from heliograph.providers.base import Provider, Match, AuthError
from heliograph.providers import goodreads as gr, storygraph as sg, pagebound as pb, fable as fb, librarything as lt
from heliograph.state import State
from test_providers import Resp, cfg, _flight_page, _page, LOG as PLOG
from test_librarything import provider as lt_provider

engine_mod.IMPORT_RETRY_DELAYS = engine_mod.IMPORT_AUTH_DELAYS = (0, 0)      # keep the retry/backoff paths fast in tests
engine_mod.IMPORT_ROUND_WAIT = 0


# ------------------------------------------------------------------ provider read_state()
def test_goodreads_read_state():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="a=1"); p = gr.Goodreads(c, PLOG)
        apollo = {"ROOT_QUERY": {'getBookByLegacyId({"legacyId":"77"})': {"__ref": "Book:1"}},
                  "Book:1": {"viewerShelving": {"__ref": "S:1"}, "details": {"numPages": 300}},
                  "S:1": {"shelf": {"name": "read"}, "rating": 5}}
        sessions = [{"bookId": "kca://b", "state": "COMPLETED", "endedDate": {"year": 2022, "month": 9, "day": 25}}]
        def fake(method, url, **kw):
            if "/book/show/77" in url: return Resp(200, "<script>%s</script>" % json.dumps(apollo), url=url)
            if "/review/edit/77" in url: return Resp(200, _flight_page(sessions, chunk=False), url=url)
            return Resp(200, "", url=url)
        with mock.patch("requests.request", side_effect=fake):
            st = p.read_state("77")
        assert st["status"] == "read" and st["rating"] == 5.0 and st["finished_dates"] == {"2022-09-25"} and st["known"] == {"rating", "finished_on"}
        apollo["S:1"].pop("rating")                                          # page doesn't expose a rating: unreadable, not "none"
        apollo["S:1"]["shelf"]["name"] = "currently-reading"
        with mock.patch("requests.request", side_effect=fake):
            st = p.read_state("77")
        assert st["status"] == "reading" and "rating" not in st["known"] and st["finished_dates"] == set()
        apollo["Book:1"]["viewerShelving"] = None                            # not shelved at all
        with mock.patch("requests.request", side_effect=fake):
            assert p.read_state("77")["status"] is None
        del apollo["Book:1"]["viewerShelving"]                               # layout changed -> refuse to guess
        with mock.patch("requests.request", side_effect=fake):
            try: p.read_state("77"); assert False
            except gr.ProviderError: pass


def test_storygraph_read_state():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["storygraph"].update(enabled=True, session_cookie="S", remember_user_token="R"); p = sg.StoryGraph(c, PLOG)
        form = ('<form action="/reviews/9"><input name="stars_integer" value="4"><input name="stars_decimal" value="25"></form>')
        def fake(method, url, **kw):
            path = url.replace(sg.BASE, "")
            if "/reviews/9/edit" in path: return Resp(200, form, url=url)
            return Resp(200, _page("Read", review=("/reviews/9", "See review")), url=url)
        with mock.patch("requests.request", side_effect=fake):
            st = p.read_state("x")
        assert st["status"] == "read" and st["rating"] == 4.25 and st["known"] == {"rating"}
        with mock.patch("requests.request", side_effect=lambda m, u, **k: Resp(200, _page("To-Read", review=("/reviews/new?book_id=x", "Add a review")), url=u)):
            st = p.read_state("x")
        assert st["status"] == "want" and st["rating"] is None


def test_fable_and_pagebound_and_librarything_read_state():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["fable"].update(enabled=True, id_token="J", refresh_token="R", token_expires_at=time.time() + 3000, user_id="U1")
        p = fb.Fable(c, PLOG)
        def ffake(method, url, **kw):
            path = url.replace("https://api.fable.co", "")
            if path == "/api/books/B1": return Resp(200, {"response": {"status": "finished"}})
            return Resp(200, {"rating": 4.5})
        with mock.patch("requests.request", side_effect=ffake):
            st = p.read_state("B1")
        assert st["status"] == "read" and st["rating"] == 4.5 and st["known"] == {"rating"}

        c = cfg(tmp); c["pagebound"].update(enabled=True, id_token="I", refresh_token="R", api_token="A", token_expires_at=time.time() + 3000)
        p = pb.Pagebound(c, PLOG)
        ub = {"id": 9, "uuid": "ubu", "status": "finished", "book_id": 5, "finished_reading_at": "9/25/2022"}
        with mock.patch("requests.request", return_value=Resp(200, {"book": {"id": 5, "uuid": "UU", "user_book": ub}})):
            st = p.read_state("5:UU")
        assert st["status"] == "read" and st["finished_dates"] == {"2022-09-25"} and st["known"] == {"finished_on"} and st["rating"] is None
        with mock.patch("requests.request", return_value=Resp(200, {"book": {"id": 5, "uuid": "UU", "user_book": None}})):
            assert p.read_state("5:UU")["status"] is None

    with tempfile.TemporaryDirectory() as tmp:
        c, p = lt_provider(tmp); p._remember("700", title="The Hobbit", author="Tolkien")
        def lfake(method, url, **kw):
            path = url.replace(lt.BASE, "")
            if path.startswith("/catalog_bottom.php"): return Resp(200, '<a href="/work/700/book/9001">x</a>')
            if "reading_dates" in path: return Resp(200, '<input name="edit_type"><input name="newdatestartedA[1]" value="2022-09-01"><input name="newdateendedA[1]" value="2022-09-25">')
            if path.startswith("/ajax_collections_popup.php"): return Resp(200, '<input data-cid="1" checked="checked"> <span>Your library</span>')
            raise AssertionError(path)
        with mock.patch("requests.request", side_effect=lfake):
            st = p.read_state("700")
        assert st["status"] == "read" and st["finished_dates"] == {"2022-09-25"} and st["known"] == {"finished_on"}


# ------------------------------------------------------------------ import with "skip books already up to date"
class FakeHC:
    def __init__(self): self.lib = {}
    def fetch_library(self): return list(self.lib.values())
    def fetch_updated_since(self, ts): return list(self.lib.values())
    def fetch_reading(self): return []
    def hydrate(self, b): b.hydrated = True


class Svc(Provider):
    def __init__(self, cfg, log, key, remote):
        self.key, self.label = key, key.title()
        super().__init__(cfg, log)
        self.remote, self.calls = remote, []
    def find(self, b, i=True, t=True): return Match("R" + str(b.ub_id), 300)
    def read_state(self, rid):
        r = self.remote(rid) if callable(self.remote) else self.remote
        if isinstance(r, Exception): raise r
        return r
    def set_status(self, r, s, b): self.calls.append(("status", s))
    def set_progress(self, r, b, bp, rp): self.calls.append(("progress", b.pages))
    def set_rating(self, r, v, b): self.calls.append(("rating", v))


def run_import(books, remotes, skip=True, dry=False):
    with tempfile.TemporaryDirectory() as tmp:
        c = Config(os.path.join(tmp, "c.json"))
        for k in remotes: c[k]["enabled"] = True
        hc = FakeHC()
        for b in books: hc.lib[b.ub_id] = b
        log = logging.getLogger("t")
        provs = [Svc(c, log, k, r) for k, r in remotes.items()]
        eng = Engine(c, provs, State(os.path.join(tmp, "s.db")), log, hardcover=hc)
        eng.import_dir = Path(tmp) / "imports"
        eng.start_import(skip_existing=skip, dry=dry)
        for _ in range(400):
            if not eng.job["running"]: break
            time.sleep(0.05)
        return {p.key: p.calls for p in provs}, dict(eng.job)


GOT = lambda: Book(1, "Game of Thrones", authors=["George R. R. Martin"], status="read", rating=5.0, finished_on="2022-09-25")
SAME = {"status": "read", "rating": 5.0, "finished_dates": {"2022-09-25"}, "known": {"rating", "finished_on"}}


def test_users_example_goodreads_matches_pagebound_missing():
    calls, job = run_import([GOT()], {"goodreads": dict(SAME), "pagebound": {"status": None, "rating": None, "finished_dates": set(), "known": set()}})
    assert calls["goodreads"] == []                                          # already read, 5 stars, same date: skipped
    assert calls["pagebound"] == [("status", "read"), ("rating", 5.0)]       # not there: written
    assert job["skipped"] == 1 and job["pushed"] == 2 and job["errors"] == 0 and job["done"] == 1


def test_only_what_differs_is_written():
    calls, job = run_import([GOT()], {"goodreads": dict(SAME, rating=3.0)})
    assert calls["goodreads"] == [("rating", 5.0)]                           # wrong stars: just the rating
    calls, _ = run_import([GOT()], {"goodreads": dict(SAME, finished_dates={"2021-01-01"})})
    assert calls["goodreads"] == [("status", "read")]                        # wrong date: re-send status (carries the date)
    calls, _ = run_import([GOT()], {"goodreads": dict(SAME, status="reading")})
    assert calls["goodreads"] == [("status", "read"), ("rating", 5.0)]       # on another shelf: everything
    calls, _ = run_import([GOT()], {"goodreads": dict(SAME, rating=None)})
    assert calls["goodreads"] == [("rating", 5.0)]                           # readable and empty: rating missing


def test_unreadable_fields_are_assumed_right_but_missing_books_get_everything():
    unreadable = {"status": "read", "rating": None, "finished_dates": set(), "known": set()}
    calls, job = run_import([GOT()], {"pagebound": unreadable})
    assert calls["pagebound"] == [] and job["skipped"] == 1                  # can't compare rating/date: leave it alone
    calls, _ = run_import([GOT()], {"pagebound": dict(unreadable, status=None)})
    assert calls["pagebound"] == [("status", "read"), ("rating", 5.0)]
    calls, _ = run_import([GOT()], {"pagebound": None})                      # service can't be read back at all: write as usual
    assert calls["pagebound"] == [("status", "read"), ("rating", 5.0)]


def test_reading_books_wanted_books_and_without_the_option():
    r = Book(2, "Dune", status="reading", pages=50, total_pages=400)
    calls, job = run_import([r], {"goodreads": {"status": "reading", "rating": None, "finished_dates": set(), "known": set()}})
    assert calls["goodreads"] == [] and job["skipped"] == 1                   # right shelf: progress follows later updates
    calls, _ = run_import([r], {"goodreads": {"status": "want", "rating": None, "finished_dates": set(), "known": set()}})
    assert calls["goodreads"] == [("status", "reading"), ("progress", 50)]
    calls, job = run_import([GOT()], {"goodreads": dict(SAME)}, skip=False)  # option off: everything is re-sent
    assert calls["goodreads"] == [("status", "read"), ("rating", 5.0)] and job["skipped"] == 0


def test_errors_are_not_blind_writes_and_preview_changes_nothing():
    calls, job = run_import([GOT()], {"goodreads": RuntimeError("layout changed")})
    assert calls["goodreads"] == [] and job["errors"] == 1 and job["skipped"] == 0
    calls, job = run_import([GOT(), Book(2, "B", status="read")], {"goodreads": AuthError("cookie dead")})
    assert calls["goodreads"] == [] and job["errors"] == 2 and "still failing" in job["message"]    # login keeps failing: both listed for retry
    calls, job = run_import([GOT()], {"goodreads": dict(SAME, rating=1.0), "pagebound": None}, dry=True)
    assert calls == {"goodreads": [], "pagebound": []} and job["pushed"] == 3 and job["dry"]


if __name__ == "__main__":
    for t in (test_goodreads_read_state, test_storygraph_read_state, test_fable_and_pagebound_and_librarything_read_state,
              test_users_example_goodreads_matches_pagebound_missing, test_only_what_differs_is_written,
              test_unreadable_fields_are_assumed_right_but_missing_books_get_everything,
              test_reading_books_wanted_books_and_without_the_option, test_errors_are_not_blind_writes_and_preview_changes_nothing):
        try: t()
        except Exception: print("FAILED", t.__name__); raise
    print("import-skip tests passed")
