import base64, json, logging, os, sys, tempfile, threading, time, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from heliograph.config import Config
from heliograph.engine import Engine
from heliograph.hardcover import Book
from heliograph.providers.base import Provider, Match
from heliograph.state import State
from heliograph.hub import Hub, Runtime, OWNER
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


def call(srv, method, path, body=None, headers=None, host=None, cookie=None, raw_resp=False):
    """cookie=None sends the test's signed-in session (srv.cookie); cookie="" sends none."""
    h = dict(headers if headers is not None else (H if method == "POST" else {}))
    if host: h["Host"] = host
    token = getattr(srv, "cookie", "") if cookie is None else cookie
    if token: h["Cookie"] = f"hg_session={token}"
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None))
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            if raw_resp: return r.status, r.headers
            return r.status, (json.loads(raw) if r.headers["Content-Type"].startswith("application/json") else raw)
    except urllib.error.HTTPError as e:
        if raw_resp: return e.code, e.headers
        return e.code, json.loads(e.read() or b"{}")


def session_of(headers):
    c = headers.get("Set-Cookie") or ""
    return c.split("hg_session=", 1)[1].split(";", 1)[0] if "hg_session=" in c else None


def fake_runtime(name, folder, tmp, books=None):
    folder.mkdir(parents=True, exist_ok=True)
    cfg = Config(folder / "config.json", env=False)
    cfg["goodreads"]["enabled"] = True; cfg["common"]["initial_sync"] = "baseline"; cfg["common"]["min_update_interval_seconds"] = 0
    log, hc = logging.getLogger("t." + name), FakeHC()
    p = Fake(cfg, log)
    eng = Engine(cfg, [p], State(folder / "state.db"), log, hardcover=hc, data_dir=folder)
    for b in books or []:
        hc.lib[b.ub_id] = b
    rt = Runtime(name, folder, cfg, eng.state, eng, log, folder / "heliograph.log")
    rt.hc, rt.p = hc, p
    return rt


def make_hub(tmp, setup_password="", books=None):
    server = Config(os.path.join(tmp, "server.json"), env=False)
    server["web"].update(port=0, password=setup_password)
    log = logging.getLogger("t.server")
    hub = Hub(server, log, data_dir=tmp, heartbeat=None,
              factory=lambda name, folder, admin: fake_runtime(name, folder, tmp, books(name) if books else None))
    hub.start(run_loops=False)
    srv = WebServer(hub, server, log, host="127.0.0.1"); srv.start()
    return hub, srv


def setup(tmp):
    """One signed-in user ('alice', the admin) with two books; returns her config, fake Hardcover, provider, engine."""
    lib = lambda name: [Book(1, "Dune", authors=["Frank Herbert"], status="reading", pages=50, total_pages=400),
                        Book(2, "Emma", authors=["Jane Austen"], status="read", rating=4)] if name == "alice" else []
    hub, srv = make_hub(tmp, books=lib)
    hub.register("alice", "alice-password", local=True)
    srv.cookie = hub.accounts.new_session("alice")
    rt = hub.runtime("alice")
    srv.cfg_path = rt.dir / "config.json"
    return rt.config, rt.hc, rt.p, rt.engine, srv


def test_pages_security_and_status():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg, hc, p, eng, srv = setup(tmp)
        try:
            code, body = call(srv, "GET", "/"); assert code == 200 and b"Heliograph" in body and b"/api/me" in body   # the app
            code, body = call(srv, "GET", "/", cookie=""); assert code == 200 and b"/auth/login" in body            # sign-in page
            assert call(srv, "GET", "/api/status", cookie="")[0] == 401 and call(srv, "GET", "/api/status", cookie="forged")[0] == 401
            assert call(srv, "POST", "/api/sync", headers={"Content-Type": "application/json"})[0] == 403   # no CSRF header
            assert call(srv, "GET", "/api/me")[1] == {"user": "alice", "admin": True}
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
            assert call(srv, "GET", "/api/hardcover/dates")[1] == {"running": False}           # reading dates: nothing yet
            assert call(srv, "POST", "/api/hardcover/dates/apply", {"keys": "x"})[0] == 400
            assert call(srv, "POST", "/api/hardcover/dates/apply", {"keys": ["5:0"]})[0] == 400   # no scan to apply from
        finally:
            srv.stop()


