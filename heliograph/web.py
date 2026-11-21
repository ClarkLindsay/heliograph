"""Tiny built-in web UI + JSON API (stdlib only). See web_index.html for the page."""
import base64, json, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .config import APP_DIR, LOG_PATH
from .crypto import verify_password
from .engine import Busy

INDEX = Path(__file__).with_name("web_index.html")
APP_ICON_KEY = "heliograph"          # icons/heliograph.png (or .svg/.webp/.jpg) in the data volume is the app's own icon / favicon
FALLBACK_ICON = Path(__file__).with_name("heliograph.svg").read_bytes()   # built-in app icon / favicon
HEADER = "X-Requested-With"
HEADER_VALUE = "heliograph-ui"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
STATUS_ORDER = {"reading": 0, "paused": 1, "want": 2, "read": 3, "dnf": 4}

# section -> (label, [(key, label, kind)])   kind: str | secret | bool | int | choice:a,b
SPEC = {
    "hardcover": ("Hardcover", [("token", "API token", "secret"), ("home_url", "Homepage link (dashboard icon)", "str")]),
    "goodreads": ("Goodreads", [
        ("enabled", "Sync to Goodreads", "bool"),
        ("cookie_refresh_url", "Cookie Auto-Refresh URL", "str"),
        ("cookie_refresh_token", "Cookie Auto-Refresh Token", "secret"),
        ("cookie", "Goodreads cookie (fallback)", "secret"),
        ("user_agent", "User-Agent override", "str"),
        ("home_url", "Homepage link (dashboard icon)", "str")]),
    "storygraph": ("StoryGraph", [
        ("enabled", "Sync to StoryGraph", "bool"),
        ("session_cookie", "_storygraph_session", "secret"),
        ("remember_user_token", "remember_user_token", "secret"),
        ("home_url", "Homepage link (dashboard icon)", "str")]),
    "fable": ("Fable", [
        ("enabled", "Sync to Fable", "bool"), ("email", "Email", "str"), ("password", "Password", "secret"),
        ("home_url", "Homepage link (dashboard icon)", "str")]),
    "pagebound": ("Pagebound", [
        ("enabled", "Sync to Pagebound", "bool"), ("email", "Email", "str"), ("password", "Password", "secret"),
        ("home_url", "Homepage link (dashboard icon)", "str")]),
    "librarything": ("LibraryThing", [
        ("enabled", "Sync to LibraryThing", "bool"), ("username", "Username", "str"),
        ("password", "Password", "secret"), ("session_cookie", "Session cookies (alternative)", "secret"),
        ("read_collection_id", "Read collection id (0 = Your library)", "int"), ("user_agent", "User-Agent override", "str"),
        ("home_url", "Homepage link (dashboard icon)", "str")]),
    "common": ("Sync behaviour", [
        ("poll_interval_minutes", "Poll Hardcover every (minutes)", "int"),
        ("min_update_interval_seconds", "Min seconds between progress updates", "int"),
        ("progress_mode", "Progress mode", "choice:pages,percent"),
        ("progress_step_percent", "Progress step (%) in percent mode", "int"),
        ("progress_step_pages", "Progress step (pages) in pages mode", "int"),
        ("link_by_isbn", "Automatically link by ISBN", "bool"),
        ("link_by_title", "Automatically link by title and author", "bool"),
        ("sync_want_to_read", "Sync Want to Read", "bool"),
        ("sync_paused_dnf", "Sync Paused / Did Not Finish", "bool"),
        ("sync_ratings", "Sync ratings when marked Read", "bool"),
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
    return {"state": state, "remote_id": rid, "url": provider.book_url(rid) if rid else None,
            "manual": bool(link["manual"]), "error": link["error"], "pushed_at": link["pushed_at"] or None}


def book_view(b, links, providers):
    pct = b.percent
    return {"id": b.ub_id, "title": b.title, "authors": b.authors, "status": b.status, "pages": b.pages,
            "total_pages": b.total_pages, "percent": pct, "rating": b.rating, "finished_on": b.finished_on,
            "services": {p.key: link_view(links.get(p.key) or dict(p_default(p)), p) for p in providers}}


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
    def __init__(self, engine, config, log):
        self.e, self.cfg, self.log = engine, config, log
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
        hc_custom = (self.cfg["hardcover"].get("home_url") or "").strip()
        hc_user = getattr(self.e._hc, "username", None)
        hardcover_home = hc_custom if hc_custom.startswith(("https://", "http://")) else (
            f"https://hardcover.app/@{hc_user}" if hc_user else "https://hardcover.app/")
        st = dict(self.e.status)
        st["hardcover_home"] = hardcover_home
        st.update(books=self.e.state.book_count(), services=services, now=time.time(),
                  poll_minutes=self.cfg["common"]["poll_interval_minutes"], job=self.import_status(),
                  secrets={"encrypted": self.cfg.encryption_enabled, "plaintext": self.cfg._plaintext_on_disk,
                         "detail": self.cfg.crypto.describe()})
        return st

    def books(self, q):
        text, status = (q.get("q") or [""])[0].strip(), (q.get("status") or [""])[0]
        problems = (q.get("problems") or [""])[0] in ("1", "nomatch")
        offset, limit = int((q.get("offset") or ["0"])[0]), min(int((q.get("limit") or ["50"])[0]), 200)
        books = self.e.state.books(text or None, status or None)
        books.sort(key=lambda b: (STATUS_ORDER.get(b.status, 9), (b.title or "").lower()))
        by_book, prov = self.e.state.links_by_book(), self.enabled_providers
        views = [book_view(b, by_book.get(b.ub_id, {}), prov) for b in books]
        if problems:
            want = ("nomatch",) if (q.get("problems") or [""])[0] == "nomatch" else ("nomatch", "error")
            views = [v for v in views if any(s["state"] in want for s in v["services"].values())]
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
        return {"lines": tail(LOG_PATH, n)}

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
            with self.e.busy():
                if key == "hardcover":
                    from .hardcover import HardcoverClient
                    HardcoverClient(self.cfg["hardcover"]["token"]).user_id
                    return {"ok": True, "message": "Hardcover token OK"}
                p = self.e.provider(key)
                if not p:
                    raise ApiError(404, "unknown service")
                return {"ok": True, "message": p.check_auth()}
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
                rows.append(row)
            out.append({"section": sec, "label": label, "fields": rows})
        return {"sections": out}

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
                    if val < (0 if key in ("read_collection_id", "min_update_interval_seconds") else 1):
                        raise ApiError(400, f"{key} is out of range")
                elif kind.startswith("choice:"):
                    if val not in kind[7:].split(","):
                        raise ApiError(400, f"Invalid value for {key}")
                else:
                    val = str(val).strip()
                    if key == "home_url" and val and not re.match(r"^https?://\S+$", val):
                        raise ApiError(400, "The homepage link must start with http:// or https://")
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


def make_handler(api, cfg):
    html = INDEX.read_bytes()
    web = cfg["web"]

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

        def _authorized(self):
            pw = web.get("password")
            if pw:
                h = self.headers.get("Authorization", "")
                try:
                    given = base64.b64decode(h.split(" ", 1)[1]).decode().split(":", 1)[1] if h.startswith("Basic ") else None
                except Exception:
                    given = None
                if given is None or not verify_password(given, pw):
                    self._send(401, {"error": "Authentication required"}, extra={"WWW-Authenticate": 'Basic realm="Heliograph"'})
                    return False
                return True
            raw = self.headers.get("Host") or ""
            host = raw[:raw.index("]") + 1] if raw.startswith("[") and "]" in raw else (raw.rsplit(":", 1)[0] if ":" in raw else raw)
            allowed = LOCAL_HOSTS | {h.strip().lower() for h in (web.get("allowed_hosts") or "").split(",") if h.strip()}
            if host.lower() not in allowed:       # DNS-rebinding protection when there's no password
                self._send(403, {"error": f"Host '{host}' not allowed; set web.allowed_hosts or a password"})
                return False
            return True

        def _route(self, method):
            if not self._authorized():
                return
            u = urlparse(self.path); path, q = u.path, parse_qs(u.query)
            try:
                if method == "GET" and path in ("/", "/index.html"):
                    return self._send(200, html, "text/html; charset=utf-8")
                if method == "GET" and path == "/api/import/log":
                    data, name, ctype = api.import_log((q.get("fmt") or ["txt"])[0])
                    return self._send(200, data, ctype, extra={"Content-Disposition": f'attachment; filename="{name}"'})
                if method == "GET" and path == "/favicon.ico":
                    return self._icon(APP_ICON_KEY)
                if method == "GET" and path.startswith("/icons/"):
                    return self._icon(path[len("/icons/"):])
                if not path.startswith("/api/"):
                    return self._send(404, {"error": "not found"})
                body = {}
                if method == "POST":
                    if self.headers.get(HEADER) != HEADER_VALUE:
                        return self._send(403, {"error": "missing header"})
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 65536:
                        return self._send(413, {"error": "too large"})
                    raw = self.rfile.read(n) if n else b""
                    body = json.loads(raw or b"{}")
                res = self._dispatch(method, path, q, body)
                self._send(200, res)
            except ApiError as e:
                self._send(e.code, {"error": e.message})
            except json.JSONDecodeError:
                self._send(400, {"error": "bad json"})
            except Exception as e:
                api.log.error("Web request failed: %s", e)
                self._send(500, {"error": "internal error"})

        def _icon(self, key):
            """Custom service icons: put <service>.svg/.png/.webp/.jpg in the data volume's icons/ folder."""
            if key in {"hardcover", APP_ICON_KEY} | {p.key for p in api.e.providers}:
                types = {"svg": "image/svg+xml", "png": "image/png", "webp": "image/webp", "jpg": "image/jpeg",
                         "jpeg": "image/jpeg", "gif": "image/gif"}
                for ext, ctype in types.items():
                    f = APP_DIR / "icons" / f"{key}.{ext}"
                    if f.is_file() and f.stat().st_size < 5_000_000:
                        return self._send(200, f.read_bytes(), ctype)
            if key == APP_ICON_KEY:
                return self._send(200, FALLBACK_ICON, "image/svg+xml")
            self._send(404, {"error": "no custom icon"})

        def _dispatch(self, method, path, q, body):
            if method == "GET":
                if path == "/api/status": return api.status()
                if path == "/api/books": return api.books(q)
                if path == "/api/settings": return api.settings()
                if path == "/api/log": return api.log_lines(min(int((q.get("n") or ["120"])[0]), 500))
                if path == "/api/import": return api.import_status()
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
                m = re.fullmatch(r"/api/book/(\d+)/push-all", path)
                if m: return api.push_all(int(m.group(1)))
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

    return Handler


class WebServer:
    def __init__(self, engine, config, log, host="0.0.0.0"):
        self.api = Api(engine, config, log)
        self.httpd = ThreadingHTTPServer((host, int(config["web"]["port"])), make_handler(self.api, config))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="web")

    @property
    def port(self):
        return self.httpd.server_address[1]

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown(); self.httpd.server_close()
