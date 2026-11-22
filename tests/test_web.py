import base64, json, logging, os, sys, tempfile, threading, time, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from heliograph.config import Config
from heliograph.engine import Engine
from heliograph.hardcover import Book
from heliograph.providers.base import Provider, Match
from heliograph.state import State
from heliograph.web import WebServer, SPEC

H = {"X-Requested-With": "heliograph-ui", "Content-Type": "application/json"}


class FakeHC:
    def __init__(self): self.lib = {}
    def fetch_library(self): return list(self.lib.values())
    def fetch_updated_since(self, ts): return list(self.lib.values())
    def fetch_reading(self): return [b for b in self.lib.values() if b.status == "reading"]
    def fetch_one(self, i): return self.lib.get(i)
    def hydrate(self, b): b.hydrated = True
    def cover_url(self, b): return "https://covers.example/hc.jpg"


class Fake(Provider):
    key, label = "goodreads", "Goodreads"
    HOME = "https://www.goodreads.com/"
    def __init__(self, c, l): super().__init__(c, l); self.calls = []
    def book_url(self, rid): return f"https://x/{rid}"
    def parse_ref(self, t): return t.strip().split("/")[-1] if t.strip() else None
    def search(self, q): return [{"id": "99", "title": "Dune", "authors": ["Frank Herbert"], "pages": 412, "cover": "https://covers.example/99.jpg"}]
    def finalize(self, c): return None if c["id"] == "audio" else Match(c["id"], c.get("pages") or 300)
    def find(self, b, i=True, t=True): return None                       # automatic linking finds nothing
    def check_auth(self): return "Fake OK"
    def set_status(self, r, s, b): self.calls.append(("status", r, s))
    def set_progress(self, r, b, bp, rp): self.calls.append(("progress", r, b.pages))
    def set_rating(self, r, v, b): self.calls.append(("rating", r, v))