def test_accounts_invites_and_isolation():
    books = lambda name: {"alice": [Book(1, "Dune", status="reading")], "bob": [Book(7, "Bob's book", status="read")]}.get(name, [])
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hub, srv = make_hub(tmp, books=books)
        try:
            # first account: from this machine (Host 127.0.0.1) it becomes the admin
            assert call(srv, "GET", "/auth/state", cookie="")[1]["first_run"] is True
            code, hdr = call(srv, "POST", "/auth/register", {"username": "Alice", "password": "alice-password"}, cookie="", raw_resp=True)
            assert code == 200 and "HttpOnly" in hdr["Set-Cookie"] and "SameSite=Lax" in hdr["Set-Cookie"]
            alice = session_of(hdr)
            assert call(srv, "GET", "/api/me", cookie=alice)[1] == {"user": "alice", "admin": True}
            # later sign-ups need an invite code, used once
            assert call(srv, "POST", "/auth/register", {"username": "bob", "password": "bob-password"}, cookie="")[0] == 400
            code, r = call(srv, "POST", "/api/admin/invite", {}, cookie=alice)
            assert code == 200 and len(r["code"]) >= 10 and len(r["invites"]) == 1 and r["code"] not in json.dumps(r["invites"])
            assert call(srv, "POST", "/auth/register", {"username": "bob", "password": "short", "code": r["code"]}, cookie="")[0] == 400
            code, hdr = call(srv, "POST", "/auth/register", {"username": "bob", "password": "bob-password", "code": r["code"]},
                             cookie="", raw_resp=True)
            assert code == 200; bob = session_of(hdr)
            assert call(srv, "POST", "/auth/register", {"username": "carol", "password": "carol-password", "code": r["code"]}, cookie="")[0] == 400
            # each user sees only their own library, settings and log
            hub.runtime("alice").engine.refresh_library(); hub.runtime("bob").engine.refresh_library()
            assert [b["title"] for b in call(srv, "GET", "/api/books", cookie=alice)[1]["books"]] == ["Dune"]
            assert [b["title"] for b in call(srv, "GET", "/api/books", cookie=bob)[1]["books"]] == ["Bob's book"]
            assert call(srv, "GET", "/api/book/1", cookie=bob)[0] == 404
            call(srv, "POST", "/api/settings", {"common": {"poll_interval_minutes": 9}}, cookie=bob)
            assert hub.runtime("bob").config["common"]["poll_interval_minutes"] == 9 and hub.runtime("alice").config["common"]["poll_interval_minutes"] == 5
            assert (hub.users_dir / "bob" / "config.json").exists() and (hub.users_dir / "alice" / "state.db").exists()
            # only the admin manages users
            assert call(srv, "GET", "/api/admin/users", cookie=bob)[0] == 403 and call(srv, "POST", "/api/admin/invite", {}, cookie=bob)[0] == 403
            assert [u["name"] for u in call(srv, "GET", "/api/admin/users", cookie=alice)[1]["users"]] == ["alice", "bob"]
            # sign-in: wrong password, throttling, sign-out
            assert call(srv, "POST", "/auth/login", {"username": "bob", "password": "nope"}, cookie="")[0] == 401
            assert call(srv, "POST", "/auth/login", {"username": "nobody", "password": "nope"}, cookie="")[0] == 401
            code, hdr = call(srv, "POST", "/auth/login", {"username": "BOB ", "password": "bob-password"}, cookie="", raw_resp=True)
            assert code == 200; bob2 = session_of(hdr)
            for _ in range(5): call(srv, "POST", "/auth/login", {"username": "alice", "password": "guess"}, cookie="")
            assert call(srv, "POST", "/auth/login", {"username": "alice", "password": "alice-password"}, cookie="")[0] == 429
            assert call(srv, "POST", "/auth/logout", {}, cookie=bob2)[0] == 200 and call(srv, "GET", "/api/me", cookie=bob2)[0] == 401
            # changing your password signs out your other sessions
            assert call(srv, "POST", "/api/account/password", {"current": "wrong", "new": "bob-password-2"}, cookie=bob)[0] == 400
            code, hdr = call(srv, "POST", "/api/account/password", {"current": "bob-password", "new": "bob-password-2"}, cookie=bob, raw_resp=True)
            assert code == 200 and call(srv, "GET", "/api/me", cookie=bob)[0] == 401 and call(srv, "GET", "/api/me", cookie=session_of(hdr))[0] == 200
            # deleting a user signs them out and keeps their folder aside
            assert call(srv, "POST", "/api/admin/user/delete", {"name": "alice"}, cookie=alice)[0] == 400       # not yourself
            assert call(srv, "POST", "/api/admin/user/delete", {"name": "bob"}, cookie=alice)[0] == 200
            assert call(srv, "GET", "/api/me", cookie=session_of(hdr))[0] == 401 and hub.runtime("bob") is None
            assert not (hub.users_dir / "bob").exists() and any(p.name.startswith(".deleted-bob-") for p in hub.users_dir.iterdir())
            assert "alice-password" not in open(os.path.join(tmp, "users.json")).read()
        finally:
            srv.stop()


