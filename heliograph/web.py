"""Tiny built-in web UI + JSON API (stdlib only). See web_index.html (the app) and web_login.html (sign in / register).
Each signed-in user only ever reaches their own engine, settings and log."""
import json, re, threading, time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .accounts import AccountError, LoginThrottle, SESSION_DAYS
from .config import APP_DIR, LOG_PATH
from .engine import Busy
from .providers.goodreads import REFRESHER_ACCOUNT

INDEX = Path(__file__).with_name("web_index.html")
LOGIN = Path(__file__).with_name("web_login.html")
COOKIE = "hg_session"
SERVICE_KEYS = ("goodreads", "storygraph", "fable", "pagebound", "librarything")
APP_ICON_KEY = "heliograph"          # icons/heliograph.png (or .svg/.webp/.jpg) in the data volume is the app's own icon / favicon
FALLBACK_ICON = Path(__file__).with_name("heliograph.svg").read_bytes()   # built-in app icon / favicon
TOUCH_ICON = Path(__file__).with_name("apple-touch-icon.png").read_bytes()  # 180 px, square: iOS rounds the corners
ICON_512 = Path(__file__).with_name("icon-512.png").read_bytes()
MANIFEST = json.dumps({"name": "Heliograph", "short_name": "Heliograph", "start_url": "/", "display": "standalone",
                       "background_color": "#0f4c5c", "theme_color": "#0f4c5c",
                       "icons": [{"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
                                 {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}).encode()
# App icons are served without the web password: iOS and Android fetch them for the Home Screen without sending it,
# and they reveal nothing. Everything else still needs the password.
PUBLIC_ICON_PATHS = ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/icon-512.png", "/manifest.webmanifest")
ICON_TYPES = {"svg": "image/svg+xml", "png": "image/png", "webp": "image/webp", "jpg": "image/jpeg",
              "jpeg": "image/jpeg", "gif": "image/gif"}
HEADER = "X-Requested-With"
HEADER_VALUE = "heliograph-ui"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
STATUS_ORDER = {"reading": 0, "paused": 1, "want": 2, "read": 3, "dnf": 4}

# section -> (label, [(key, label, kind)])   kind: str | secret | bool | int | choice:a,b
SPEC = {
    "hardcover": ("Hardcover", [("token", "API token", "secret"), ("home_url", "Dashboard icon link", "str")]),
    "goodreads": ("Goodreads", [
        ("enabled", "Sync to Goodreads", "bool"),
        ("cookie_refresh_url", "Cookie Auto-Refresh URL", "str"),
        ("cookie_refresh_token", "Cookie Auto-Refresh Token", "secret"),
        ("cookie_refresh_account", "Cookie refresher account (its ACCOUNTS name; blank = default)", "str"),
        ("favorites_shelf", "Shelf for your Hardcover Favorites (Goodreads has no favorites of its own; made the first time a book goes on it)", "str"),
        ("cookie", "Goodreads cookie (fallback)", "secret"),
        ("user_agent", "User-Agent override", "str"),
        ("request_delay_seconds", "Seconds between Goodreads requests (fewer WAF challenges)", "int"),
        ("waf_cooldown_minutes", "Pause after a WAF challenge that can't be cleared (minutes)", "int"),
        ("home_url", "Dashboard icon link", "str")]),
    "storygraph": ("StoryGraph", [
        ("enabled", "Sync to StoryGraph", "bool"),
        ("session_cookie", "_storygraph_session", "secret"),
        ("remember_user_token", "remember_user_token", "secret"),
        ("home_url", "Dashboard icon link", "str")]),
    "fable": ("Fable", [
        ("enabled", "Sync to Fable", "bool"), ("email", "Email", "str"), ("password", "Password", "secret"),
        ("home_url", "Dashboard icon link", "str")]),
    "pagebound": ("Pagebound", [
        ("enabled", "Sync to Pagebound", "bool"), ("email", "Email", "str"), ("password", "Password", "secret"),
        ("home_url", "Dashboard icon link", "str")]),
    "librarything": ("LibraryThing", [
        ("enabled", "Sync to LibraryThing", "bool"), ("username", "Username", "str"),
        ("password", "Password", "secret"), ("session_cookie", "Session cookies (alternative)", "secret"),
        ("read_collection_id", "Read collection id (0 = Your library)", "int"), ("user_agent", "User-Agent override", "str"),
        ("home_url", "Dashboard icon link", "str")]),
    "bookorbit": ("BookOrbit", [
        ("url", "BookOrbit URL (from Docker: http://host.docker.internal:3000)", "str"),
        ("web_url", "BookOrbit address in your browser, for “BookOrbit ↗” links (e.g. https://bookorbit.example.com)", "str"),
        ("api_key", "API key (BookOrbit → Settings → API keys)", "secret"),
        ("auto_ebook", "When you add an ebook to an audiobook you haven't read yet, switch its Hardcover edition to the ebook", "bool"),
        ("from_hardcover", "Copy your Hardcover star ratings and reviews into BookOrbit (Hardcover is the source)", "bool"),
        ("mark_owned", "Your BookOrbit books are copies you own: add them to your Hardcover Owned list and mark them as owned on Goodreads, StoryGraph and Pagebound (and books you add later, every 6 hours)", "bool")]),
    "common": ("Sync behaviour", [
        ("poll_interval_minutes", "Poll Hardcover every (minutes)", "int"),
        ("min_update_interval_seconds", "Min seconds between progress updates", "int"),
        ("progress_mode", "Progress mode", "choice:pages,percent"),
        ("progress_step_percent", "Progress step (%) in percent mode", "int"),
        ("progress_step_pages", "Progress step (pages) in pages mode", "int"),
        ("link_by_isbn", "Automatically link by ISBN", "bool"),
        ("link_by_title", "Automatically link by title and author", "bool"),
        ("preferred_language", "Preferred edition language (blank = any)", "str"),
        ("avoid_audiobooks", "Avoid audiobook editions for books you read (audiobooks in Hardcover get audiobook editions)", "bool"),
        ("sync_want_to_read", "Sync Want to Read", "bool"),
        ("sync_paused_dnf", "Sync Paused / Did Not Finish", "bool"),
        ("sync_ratings", "Sync ratings when marked Read", "bool"),
        ("sync_reviews", "Sync reviews: your Hardcover review goes to Goodreads, StoryGraph, Fable and Pagebound", "bool"),
        ("sync_owned", "Sync owned books: books on your Hardcover Owned list are marked as owned on Goodreads, StoryGraph and Pagebound (taking one off the list unmarks it where Heliograph marked it)", "bool"),
        ("sync_favorites", "Sync favorites: books on your Hardcover Favorites list go to StoryGraph's favorites, your Fable Favorites list, and a favorites shelf on Goodreads and Pagebound (taking one off takes it off where Heliograph added it)", "bool"),
        ("overwrite_reviews", "…even replacing a different review you wrote on a service yourself (off: that one is left alone)", "bool"),
        ("remove_from_services", "When you remove a book from Hardcover, also remove it from your services "
                                 "(off / want = only Want to Read books / all = any status, deletes ratings and reviews there too)",
         "choice:off,want,all"),
        ("initial_sync", "First run for a newly enabled service", "choice:baseline,import_all"),
        ("verbose_logging", "Verbose logging", "bool")]),
}
CREDENTIAL_KEYS = {"email", "password", "username"}      # changing these invalidates stored login tokens
TOKEN_KEYS = ("id_token", "refresh_token", "token_expires_at", "api_token", "user_id")


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message); self.code, self.message = code, message


def link_view(link, provider):
    if link["skip"]:
        state = "skip"
    elif not link["remote_id"] and link["link_failed_at"]:
        state = "nomatch"
    elif link["error"]:
        state = "error"
    elif link["remote_id"]:
        state = "linked"
    elif link["status"] is not None:
        state = "recorded"
    else:
        state = "pending"
    rid = link["remote_id"]
    suggest = None
    if state == "nomatch" and link.get("suggest"):
        try:
            suggest = json.loads(link["suggest"])
        except (TypeError, ValueError):
            suggest = None
    held = None
    try:
        h = json.loads(link.get("held") or "null")
        if h and not h.get("keep") and not h.get("ok"):
            held = {k: h.get(k) for k in ("from", "from_unit", "to", "unit", "at")}
    except (TypeError, ValueError):
        pass
    return {"state": state, "remote_id": rid, "url": provider.book_url(rid) if rid else None,
            "manual": bool(link["manual"]), "error": link["error"], "pushed_at": link["pushed_at"] or None,
            "suggest": suggest, "held": held}


def book_view(b, links, providers):
    pct = b.percent
    svcs = {p.key: link_view(links.get(p.key) or dict(p_default(p)), p) for p in providers}
    return {"id": b.ub_id, "title": b.title, "authors": b.authors, "status": b.status, "pages": b.pages,
            "total_pages": b.total_pages, "percent": pct, "rating": b.rating, "finished_on": b.finished_on,
            "listened": [b.seconds, b.total_seconds] if b.timed else None,
            "services": svcs, "back": next((v["held"] for v in svcs.values() if v["held"]), None)}


def p_default(_p):
    from .state import State
    return State.DEFAULT


def tail(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 65536))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


class Api:
    """The JSON API for one user: their engine, settings and log."""
    def __init__(self, engine, config, log, log_path=LOG_PATH):
        self.e, self.cfg, self.log, self.log_path = engine, config, log, log_path
        self._sync_thread = None

    @property
    def enabled_providers(self):
        return [p for p in self.e.providers if p.enabled]

    # ---- reads
    def status(self):
        by_book = self.e.state.links_by_book()
        services = []
        for p in self.e.providers:
            counts = {"linked": 0, "nomatch": 0, "error": 0, "skip": 0}
            for links in by_book.values():
                if p.key in links:
                    s = link_view(links[p.key], p)["state"]
                    if s in counts:
                        counts[s] += 1
            services.append({"key": p.key, "label": p.label, "enabled": p.enabled, "counts": counts, "home": p.home_url()})
        st = dict(self.e.status)
        st["hardcover_home"] = self.hardcover_home()
        st.update(books=self.e.state.book_count(), services=services, now=time.time(), progress_back=len(self.e.held_back()),
                  poll_minutes=self.cfg["common"]["poll_interval_minutes"], job=self.import_status(),
                  secrets={"encrypted": self.cfg.encryption_enabled, "plaintext": self.cfg._plaintext_on_disk,
                         "detail": self.cfg.crypto.describe()})
        return st

    def books(self, q):
        text, status = (q.get("q") or [""])[0].strip(), (q.get("status") or [""])[0]
        flag = (q.get("problems") or [""])[0]
        problems = flag in ("1", "nomatch")
        offset, limit = int((q.get("offset") or ["0"])[0]), min(int((q.get("limit") or ["50"])[0]), 200)
        books = self.e.state.books(text or None, status or None)
        books.sort(key=lambda b: (STATUS_ORDER.get(b.status, 9), (b.title or "").lower()))
        by_book, prov = self.e.state.links_by_book(), self.enabled_providers
        views = [book_view(b, by_book.get(b.ub_id, {}), prov) for b in books]
        if problems:
            want = ("nomatch",) if flag == "nomatch" else ("nomatch", "error")
            views = [v for v in views if v["back"] or any(s["state"] in want for s in v["services"].values())]
        elif flag == "back":
            views = [v for v in views if v["back"]]
        return {"total": len(views), "books": views[offset: offset + limit]}

    def book(self, ub_id):
        b = self.e.state.get_book(ub_id)
        if not b:
            raise ApiError(404, "Book not found (try Refresh library)")
        return book_view(b, self.e.state.links_by_book().get(ub_id, {}), self.enabled_providers)

    def cover(self, ub_id):
        return {"url": self._guard(self.e.book_cover, ub_id)}

    def import_status(self):
        j = dict(self.e.job)
        j.pop("cancel", None)
        # books the user has since linked by hand no longer count as unmatched
        j["unmatched"] = [i for i in j.get("unmatched") or [] if not self.e.state.link(i["id"], i["service"])["remote_id"]]
        j["recent"] = self.e.job_entries[-10:] if j.get("running") else []
        j["log_available"] = self.e.log_available()
        j["can_retry"] = bool(j.get("failed_items")) and not j.get("running")
        return j

    def import_log(self, fmt):
        try:
            return self.e.export_log("csv" if fmt == "csv" else "txt")
        except KeyError as e:
            raise ApiError(404, e.args[0])

    def import_retry(self):
        self._guard(self.e.retry_failed)
        return self.import_status()

    def log_lines(self, n):
        return {"lines": tail(self.log_path, n)}

    # ---- actions
    def _guard(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Busy as e:
            raise ApiError(409, str(e))
        except KeyError as e:
            raise ApiError(404, str(e.args[0]) if e.args else "not found")
        except ValueError as e:
            raise ApiError(400, str(e))
        except Exception as e:
            self.log.error("Web action failed: %s", e)
            raise ApiError(502, str(e)[:300])

    def sync_now(self):
        self.e.request_sync()
        return {"ok": True}

    def refresh_library(self):
        return {"books": self._guard(self.e.refresh_library)}

    def search(self, ub_id, key, body):
        res = self._guard(self.e.search_candidates, ub_id, key, (body.get("query") or "").strip() or None)
        return {"results": res}

    def link(self, ub_id, key, body):
        if body.get("ref") is not None:
            self._guard(self.e.link_candidate, ub_id, key, ref=str(body["ref"]))
        elif isinstance(body.get("candidate"), dict):
            self._guard(self.e.link_candidate, ub_id, key, cand=body["candidate"])
        else:
            raise ApiError(400, "Send a candidate or a ref")
        return self.book(ub_id)

    def unlink(self, ub_id, key):
        self._guard(self.e.unlink, ub_id, key); return self.book(ub_id)

    def skip(self, ub_id, key, body):
        self._guard(self.e.set_skip, ub_id, key, bool(body.get("skip"))); return self.book(ub_id)

    def push(self, ub_id, key):
        res = self._guard(self.e.push_now, ub_id, key)
        return {"result": res, "book": self.book(ub_id)}

    def push_all(self, ub_id):
        res = self._guard(self.e.push_all, ub_id)
        return {"results": res, "book": self.book(ub_id)}

    def resolve_back(self, ub_id, body):
        res = self._guard(self.e.resolve_back, ub_id, bool(body.get("send")))
        return {"results": res, "book": self.book(ub_id)}

    def import_start(self, body):
        known = {p.key for p in self.e.providers}
        services = [k for k in (body.get("services") or []) if k in known]
        statuses = [x for x in (body.get("statuses") or []) if x in STATUS_ORDER]
        self._guard(self.e.start_import, services or None, statuses or None, bool(body.get("dry")), bool(body.get("skip_existing")))
        return self.import_status()

    def import_cancel(self):
        self.e.cancel_import()
        return self.import_status()

    def test(self, key):
        try:
            if key == "hardcover":
                from .hardcover import HardcoverClient
                HardcoverClient(self.cfg["hardcover"]["token"]).user_id
                return {"ok": True, "message": "Hardcover token OK"}
            if key == "bookorbit":
                me = self.e.bookorbit().me() or {}
                return {"ok": True, "message": f"BookOrbit OK: signed in as {me.get('username') or '?'}"}
            p = self.e.provider(key)
            if not p:
                raise ApiError(404, "unknown service")
            with self.e.busy(key, why="a connection test"):
                msg = p.check_auth()
            self.e.refresh_profile_links(keys=[key], force=True)   # signed in: the icon can link to your profile now
            return {"ok": True, "message": msg}
        except Busy as e:
            raise ApiError(409, str(e))
        except ApiError:
            raise
        except Exception as e:
            return {"ok": False, "message": str(e)[:300]}

    # ---- settings
    def settings(self):
        out = []
        for sec, (label, fields) in SPEC.items():
            rows = []
            for key, flabel, kind in fields:
                val = self.cfg[sec][key]
                row = {"key": key, "label": flabel, "kind": kind, "locked": (sec, key) in self.cfg._env}
                if kind == "secret":
                    row["set"] = bool(val)
                else:
                    row["value"] = val
                if key == "home_url" and not (val or "").strip():
                    row["auto"] = self.auto_home(sec)                 # shown in the box; saving it unchanged keeps it automatic
                rows.append(row)
            out.append({"section": sec, "label": label, "fields": rows})
        return {"sections": out}

    def hardcover_home(self):
        custom = (self.cfg["hardcover"].get("home_url") or "").strip()
        if custom.startswith(("https://", "http://")):
            return custom
        user = getattr(self.e._hc, "username", None)
        if not user and (self.cfg["hardcover"].get("token") or "").strip():
            try:
                hc = self.e.hc(); hc.user_id                             # one request, then cached by the client
                user = getattr(hc, "username", None)
            except Exception:
                user = None
        return f"https://hardcover.app/@{user}" if user else "https://hardcover.app/"

    def auto_home(self, section):
        """The link the dashboard icon uses when you haven't set one: your profile there, else the homepage."""
        if section == "hardcover":
            return self.hardcover_home()
        p = self.e.provider(section)
        return p.home_url() if p else None

    def save_settings(self, body):
        changes, errors = [], []
        for sec, vals in (body or {}).items():
            if sec not in SPEC or not isinstance(vals, dict):
                raise ApiError(400, f"Unknown section {sec}")
            kinds = {k: kind for k, _, kind in SPEC[sec][1]}
            for key, val in vals.items():
                kind = kinds.get(key)
                if kind is None:
                    raise ApiError(400, f"Unknown setting {sec}.{key}")
                if (sec, key) in self.cfg._env:
                    errors.append(f"{sec}.{key} is set by an environment variable and can't be changed here")
                    continue
                if kind == "secret":
                    if val == "":
                        continue
                    val = "" if val == "__clear__" else str(val)
                elif kind == "bool":
                    val = bool(val)
                elif kind == "int":
                    try:
                        val = int(val)
                    except (TypeError, ValueError):
                        raise ApiError(400, f"{SPEC[sec][1][[k for k, _, _ in SPEC[sec][1]].index(key)][1]} must be a number")
                    if val < (0 if key in ("read_collection_id", "min_update_interval_seconds", "request_delay_seconds",
                                           "waf_cooldown_minutes") else 1):
                        raise ApiError(400, f"{key} is out of range")
                elif kind.startswith("choice:"):
                    if val not in kind[7:].split(","):
                        raise ApiError(400, f"Invalid value for {key}")
                else:
                    val = str(val).strip()
                    if key == "home_url" and val and not re.match(r"^https?://\S+$", val):
                        raise ApiError(400, "The homepage link must start with http:// or https://")
                    if key == "cookie_refresh_account":
                        val = val.lower()
                        if val and not REFRESHER_ACCOUNT.match(val):
                            raise ApiError(400, "Refresher account names are lowercase letters, digits, - and _ (as in its ACCOUNTS)")
                if self.cfg[sec][key] != val:
                    changes.append((sec, key, val))
        for sec, key, val in changes:
            self.cfg[sec][key] = val
            if key in CREDENTIAL_KEYS and sec in ("fable", "pagebound"):
                for t in TOKEN_KEYS:
                    self.cfg[sec][t] = 0 if t == "token_expires_at" else ""      # force a fresh login
        if changes:
            self.cfg.save()
            if any(s == "hardcover" for s, _, _ in changes):
                self.e.reset_hardcover()
            if any(k == "verbose_logging" for _, k, _ in changes):
                import logging
                self.log.setLevel(logging.DEBUG if self.cfg["common"]["verbose_logging"] else logging.INFO)
            self.log.info("Settings changed from the web UI: %s", ", ".join(f"{s}.{k}" for s, k, _ in changes))
        return {"saved": len(changes), "notes": errors, **self.settings()}


def make_handler(hub, cfg, server_log):
    html, login_html = INDEX.read_bytes(), LOGIN.read_bytes()
    web = cfg["web"]
    apis, apis_lock, throttle = {}, threading.Lock(), LoginThrottle()

    def api_for(name):
        rt = hub.runtime(name)
        if rt is None:
            return None
        with apis_lock:
            a = apis.get(name)
            if a is None or a.e is not rt.engine:
                a = apis[name] = Api(rt.engine, rt.config, rt.log, rt.log_path)
            return a

    class Handler(BaseHTTPRequestHandler):
        server_version = "heliograph"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                             "img-src 'self' data: https:; frame-ancestors 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        # ---- sessions
        def _token(self):
            c = SimpleCookie()
            try:
                c.load(self.headers.get("Cookie") or "")
            except Exception:
                return None
            return c[COOKIE].value if COOKIE in c else None

        def _user(self):
            return hub.accounts.session_user(self._token())

        def _https(self):
            return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" or \
                '"https"' in (self.headers.get("CF-Visitor") or "")

        def _cookie(self, token, max_age):
            v = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}" + ("; Secure" if self._https() else "")
            return {"Set-Cookie": v}

        def _local(self):
            """The page was opened as localhost (or an allowed host name): you're at the machine running it."""
            raw = self.headers.get("Host") or ""
            host = raw[:raw.index("]") + 1] if raw.startswith("[") and "]" in raw else (raw.rsplit(":", 1)[0] if ":" in raw else raw)
            allowed = LOCAL_HOSTS | {h.strip().lower() for h in (web.get("allowed_hosts") or "").split(",") if h.strip()}
            return host.lower() in allowed

        def _auth(self, method, path, body):
            """/auth/*: sign in, register, sign out, and what the sign-in page needs to know."""
            if method == "GET" and path == "/auth/state":
                return 200, {"first_run": hub.first_run(), "existing_data": hub.has_legacy_data(),
                             "setup_code": bool(hub.setup_code_hash()), "local": self._local(), "user": self._user()}, None
            if method != "POST":
                return 404, {"error": "not found"}, None
            if path == "/auth/logout":
                hub.accounts.end_session(self._token())
                return 200, {"ok": True}, self._cookie("", 0)
            name = (body.get("username") or "").strip().lower()
            if path == "/auth/login":
                wait = throttle.wait_for(name)
                if wait:
                    return 429, {"error": f"Too many failed attempts; try again in {int(wait) + 1} s"}, None
                if not hub.accounts.verify(name, body.get("password") or ""):
                    throttle.failed(name)
                    server_log.warning("Failed sign-in for '%s'", name[:40])
                    return 401, {"error": "Wrong username or password"}, None
                throttle.ok(name)
            elif path == "/auth/register":
                key = "\0register"
                wait = throttle.wait_for(key)
                if wait:
                    return 429, {"error": f"Too many failed attempts; try again in {int(wait) + 1} s"}, None
                try:
                    name = hub.register(name, body.get("password") or "", body.get("code") or "", local=self._local())
                except AccountError as e:
                    throttle.failed(key)
                    return 400, {"error": str(e)}, None
                server_log.info("New account '%s'%s", name, " (admin)" if hub.accounts.is_admin(name) else "")
            else:
                return 404, {"error": "not found"}, None
            token = hub.accounts.new_session(name)
            return 200, {"user": name, "admin": hub.accounts.is_admin(name)}, self._cookie(token, SESSION_DAYS * 86400)

        def _admin(self, user, method, path, body):
            if not hub.accounts.is_admin(user):
                raise ApiError(403, "Only the admin can do that")
            if method == "GET" and path == "/api/admin/users":
                url, _, admin_acct = hub.admin_refresher()
                return {"users": hub.accounts.users(), "invites": hub.accounts.invites(), "me": user,
                        "refresher": {"set": bool(url), "admin_account": admin_acct}}
            if method == "POST" and path == "/api/admin/user/refresher":
                try:
                    hub.set_refresher_account(str(body.get("name") or ""), str(body.get("account") or ""))
                except AccountError as e:
                    raise ApiError(400, str(e))
                return {"users": hub.accounts.users()}
            if method == "POST" and path == "/api/admin/invite":
                code = hub.accounts.create_invite(user)
                return {"code": code, "invites": hub.accounts.invites()}
            if method == "POST" and path == "/api/admin/invite/delete":
                hub.accounts.delete_invite(str(body.get("id") or ""))
                return {"invites": hub.accounts.invites()}
            if method == "POST" and path == "/api/admin/user/delete":
                try:
                    hub.delete_user(str(body.get("name") or ""), user)
                except AccountError as e:
                    raise ApiError(400, str(e))
                return {"users": hub.accounts.users()}
            raise ApiError(404, "not found")

        def _public_icon(self, path):
            if path == "/manifest.webmanifest":
                return self._send(200, MANIFEST, "application/manifest+json")
            custom = APP_DIR / "icons" / f"{APP_ICON_KEY}.png"            # your own icon, if you've added one
            if custom.is_file() and custom.stat().st_size < 5_000_000:
                return self._send(200, custom.read_bytes(), "image/png")
            return self._send(200, ICON_512 if path == "/icon-512.png" else TOUCH_ICON, "image/png")

        def _route(self, method):
            u = urlparse(self.path); path, q = u.path, parse_qs(u.query)
            try:
                # public: app icons (Home Screen fetches send no cookie), service logos, the sign-in page
                if method == "GET" and path in PUBLIC_ICON_PATHS:
                    return self._public_icon(path)
                if method == "GET" and path == "/favicon.ico":
                    return self._icon(APP_ICON_KEY)
                if method == "GET" and path.startswith("/icons/"):
                    return self._icon(path[len("/icons/"):])
                body = {}
                if method == "POST":
                    if self.headers.get(HEADER) != HEADER_VALUE:            # CSRF: only the page's own requests
                        return self._send(403, {"error": "missing header"})
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 65536:
                        return self._send(413, {"error": "too large"})
                    raw = self.rfile.read(n) if n else b""
                    body = json.loads(raw or b"{}")
                if path.startswith("/auth/"):
                    code, res, extra = self._auth(method, path, body)
                    return self._send(code, res, extra=extra)
                user = self._user()
                if method == "GET" and path in ("/", "/index.html"):
                    return self._send(200, html if user else login_html, "text/html; charset=utf-8")
                if not path.startswith("/api/"):
                    return self._send(404, {"error": "not found"})
                if not user:
                    return self._send(401, {"error": "Please sign in"})
                if path == "/api/me":
                    return self._send(200, {"user": user, "admin": hub.accounts.is_admin(user)})
                if method == "POST" and path == "/api/account/password":
                    if not hub.accounts.verify(user, body.get("current") or ""):
                        raise ApiError(400, "Your current password is wrong")
                    try:
                        hub.accounts.set_password(user, body.get("new") or "")
                    except AccountError as e:
                        raise ApiError(400, str(e))
                    token = hub.accounts.new_session(user)           # other sessions are signed out
                    return self._send(200, {"ok": True}, extra=self._cookie(token, SESSION_DAYS * 86400))
                if path.startswith("/api/admin/"):
                    return self._send(200, self._admin(user, method, path, body))
                api = api_for(user)
                if api is None:
                    return self._send(503, {"error": "Your settings couldn't be loaded; see the server log"})
                m = re.fullmatch(r"/api/book/(\d+)/hardcover", path) if method == "GET" else None
                if m:                                    # a link in the book view: off to Hardcover, at your edition
                    try:
                        url = api.e.hardcover_url(int(m.group(1)))
                    except KeyError:
                        raise ApiError(404, "no such book")
                    except Exception as e:
                        raise ApiError(502, f"Couldn't reach Hardcover: {str(e)[:200]}")
                    return self._send(302, b"", "text/plain", extra={"Location": url})
                if method == "GET" and path == "/api/import/log":
                    data, name, ctype = api.import_log((q.get("fmt") or ["txt"])[0])
                    return self._send(200, data, ctype, extra={"Content-Disposition": f'attachment; filename="{name}"'})
                self._send(200, self._dispatch(api, method, path, q, body))
            except ApiError as e:
                self._send(e.code, {"error": e.message})
            except json.JSONDecodeError:
                self._send(400, {"error": "bad json"})
            except Exception as e:
                server_log.error("Web request failed: %s", e)
                self._send(500, {"error": "internal error"})

        def _icon(self, key):
            """Custom service icons: put <service>.svg/.png/.webp/.jpg in the data volume's icons/ folder."""
            # Only fixed names are ever looked up: the path comes from this table, never from the request text.
            names = {k: k for k in ("hardcover", APP_ICON_KEY) + SERVICE_KEYS}
            safe = names.get(key)
            if safe:
                for ext, ctype in ICON_TYPES.items():
                    f = APP_DIR / "icons" / f"{safe}.{ext}"
                    if f.is_file() and f.stat().st_size < 5_000_000:
                        return self._send(200, f.read_bytes(), ctype)
            if key == APP_ICON_KEY:
                return self._send(200, FALLBACK_ICON, "image/svg+xml")
            self._send(404, {"error": "no custom icon"})

        def _dispatch(self, api, method, path, q, body):
            if method == "GET":
                if path == "/api/status": return api.status()
                if path == "/api/books": return api.books(q)
                if path == "/api/settings": return api.settings()
                if path == "/api/log": return api.log_lines(min(int((q.get("n") or ["120"])[0]), 500))
                if path == "/api/import": return api.import_status()
                if path == "/api/editions": return dict(api.e.ed_job)
                if path == "/api/hardcover/cleanup": return api._guard(api.e.hardcover_cleanup)
                if path == "/api/removals": return api.e.pending_removals()
                if path == "/api/hardcover/dates": return dict(api.e.dates_job)
                if path == "/api/matching": return api.e.matching_status()
                if path == "/api/bookorbit/owned": return dict(api.e.own_job)
                if path == "/api/bookorbit": return dict(api.e.bo_job, bo_url=api.cfg["bookorbit"].get("url") or "",
                                                              bo_web=api.cfg["bookorbit"].get("web_url") or "")
                m = re.fullmatch(r"/api/book/(\d+)", path)
                if m: return api.book(int(m.group(1)))
                m = re.fullmatch(r"/api/book/(\d+)/cover", path)
                if m: return api.cover(int(m.group(1)))
            else:
                if path == "/api/sync": return api.sync_now()
                if path == "/api/refresh-library": return api.refresh_library()
                if path == "/api/settings": return api.save_settings(body)
                if path == "/api/import": return api.import_start(body)
                if path == "/api/import/cancel": return api.import_cancel()
                if path == "/api/import/retry-failed": return api.import_retry()
                if path == "/api/editions": return dict(api._guard(api.e.start_edition_check, bool(body.get("fix"))))
                if path == "/api/removals": return api._guard(api.e.resolve_removals, bool(body.get("remove")))
                if path == "/api/hardcover/dates/scan": return dict(api._guard(api.e.start_dates_scan))
                if path == "/api/matching/reset": return api.e.reset_matching()
                if path == "/api/bookorbit/scan": return dict(api._guard(api.e.start_bookorbit_scan, bool(body.get("full"))))
                if path == "/api/bookorbit/owned": return dict(api._guard(api.e.start_bookorbit_owned))
                if path == "/api/bookorbit/apply":
                    keys = body.get("keys")
                    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                        raise ApiError(400, "Send keys: a list of book keys")
                    return dict(api._guard(api.e.apply_bookorbit, keys))
                if path == "/api/bookorbit/suggestion": return api._guard(api.e.bookorbit_use_suggestion, str(body.get("key") or ""))
                if path == "/api/bookorbit/search":
                    return api._guard(api.e.bookorbit_search, str(body.get("key") or ""), str(body.get("query") or ""))
                if path == "/api/bookorbit/aside":
                    return api._guard(api.e.bookorbit_set_aside, str(body.get("key") or ""), body.get("aside") is not False)
                if path == "/api/bookorbit/link":
                    return api._guard(api.e.bookorbit_link, str(body.get("key") or ""), str(body.get("ref") or ""))
                if path == "/api/hardcover/dates/apply":
                    keys = body.get("keys")
                    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                        raise ApiError(400, "Send keys: a list of change keys")
                    return dict(api._guard(api.e.apply_dates, keys))
                if path == "/api/hardcover/cleanup":
                    try:
                        ub, ed = int(body["id"]), int(body["edition_id"])
                    except (KeyError, TypeError, ValueError):
                        raise ApiError(400, "Send id and edition_id")
                    return api._guard(api.e.hardcover_set_edition, ub, ed)
                m = re.fullmatch(r"/api/book/(\d+)/push-all", path)
                if m: return api.push_all(int(m.group(1)))
                m = re.fullmatch(r"/api/book/(\d+)/progress-back", path)
                if m: return api.resolve_back(int(m.group(1)), body)
                m = re.fullmatch(r"/api/test/(\w+)", path)
                if m: return api.test(m.group(1))
                m = re.fullmatch(r"/api/book/(\d+)/(\w+)/(search|link|unlink|skip|push)", path)
                if m:
                    ub, key, act = int(m.group(1)), m.group(2), m.group(3)
                    if api.e.provider(key) is None:
                        raise ApiError(404, "unknown service")
                    return {"search": lambda: api.search(ub, key, body), "link": lambda: api.link(ub, key, body),
                            "unlink": lambda: api.unlink(ub, key), "skip": lambda: api.skip(ub, key, body),
                            "push": lambda: api.push(ub, key)}[act]()
            raise ApiError(404, "not found")

        def do_GET(self): self._route("GET")
        def do_POST(self): self._route("POST")

    Handler.apis = apis
    return Handler


class WebServer:
    def __init__(self, hub, config, log, host="0.0.0.0"):
        self.hub = hub
        self.handler = make_handler(hub, config, log)
        self.httpd = ThreadingHTTPServer((host, int(config["web"]["port"])), self.handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="web")

    @property
    def port(self):
        return self.httpd.server_address[1]

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown(); self.httpd.server_close()