def call(srv, method, path, body=None, headers=None, host=None, creds=None):
    h = dict(headers if headers is not None else (H if method == "POST" else {}))
    if host: h["Host"] = host
    if creds: h["Authorization"] = "Basic " + base64.b64encode(f"u:{creds}".encode()).decode()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None))
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read(); return r.status, (json.loads(raw) if r.headers["Content-Type"].startswith("application/json") else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def setup(tmp, password=""):
    cfg = Config(os.path.join(tmp, "c.json"))
    cfg["goodreads"]["enabled"] = True; cfg["common"]["initial_sync"] = "baseline"; cfg["common"]["min_update_interval_seconds"] = 0
    cfg["web"].update(port=0, password=password)
    log, hc = logging.getLogger("t"), FakeHC()
    p = Fake(cfg, log)
    eng = Engine(cfg, [p], State(os.path.join(tmp, "s.db")), log, hardcover=hc)
    hc.lib[1] = Book(1, "Dune", authors=["Frank Herbert"], status="reading", pages=50, total_pages=400)
    hc.lib[2] = Book(2, "Emma", authors=["Jane Austen"], status="read", rating=4)
    srv = WebServer(eng, cfg, log, host="127.0.0.1"); srv.start()
    return cfg, hc, p, eng, srv


def test_pages_security_and_status():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, hc, p, eng, srv = setup(tmp)
        try:
            code, body = call(srv, "GET", "/"); assert code == 200 and b"Heliograph" in body
            assert call(srv, "GET", "/api/status", host="evil.example.com")[0] == 403           # DNS-rebinding guard
            assert call(srv, "GET", "/api/status", host="localhost:8080")[0] == 200
            assert call(srv, "POST", "/api/sync", headers={"Content-Type": "application/json"})[0] == 403   # no CSRF header
            eng.run_once()
            code, st = call(srv, "GET", "/api/status")
            assert st["books"] == 2 and st["examined"] == 2 and st["services"][0]["key"] == "goodreads"
            assert st["services"][0]["home"] == "https://www.goodreads.com/" and st["hardcover_home"] == "https://hardcover.app/"
            cfg["goodreads"]["home_url"] = "https://www.goodreads.com/review/list"
            assert call(srv, "GET", "/api/status")[1]["services"][0]["home"].endswith("/review/list")
            cfg["goodreads"]["home_url"] = "javascript:alert(1)"                      # never used as a link
            assert call(srv, "GET", "/api/status")[1]["services"][0]["home"] == "https://www.goodreads.com/"
            cfg["goodreads"]["home_url"] = ""
            assert call(srv, "POST", "/api/settings", {"goodreads": {"home_url": "javascript:alert(1)"}})[0] == 400
            assert call(srv, "POST", "/api/settings", {"hardcover": {"home_url": "https://hardcover.app/@me"}})[0] == 200
            assert call(srv, "GET", "/api/status")[1]["hardcover_home"] == "https://hardcover.app/@me"
            code, lst = call(srv, "GET", "/api/books")
            assert [b["title"] for b in lst["books"]] == ["Dune", "Emma"]                        # reading first
            assert lst["books"][0]["services"]["goodreads"]["state"] == "recorded" and lst["books"][0]["percent"] == 13
            assert call(srv, "GET", "/api/books?q=austen")[1]["total"] == 1
            assert call(srv, "GET", "/api/books?status=read")[1]["books"][0]["title"] == "Emma"
        finally:
            srv.stop()


def test_password_protects_everything():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, hc, p, eng, srv = setup(tmp, password="s3cret")
        try:
            assert call(srv, "GET", "/")[0] == 401 and call(srv, "GET", "/api/status")[0] == 401
            assert call(srv, "GET", "/api/status", creds="wrong")[0] == 401
            assert call(srv, "GET", "/api/status", creds="s3cret", host="my-nas.lan")[0] == 200   # password replaces the host check
        finally:
            srv.stop()


def test_link_flow():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, hc, p, eng, srv = setup(tmp)
        try:
            eng.run_once(); hc.lib[1].pages = 120; eng.run_once()
            v = call(srv, "GET", "/api/book/1")[1]["services"]["goodreads"]
            assert v["state"] == "nomatch" and "no confident match" in v["error"]                # auto-link failed
            code, r = call(srv, "POST", "/api/book/1/goodreads/search", {"query": "dune"})
            assert code == 200 and r["results"][0]["url"] == "https://x/99" and r["results"][0]["raw"]["id"] == "99"
            assert r["results"][0]["cover"] == "https://covers.example/99.jpg"
            assert call(srv, "GET", "/api/book/1/cover")[1] == {"url": "https://covers.example/hc.jpg"}
            code, b = call(srv, "POST", "/api/book/1/goodreads/link", {"candidate": r["results"][0]["raw"]})
            v = b["services"]["goodreads"]; assert v["state"] == "recorded" or v["state"] == "linked"
            assert v["remote_id"] == "99" and v["manual"] is True and v["error"] is None
            code, r = call(srv, "POST", "/api/book/1/goodreads/push")
            assert code == 200 and r["result"]["pushed"] >= 2                                    # status + progress
            assert ("status", "99", "reading") in p.calls and ("progress", "99", 120) in p.calls
            assert r["book"]["services"]["goodreads"]["state"] == "linked"
            # paste a URL, audio edition refused, bad ref refused
            assert call(srv, "POST", "/api/book/1/goodreads/link", {"ref": "https://www.goodreads.com/book/show/555"})[1]["services"]["goodreads"]["remote_id"] == "555"
            assert call(srv, "POST", "/api/book/1/goodreads/link", {"candidate": {"id": "audio", "title": "x"}})[0] == 400
            assert call(srv, "POST", "/api/book/1/goodreads/link", {"ref": ""})[0] == 400
            # skip blocks pushes, unlink clears the link
            assert call(srv, "POST", "/api/book/1/goodreads/skip", {"skip": True})[1]["services"]["goodreads"]["state"] == "skip"
            assert call(srv, "POST", "/api/book/1/goodreads/push")[0] == 400
            call(srv, "POST", "/api/book/1/goodreads/skip", {"skip": False})
            v = call(srv, "POST", "/api/book/1/goodreads/unlink")[1]["services"]["goodreads"]
            assert v["remote_id"] is None and v["manual"] is False
            assert call(srv, "GET", "/api/book/12345")[0] == 404 and call(srv, "POST", "/api/book/1/nope/push")[0] == 404
        finally:
            srv.stop()


def test_push_respects_sync_settings():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, hc, p, eng, srv = setup(tmp)
        try:
            hc.lib[3] = Book(3, "Wish", status="want"); eng.run_once()
            cfg["common"]["sync_want_to_read"] = False
            call(srv, "POST", "/api/book/3/goodreads/link", {"ref": "7"})
            code, r = call(srv, "POST", "/api/book/3/goodreads/push")
            assert code == 400 and "aren't synced" in r["error"]
        finally:
            srv.stop()


def test_settings_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, hc, p, eng, srv = setup(tmp)
        cfg["hardcover"]["token"] = "TOPSECRET"; cfg["fable"].update(email="a@b.c", password="pw", id_token="tok", refresh_token="r")
        try:
            code, s = call(srv, "GET", "/api/settings")
            raw = json.dumps(s); assert "TOPSECRET" not in raw and '"pw"' not in raw                 # secrets never leave the server
            tok = [f for sec in s["sections"] if sec["section"] == "hardcover" for f in sec["fields"]][0]
            assert tok["set"] is True and "value" not in tok
            code, r = call(srv, "POST", "/api/settings", {"common": {"poll_interval_minutes": 2, "progress_mode": "percent"},
                                                           "hardcover": {"token": ""}, "goodreads": {"cookie_refresh_url": " http://r:5080 "}})
            assert code == 200 and r["saved"] == 3 and cfg["common"]["poll_interval_minutes"] == 2
            assert cfg["hardcover"]["token"] == "TOPSECRET" and cfg["goodreads"]["cookie_refresh_url"] == "http://r:5080"
            assert json.load(open(os.path.join(tmp, "c.json")))["common"]["progress_mode"] == "percent"     # persisted
            call(srv, "POST", "/api/settings", {"fable": {"email": "new@b.c"}})
            assert cfg["fable"]["id_token"] == "" and cfg["fable"]["refresh_token"] == ""            # new login forced
            call(srv, "POST", "/api/settings", {"hardcover": {"token": "__clear__"}}); assert cfg["hardcover"]["token"] == ""
            assert call(srv, "POST", "/api/settings", {"common": {"poll_interval_minutes": "abc"}})[0] == 400
            assert call(srv, "POST", "/api/settings", {"common": {"poll_interval_minutes": 0}})[0] == 400
            assert call(srv, "POST", "/api/settings", {"common": {"progress_mode": "weird"}})[0] == 400
            assert call(srv, "POST", "/api/settings", {"nope": {"x": 1}})[0] == 400
            assert call(srv, "POST", "/api/settings", {"fable": {"id_token": "x"}})[0] == 400          # internal fields aren't editable
            os.environ["HELIOGRAPH__common__sync_ratings"] = "false"
            try:
                cfg2 = Config(os.path.join(tmp, "c.json")); srv.api.cfg = cfg2
                code, r = call(srv, "POST", "/api/settings", {"common": {"sync_ratings": True}})
                assert r["saved"] == 0 and "environment" in r["notes"][0] and cfg2["common"]["sync_ratings"] is False
            finally:
                del os.environ["HELIOGRAPH__common__sync_ratings"]
            assert call(srv, "POST", "/api/test/goodreads")[1] == {"ok": True, "message": "Fake OK"}
        finally:
            srv.stop()


def test_wake_and_refresh():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, hc, p, eng, srv = setup(tmp)
        try:
            assert call(srv, "POST", "/api/refresh-library")[1]["books"] == 2 and eng.state.book_count() == 2
            assert call(srv, "POST", "/api/sync")[1]["ok"] and eng.wake.is_set()
            ev = threading.Event(); t = threading.Thread(target=eng.loop, args=(ev,)); t.start()
            time.sleep(1.5); ev.set(); t.join(5); assert not t.is_alive() and eng.status["last_end"] > 0
        finally:
            srv.stop()


def test_old_database_is_upgraded():
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.db"); db = sqlite3.connect(path)
        db.executescript("""create table links(ub_id integer, service text, remote_id text, remote_pages integer, status text,
            pages integer, rating real, pushed_at real default 0, link_failed_at real default 0, primary key(ub_id, service));
            insert into links values(1,'goodreads','77',300,'reading',5,null,1.0,0);""")
        db.commit(); db.close()
        s = State(path); l = s.link(1, "goodreads")
        assert l["remote_id"] == "77" and l["skip"] == 0 and l["error"] is None
        s.save_link(1, "goodreads", skip=1, error="x"); assert s.link(1, "goodreads")["skip"] == 1


def test_push_all_and_import():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, hc, p, eng, srv = setup(tmp)
        try:
            eng.run_once()
            call(srv, "POST", "/api/book/1/goodreads/link", {"ref": "55"})
            code, r = call(srv, "POST", "/api/book/1/push-all")
            assert code == 200 and r["results"][0]["service"] == "goodreads" and r["results"][0]["ok"] and r["results"][0]["pushed"] >= 2
            assert ("status", "55", "reading") in p.calls and ("progress", "55", 50) in p.calls
            call(srv, "POST", "/api/book/1/goodreads/skip", {"skip": True})
            r = call(srv, "POST", "/api/book/1/push-all")[1]["results"][0]
            assert r["ok"] is False and r.get("skipped") and "skip" in r["error"]
            call(srv, "POST", "/api/book/1/goodreads/skip", {"skip": False})

            # import: preview changes nothing, real import sends everything (links are found via the manual link / find())
            p.find = lambda b, i=True, t=True: Match("R" + str(b.ub_id), 300)
            p.calls.clear()
            def wait():
                for _ in range(100):
                    j = call(srv, "GET", "/api/import")[1]
                    if not j["running"]: return j
                    time.sleep(0.1)
                raise AssertionError("import never finished")
            assert call(srv, "POST", "/api/import", {"services": ["goodreads"], "statuses": ["reading", "read"], "dry": True})[0] == 200
            j = wait()
            assert j["dry"] and j["done"] == 2 and j["total"] == 2 and j["pushed"] >= 3 and p.calls == [] and "Preview finished" in j["message"]
            assert call(srv, "POST", "/api/import", {"services": ["goodreads"], "statuses": ["read"], "skip_existing": True})[0] == 200
            j = wait()
            assert j["done"] == 1 and j["message"].startswith("Import finished") and j["skip_existing"] is True
            assert ("status", "R2", "read") in p.calls and ("rating", "R2", 4) in p.calls and not any(c[1] == "R1" for c in p.calls)
            st = call(srv, "GET", "/api/status")[1]
            assert st["job"]["message"].startswith("Import finished") and st["secrets"]["encrypted"] is False
            # cancel + double start
            slow = p.set_status
            def slow_status(r, s_, b): time.sleep(0.4); slow(r, s_, b)
            p.set_status = slow_status
            assert call(srv, "POST", "/api/import", {"statuses": ["reading", "read", "want"]})[0] == 200
            assert call(srv, "POST", "/api/import", {})[0] == 400                     # already running
            call(srv, "POST", "/api/import/cancel"); j = wait()
            assert j["message"].startswith("Cancelled") or j["done"] == j["total"]
        finally:
            srv.stop()


def test_icons_and_hashed_password():
    import heliograph.web as web
    from heliograph.crypto import hash_password
    with tempfile.TemporaryDirectory() as tmp:
        cfg, hc, p, eng, srv = setup(tmp, password=hash_password("pw1"))
        old = web.APP_DIR; web.APP_DIR = type(old)(tmp)
        try:
            os.makedirs(os.path.join(tmp, "icons")); open(os.path.join(tmp, "icons", "goodreads.svg"), "w").write("<svg/>")
            assert call(srv, "GET", "/icons/goodreads", creds="wrong")[0] == 401
            code, body = call(srv, "GET", "/icons/goodreads", creds="pw1"); assert code == 200 and body == b"<svg/>"
            assert call(srv, "GET", "/icons/storygraph", creds="pw1")[0] == 404
            # the app's own icon: built-in fallback until icons/heliograph.png exists, then that file (also served as /favicon.ico)
            code, body = call(srv, "GET", "/favicon.ico", creds="pw1"); assert code == 200 and b"<svg" in body
            open(os.path.join(tmp, "icons", "heliograph.png"), "wb").write(b"\x89PNG fake")
            code, body = call(srv, "GET", "/icons/heliograph", creds="pw1"); assert code == 200 and body.startswith(b"\x89PNG")
            assert call(srv, "GET", "/favicon.ico", creds="pw1")[1].startswith(b"\x89PNG") and call(srv, "GET", "/icons/..%2Fc", creds="pw1")[0] == 404
            open(os.path.join(tmp, "icons", "evil.svg"), "w").write("<svg/>")     # only known service names are served
            assert call(srv, "GET", "/icons/evil", creds="pw1")[0] == 404 and call(srv, "GET", "/icons/..%2Ficons%2Fgoodreads", creds="pw1")[0] == 404
        finally:
            web.APP_DIR = old; srv.stop()


def test_import_log_flagged_books_and_retry_failed():
    import heliograph.engine as E
    from pathlib import Path
    E.IMPORT_RETRY_DELAYS = E.IMPORT_AUTH_DELAYS = (0, 0); E.IMPORT_ROUND_WAIT = 0; E.IMPORT_ROUNDS = 1
    with tempfile.TemporaryDirectory() as tmp:
        cfg, hc, p, eng, srv = setup(tmp)
        eng.import_dir = Path(tmp) / "imports"
        try:
            assert call(srv, "GET", "/api/import/log")[0] == 404                       # nothing yet
            assert call(srv, "POST", "/api/import/retry-failed")[0] == 400
            assert call(srv, "POST", "/api/import", {"dry": True})[0] == 200
            for _ in range(200):
                if not call(srv, "GET", "/api/import")[1]["running"]: break
                time.sleep(0.05)
            j = call(srv, "GET", "/api/import")[1]
            assert j["nomatch"] == 2 and {i["id"] for i in j["unmatched"]} == {1, 2} and j["log_available"] and not j["can_retry"]
            code, txt = call(srv, "GET", "/api/import/log?fmt=txt"); assert code == 200 and b"no confident match" in txt
            code, csvb = call(srv, "GET", "/api/import/log?fmt=csv"); assert code == 200 and b"no match" in csvb
            # the preview flagged them in the Books tab, and the filter finds them
            assert call(srv, "GET", "/api/books?problems=nomatch")[1]["total"] == 2
            assert call(srv, "GET", "/api/books?problems=1")[1]["total"] == 2
            call(srv, "POST", "/api/book/1/goodreads/link", {"candidate": {"id": "99", "title": "Dune", "authors": [], "pages": 400}})
            assert call(srv, "GET", "/api/books?problems=nomatch")[1]["total"] == 1
            assert [i["id"] for i in call(srv, "GET", "/api/import")[1]["unmatched"]] == [2]     # linked by hand: drops off the list
            # failures are listed and can be retried
            p.set_status = lambda *a: (_ for _ in ()).throw(RuntimeError("timed out"))
            assert call(srv, "POST", "/api/import", {})[0] == 200
            for _ in range(200):
                if not call(srv, "GET", "/api/import")[1]["running"]: break
                time.sleep(0.05)
            j = call(srv, "GET", "/api/import")[1]
            assert j["errors"] == 1 and j["can_retry"] and j["failed_items"][0]["id"] == 1 and "timed out" in j["failed_items"][0]["error"]
            p.set_status = lambda r, s, b: p.calls.append(("status", r, s))
            assert call(srv, "POST", "/api/import/retry-failed")[0] == 200
            for _ in range(200):
                if not call(srv, "GET", "/api/import")[1]["running"]: break
                time.sleep(0.05)
            j = call(srv, "GET", "/api/import")[1]
            assert j["errors"] == 0 and ("status", "99", "reading") in p.calls
        finally:
            srv.stop()
            E.IMPORT_ROUNDS = 2


if __name__ == "__main__":
    for t in (test_push_all_and_import, test_icons_and_hashed_password, test_pages_security_and_status, test_password_protects_everything, test_link_flow, test_push_respects_sync_settings,
              test_settings_roundtrip, test_wake_and_refresh, test_old_database_is_upgraded, test_import_log_flagged_books_and_retry_failed):
        try: t()
        except Exception: print("FAILED", t.__name__); raise
    print("web tests passed")