def test_sharing_the_admins_cookie_refresher():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hub, srv = make_hub(tmp)
        try:
            hub.register("alice", "alice-password", local=True); alice = hub.accounts.new_session("alice")
            for n in ("bob", "carol"):
                hub.register(n, n + "-password", code=hub.accounts.create_invite("alice"))
            bob = hub.accounts.new_session("bob")
            assert hub.shared_refresher("bob") is None                              # admin has no refresher yet
            hub.runtime("alice").config["goodreads"].update(cookie_refresh_url="http://r:5080", cookie_refresh_token="T")
            assert "ask the admin" in hub.shared_refresher("bob")["error"]          # nothing assigned yet
            post = lambda body, who=alice: call(srv, "POST", "/api/admin/user/refresher", body, cookie=who)
            assert post({"name": "bob", "account": "bob"}, who=bob)[0] == 403       # users can't pick their own
            assert post({"name": "bob", "account": "default"})[0] == 400            # the admin's own account
            assert post({"name": "bob", "account": "Not ok!"})[0] == 400
            assert post({"name": "alice", "account": "x"})[0] == 400                # the admin uses Settings
            assert post({"name": "bob", "account": "Bob"})[0] == 200
            assert post({"name": "carol", "account": "bob"})[0] == 400              # one account per user
            assert hub.shared_refresher("bob") == {"url": "http://r:5080", "token": "T", "account": "bob"}
            u = call(srv, "GET", "/api/admin/users", cookie=alice)[1]
            assert u["refresher"] == {"set": True, "admin_account": "default"} and [x["refresher_account"] for x in u["users"]] == ["", "bob", ""]
            gr = next(p for p in hub.runtime("bob").engine.providers if p.key == "goodreads")
            assert gr.shared_refresher()["account"] == "bob"                       # wired into bob's sync
            hub.runtime("alice").config["goodreads"]["cookie_refresh_account"] = "bob"   # admin moves onto bob's
            assert "admin's own" in hub.shared_refresher("bob")["error"]
            assert '"T"' not in json.dumps(call(srv, "GET", "/api/settings", cookie=bob)[1])        # the admin's token never shows
        finally:
            srv.stop()


def test_first_account_needs_this_machine_or_the_setup_code():
    from heliograph.crypto import hash_password
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hub, srv = make_hub(tmp, setup_password=hash_password("old-web-password"))
        try:
            st = call(srv, "GET", "/auth/state", cookie="", host="books.example.com")[1]
            assert st["first_run"] and st["setup_code"] and not st["local"]
            body = {"username": "me", "password": "a-long-password"}
            assert call(srv, "POST", "/auth/register", body, cookie="", host="books.example.com")[0] == 400       # from outside
            assert call(srv, "POST", "/auth/register", dict(body, code="guess"), cookie="", host="books.example.com")[0] == 400
            code, r = call(srv, "POST", "/auth/register", dict(body, code="old-web-password"), cookie="", host="books.example.com")
            assert code == 200 and r == {"user": "me", "admin": True}
            assert call(srv, "GET", "/auth/state", cookie="")[1]["first_run"] is False
        finally:
            srv.stop()


def test_existing_data_moves_to_the_first_account():
    """A pre-0.4 install (config.json + state.db in the data folder) is kept and handed to the first account."""
    from heliograph.hub import default_factory
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = Config(os.path.join(tmp, "config.json")); c["common"]["poll_interval_minutes"] = 7; c.save()
        s = State(os.path.join(tmp, "state.db")); s.save_link(1, "goodreads", remote_id="77"); s.db.close()
        server = Config(os.path.join(tmp, "server.json"), env=False)
        hub = Hub(server, logging.getLogger("t.mig"), data_dir=tmp, heartbeat=None, factory=default_factory)
        hub.start(run_loops=False)
        assert not os.path.exists(os.path.join(tmp, "config.json")) and hub.runtime(OWNER) is not None
        assert os.path.exists(os.path.join(tmp, "backup-before-0.4", "state.db"))
        hub.register("clark", "clark-password", local=True)
        rt = hub.runtime("clark")
        assert rt.config["common"]["poll_interval_minutes"] == 7 and rt.state.link(1, "goodreads")["remote_id"] == "77"
        assert hub.runtime(OWNER) is None and not (hub.users_dir / OWNER).exists()
        hub2 = Hub(server, logging.getLogger("t.mig2"), data_dir=tmp, heartbeat=None, factory=default_factory)
        hub2.start(run_loops=False)                                  # restarting changes nothing
        assert hub2.runtime("clark").state.link(1, "goodreads")["remote_id"] == "77" and hub2.runtime(OWNER) is None
        for h in (hub, hub2):
            for r in h.runtimes.values():
                r.state.db.close()
                for hd in list(r.log.handlers): hd.close(); r.log.removeHandler(hd)


def test_link_flow():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg, hc, p, eng, srv = setup(tmp)
        cfg["hardcover"]["token"] = "TOPSECRET"; cfg["fable"].update(email="a@b.c", password="pw", id_token="tok", refresh_token="r")
        try:
            code, s = call(srv, "GET", "/api/settings")
            raw = json.dumps(s); assert "TOPSECRET" not in raw and '"pw"' not in raw                 # secrets never leave the server
            tok = [f for sec in s["sections"] if sec["section"] == "hardcover" for f in sec["fields"]][0]
            assert tok["set"] is True and "value" not in tok
            # icon links: a blank one shows the link the icon uses automatically (profile, else homepage)
            home = lambda st, sec: next(f for x in st["sections"] if x["section"] == sec for f in x["fields"] if f["key"] == "home_url")
            assert home(s, "goodreads") == {"key": "home_url", "label": "Dashboard icon link", "kind": "str", "locked": False,
                                            "value": "", "auto": "https://www.goodreads.com/"}
            assert home(s, "hardcover")["auto"] == "https://hardcover.app/"
            eng.state.set("profile_url:goodreads", "https://www.goodreads.com/user/show/5")
            assert home(call(srv, "GET", "/api/settings")[1], "goodreads")["auto"] == "https://www.goodreads.com/user/show/5"
            cfg["goodreads"]["home_url"] = "https://www.goodreads.com/mine"
            assert "auto" not in home(call(srv, "GET", "/api/settings")[1], "goodreads")         # your own link: no auto
            cfg["goodreads"]["home_url"] = ""
            code, r = call(srv, "POST", "/api/settings", {"common": {"poll_interval_minutes": 2, "progress_mode": "percent"},
                                                           "hardcover": {"token": ""}, "goodreads": {"cookie_refresh_url": " http://r:5080 "}})
            assert code == 200 and r["saved"] == 3 and cfg["common"]["poll_interval_minutes"] == 2
            assert cfg["hardcover"]["token"] == "TOPSECRET" and cfg["goodreads"]["cookie_refresh_url"] == "http://r:5080"
            assert json.load(open(srv.cfg_path))["common"]["progress_mode"] == "percent"     # persisted
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
                cfg2 = Config(srv.cfg_path); srv.handler.apis["alice"].cfg = cfg2
                code, r = call(srv, "POST", "/api/settings", {"common": {"sync_ratings": True}})
                assert r["saved"] == 0 and "environment" in r["notes"][0] and cfg2["common"]["sync_ratings"] is False
            finally:
                del os.environ["HELIOGRAPH__common__sync_ratings"]
            assert call(srv, "POST", "/api/test/goodreads")[1] == {"ok": True, "message": "Fake OK"}
        finally:
            srv.stop()


def test_wake_and_refresh():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, "s.db"); db = sqlite3.connect(path)
        db.executescript("""create table links(ub_id integer, service text, remote_id text, remote_pages integer, status text,
            pages integer, rating real, pushed_at real default 0, link_failed_at real default 0, primary key(ub_id, service));
            insert into links values(1,'goodreads','77',300,'reading',5,null,1.0,0);""")
        db.commit(); db.close()
        s = State(path); l = s.link(1, "goodreads")
        assert l["remote_id"] == "77" and l["skip"] == 0 and l["error"] is None
        s.save_link(1, "goodreads", skip=1, error="x"); assert s.link(1, "goodreads")["skip"] == 1


def test_push_all_and_import():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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


def test_icons():
    import heliograph.web as web
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg, hc, p, eng, srv = setup(tmp)
        old = web.APP_DIR; web.APP_DIR = type(old)(tmp)
        try:
            # icons are public (the sign-in page and the Home Screen need them before you sign in)
            os.makedirs(os.path.join(tmp, "icons")); open(os.path.join(tmp, "icons", "goodreads.svg"), "w").write("<svg/>")
            code, body = call(srv, "GET", "/icons/goodreads", cookie=""); assert code == 200 and body == b"<svg/>"
            assert call(srv, "GET", "/icons/storygraph")[0] == 404
            # the app's own icon: built-in fallback until icons/heliograph.png exists, then that file (also served as /favicon.ico)
            code, body = call(srv, "GET", "/favicon.ico", cookie=""); assert code == 200 and b"<svg" in body
            open(os.path.join(tmp, "icons", "heliograph.png"), "wb").write(b"\x89PNG fake")
            code, body = call(srv, "GET", "/icons/heliograph"); assert code == 200 and body.startswith(b"\x89PNG")
            assert call(srv, "GET", "/favicon.ico")[1].startswith(b"\x89PNG") and call(srv, "GET", "/icons/..%2Fc")[0] == 404
            # Home Screen icons, without a session
            code, body = call(srv, "GET", "/apple-touch-icon.png", cookie="")
            assert code == 200 and body.startswith(b"\x89PNG")
            assert call(srv, "GET", "/apple-touch-icon-precomposed.png", cookie="")[0] == 200 and call(srv, "GET", "/icon-512.png", cookie="")[0] == 200
            code, body = call(srv, "GET", "/manifest.webmanifest", cookie="")
            assert code == 200 and json.loads(body)["icons"][0]["src"] == "/apple-touch-icon.png"
            assert call(srv, "GET", "/api/status", cookie="")[0] == 401
            open(os.path.join(tmp, "icons", "evil.svg"), "w").write("<svg/>")     # only known service names are served
            assert call(srv, "GET", "/icons/evil")[0] == 404 and call(srv, "GET", "/icons/..%2Ficons%2Fgoodreads")[0] == 404
        finally:
            web.APP_DIR = old; srv.stop()


def test_import_log_flagged_books_and_retry_failed():
    import heliograph.engine as E
    from pathlib import Path
    E.IMPORT_RETRY_DELAYS = E.IMPORT_AUTH_DELAYS = (0, 0); E.IMPORT_ROUND_WAIT = 0; E.IMPORT_ROUNDS = 1
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
    for t in (test_push_all_and_import, test_icons, test_pages_security_and_status, test_accounts_invites_and_isolation,
              test_first_account_needs_this_machine_or_the_setup_code, test_existing_data_moves_to_the_first_account,
              test_sharing_the_admins_cookie_refresher,
              test_link_flow, test_push_respects_sync_settings,
              test_settings_roundtrip, test_wake_and_refresh, test_old_database_is_upgraded, test_import_log_flagged_books_and_retry_failed):
        try: t()
        except Exception: print("FAILED", t.__name__); raise
    print("web tests passed")
