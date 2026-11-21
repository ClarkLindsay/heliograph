"""LibraryThing via its website's own AJAX endpoints, ported from ShelfSync's librarything/api.lua + provider.lua.

LibraryThing has no reading-status field: a status is which default collection the copy is in plus its reading
dates. There's no progress tracking, so only status changes (and ratings) are sent: Want -> "To read",
Reading -> "Currently reading" (+ start date today), Read -> read collection (default "Your library") +
finish date. Ratings are half stars, floored. Paused/DNF don't exist there.
"""
import base64, html as htmllib, json, re, time
from urllib.parse import quote_plus
import requests
from .base import Provider, AuthError, ProviderError, Match, clean_cover

BASE = "https://www.librarything.com"
APP_AUTH_URL = "https://api4.librarything.com"
APP_TOKEN = "687aa7458d1ae63c50e1e32887e4d40b"
APP_DIRECT_SIGNIN = "e038a6205d72c98c68e0afa670d0199c"
APP_USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 16; 23090RA98G Build/BP2A.250605.031.A3)"
DEFAULT_UA = "HardcoverSync/0.4 (ShelfSync-compatible; +https://github.com/Lyfts/ShelfSync)"
SESSION_COOKIES = ("cookie_userid", "cookie_usernum", "cookie_userchecksum")
DEFAULT_IDS = {"library": 1, "to_read": 3, "read_unowned": 5, "reading": 7}
COLLECTION_NAMES = {"library": "your library", "to_read": "to read",
                    "read_unowned": "read but unowned", "reading": "currently reading"}
DEFAULT_COLLECTIONS = [(1, "Your library"), (4, "Wishlist"), (7, "Currently reading"),
                       (3, "To read"), (5, "Read but unowned"), (6, "Favorites")]
TEST_WORK_ID = "8556314"
CHALLENGE_ERROR = ("LibraryThing's Cloudflare protection blocked the request with a browser check; "
                   "try again later (or set a different user_agent)")


# ---------- helpers (ports of the plugin's)
def html_text(fragment):
    if fragment is None:
        return None
    return " ".join(htmllib.unescape(re.sub(r"<[^>]*>", " ", fragment)).split())


def input_value(html, name):
    for tag in re.findall(r"<input[^>]*>", html or ""):
        if f'name="{name}"' in tag:
            m = re.search(r'value="([^"]*)"', tag)
            return htmllib.unescape(m.group(1)) if m else ""
    return None


def json_unescape(s):
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "")
    return re.sub(r'\\(["/\\])', r"\1", s)


def b64_decode(data):
    data = re.sub(r"[^A-Za-z0-9+/=]", "", data or "").split("=")[0]
    data += "=" * (-len(data) % 4)
    return base64.b64decode(data).decode("utf-8", "replace")


def session_cookies(header):
    jar = {}
    for name, value in re.findall(r"(?:^|[;,])\s*([\w]+)=([^;,]*)", header or ""):
        if name in SESSION_COOKIES and value.strip() not in ("", "deleted"):
            jar[name] = value.strip()
    return jar


def cookie_header(jar):
    return "; ".join(f"{n}={jar[n]}" for n in SESSION_COOKIES if n in jar)


def collection_key(name):
    n = " ".join((name or "").lower().split())
    return next((k for k, v in COLLECTION_NAMES.items() if v == n), None)


def parse_search_results(html):
    books, seen = [], set()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", re.S):
        wid = re.search(r'data-workid="(\d+)"', row)
        item = re.search(r'<p class="item">(.*?)</p>', row, re.S)
        if not wid or not item or wid.group(1) in seen:
            continue
        seen.add(wid.group(1))
        t = re.search(r'<a[^>]*data-workid="\d+"[^>]*>(.*?)</a>', item.group(1), re.S)
        a = re.search(r'<a[^>]*href="/a/[^"]*"[^>]*>(.*?)</a>', item.group(1), re.S)
        title, author = html_text(t.group(1)) if t else None, html_text(a.group(1)) if a else None
        if title:
            img = re.search(r'<img[^>]*src="([^"]+)"', row)
            books.append({"id": wid.group(1), "title": title, "authors": [author] if author else [],
                          "cover": clean_cover(htmllib.unescape(img.group(1)), BASE) if img else None})
    return books


def parse_collections_popup(html):
    out = []
    for m in re.finditer(r'<input([^<]*?data-cid="(\d+)"[^<]*?)<span>([^<]*)</span>', html or ""):
        name = html_text(m.group(3))
        out.append({"id": int(m.group(2)), "name": name, "key": collection_key(name),
                    "checked": 'checked="checked"' in m.group(1)})
    return out


def parse_collection_checkboxes(html):
    out = []
    for m in re.finditer(r'(<input[^>]*name="books_collections\[\]"[^>]*>)\s*<span>(.*?)</span>', html or "", re.S):
        v = re.search(r'value="(\d+)"', m.group(1))
        if v:
            name = html_text(m.group(2))
            out.append({"id": int(v.group(1)), "name": name, "key": collection_key(name)})
    return out


def parse_toggle_response(html):
    return [{"id": int(i), "name": html_text(n)} for i, n in re.findall(r'collection=(\d+)"[^>]*>(.*?)</a>', html or "", re.S)]


def copy_links(html):
    return list(dict.fromkeys(re.findall(r"/work/(\d+/book/\d+)", html or "")))


def copy_in_links(links, work_id):
    for link in links:
        work, copy = link.split("/book/")
        if work == str(work_id):
            return copy
    return None


def status_for(collections, reads, read_id):
    keys = {c.get("key") for c in collections}
    in_read = bool(read_id) and any(c["id"] == read_id for c in collections)
    if "reading" in keys: return "reading"
    if "to_read" in keys: return "want"
    last = reads[-1] if reads else None
    if (last and last.get("finished")) or "read_unowned" in keys or in_read:
        return "read"
    return None


class LibraryThing(Provider):
    key, label = "librarything", "LibraryThing"
    HOME = "https://www.librarything.com/"
    SUPPORTED = ("want", "reading", "read")
    tracks_progress = False

    def __init__(self, config, log):
        super().__init__(config, log)
        self._site_session = None
        self._new_site = False

    # ---- small persistent kv (engine injects .state)
    def _kv(self, key, default=None):
        if self.state is None:
            return default
        raw = self.state.get(f"lt:{key}")
        return json.loads(raw) if raw else default

    def _kv_set(self, key, value):
        if self.state is not None:
            self.state.set(f"lt:{key}", json.dumps(value))

    def _work(self, wid):
        return (self._kv("works", {}) or {}).get(str(wid), {})

    def _remember(self, wid, **fields):
        works = self._kv("works", {}) or {}
        w = works.get(str(wid), {})
        for k, v in fields.items():
            if v is None: w.pop(k, None)
            else: w[k] = v
        works[str(wid)] = w
        self._kv_set("works", works)

    # ---- session
    def _session(self):
        return cookie_header(session_cookies(self.cfg.get("session_cookie", "")))

    def _usernum(self):
        return session_cookies(self.cfg.get("session_cookie", "")).get("cookie_usernum")

    def _username(self):
        return session_cookies(self.cfg.get("session_cookie", "")).get("cookie_userid") or self.cfg.get("username")

    def _login(self):
        user, pw = self.cfg.get("username"), self.cfg.get("password")
        if not user or not pw:
            raise AuthError("LibraryThing: set username + password, or paste the session cookies")
        r = requests.get(APP_AUTH_URL + "/app_authorize.php", timeout=30, headers={
            "User-Agent": APP_USER_AGENT, "Accept": "application/json", "Accept-Language": "en"},
            params={"v": 2, "app_token": APP_TOKEN, "auth_username": user, "auth_password": pw,
                    "direct_signin": APP_DIRECT_SIGNIN})
        if r.status_code != 200:
            raise AuthError(f"LibraryThing sign-in failed (HTTP {r.status_code})")
        try:
            d = r.json()["data"]
            jar = {n: d[n] for n in SESSION_COOKIES}
        except (ValueError, KeyError, TypeError):
            raise AuthError("LibraryThing returned an incomplete sign-in response")
        if any(not v or v == "deleted" or re.search(r"[,;\s]", str(v)) for v in jar.values()):
            raise AuthError("LibraryThing returned invalid session cookies")
        if jar["cookie_userid"] != self._username():
            self._kv_set("works", {}); self._kv_set("collection_ids", {}); self._kv_set("collections", [])
        self.cfg["session_cookie"] = cookie_header(jar)
        self.config.save()

    def _req(self, path, form=None, method=None, ajax=False, extract=None, _retried=False, params=None):
        if len(session_cookies(self.cfg.get("session_cookie", ""))) < 3:
            self._login()
        cookie = self._session()
        if self._site_session or self._kv("site_session"):
            cookie += "; LTAnonSessionID=" + (self._site_session or self._kv("site_session"))
        method = method or ("POST" if form is not None else "GET")
        h = {"User-Agent": self.cfg.get("user_agent") or DEFAULT_UA, "Cookie": cookie, "Accept-Language": "en",
             "Accept": "*/*" if ajax else "text/html,application/xhtml+xml"}
        if ajax:
            h["X-Requested-With"] = "XMLHttpRequest"
        r = requests.request(method, BASE + path, headers=h, data=form if form is not None else None,
                             allow_redirects=False, timeout=30)
        m = re.findall(r"LTAnonSessionID=([^;,]*)", r.headers.get("set-cookie", ""))
        self._new_site = False
        if m and m[-1] not in ("", "deleted") and m[-1] != self._site_session:
            self._site_session = m[-1]; self._kv_set("site_session", m[-1]); self._new_site = True
        body = r.text
        if r.headers.get("cf-mitigated") == "challenge" or (
                r.status_code == 403 and ("Just a moment..." in body or "cf_chl_" in body)):
            raise ProviderError(CHALLENGE_ERROR)
        loc = re.sub(r"^https?://[^/]+", "", r.headers.get("location") or "")
        sign_in = r.status_code in (301, 302, 303) and re.match(r"^/(signin|enter|signup|signoutconfirm)", loc)
        signed_out = r.status_code == 200 and re.search(r"LibraryThing\.is_signed_in\s*=\s*0", body)
        if r.status_code == 401 or sign_in or signed_out:
            if not _retried and self.cfg.get("username") and self.cfg.get("password"):
                self.log.info("LibraryThing: session expired, signing in again")
                self._login()
                return self._req(path, form, method, ajax, extract, True, params)
            raise AuthError("LibraryThing session expired (paste fresh cookies or set username/password)")
        return r.status_code, (extract(body) if extract and r.status_code == 200 else body)

    def _err(self, code, body=None):
        return ProviderError(f"LibraryThing answered HTTP {code}")

    def book_url(self, remote_id):
        return f"{BASE}/work/{remote_id}"

    def parse_ref(self, text):
        m = re.search(r"/work/(\d+)", text or "") or re.fullmatch(r"\s*(\d+)\s*", text or "")
        return m.group(1) if m else None

    # ---- search / linking
    def search_works(self, query):
        query = " ".join((query or "").split())
        if not query:
            return []
        path = ("/ajax_newsearch.php?search=" + quote_plus(query)
                + "&searchtype=newwork_titles&page=1&sortchoice=0&optionidpotential=0&optionidreal=0")
        for _ in range(2):       # without a site session the first call only sets one
            code, body = self._req(path, method="POST", ajax=True)
            if code != 200:
                raise ProviderError(f"LibraryThing search failed (HTTP {code})")
            if not self._new_site:
                break
        m = re.search(r'"text"\s*:\s*"(.*?)"', body, re.S)
        if not m:
            raise ProviderError("Unexpected search response from LibraryThing")
        html = b64_decode(json_unescape(m.group(1)))
        if "If this error persists" in html:
            raise ProviderError("LibraryThing's search failed")
        return parse_search_results(html)

    def search(self, query):
        return self.search_works(query)

    def lookup_isbn(self, isbn):
        books = self.search_works(isbn)
        return self.finalize(books[0]) if books else None

    def finalize(self, cand):
        self._remember(cand["id"], title=cand["title"], author=(cand.get("authors") or [None])[0])
        return Match(cand["id"])

    def find(self, book, by_isbn=True, by_title=True):
        from .. import matching
        if by_isbn:
            for isbn in dict.fromkeys(filter(None, (book.isbn13, book.isbn10))):
                m = self.lookup_isbn(isbn)
                if m:
                    return m
        if by_title and book.title.strip():
            first = book.authors[0].replace(",", " ") if book.authors else ""
            books = self.search_works((book.title + " " + first).strip())
            if not books and first:                 # the author can be spelled differently there
                books = self.search_works(book.title)
            best = matching.best_match(book.title, book.authors, books)
            return self.finalize(best) if best else None
        return None

    # ---- collections
    def _ids(self):
        return {**DEFAULT_IDS, **(self._kv("collection_ids", {}) or {})}

    def _learn(self, collections):
        if not collections:
            return
        self._kv_set("collections", [{"id": c["id"], "name": c["name"]} for c in collections])
        ids = {c["key"]: c["id"] for c in collections if c.get("key")}
        if ids:
            self._kv_set("collection_ids", ids)

    def _read_collection_id(self):
        ids, chosen = self._ids(), int(self.cfg.get("read_collection_id") or 0)
        if chosen and chosen not in (ids["reading"], ids["to_read"]):
            return chosen
        return ids["library"]

    def _status_collection(self, status):
        ids = self._ids()
        return {"reading": ids["reading"], "want": ids["to_read"], "read": self._read_collection_id()}.get(status)

    def _add_collection_ids(self, status):
        lib, cid = self._ids()["library"], self._status_collection(status)
        if cid == lib:
            return [lib]
        return [cid] if status == "read" else [lib, cid]

    def _add_form(self, work_id):
        code, html = self._req("/ajax_lb_addbooks_express_open.php", ajax=True, form=[
            ("workcode", work_id), ("forceLT2", 1), ("is_lightbox_request", 1), ("is_ajax", 1)])
        if code != 200:
            raise self._err(code)
        cols = parse_collection_checkboxes(html)
        if not cols:
            raise AuthError("LibraryThing didn't accept the saved session; enter its session cookies again")
        self._learn(cols)
        return html

    def check_auth(self):
        self._add_form(TEST_WORK_ID)
        return "LibraryThing session OK"

    # ---- the user's copy
    def _search_catalog(self, work_id, term):
        term = (term or "").strip()
        if not term:
            return None
        user = self._username()
        if not user:
            raise AuthError("Not logged in to LibraryThing")
        code, links = self._req(f"/catalog_bottom.php?view={quote_plus(user)}&collection=-1&deepsearch={quote_plus(term)}",
                                extract=lambda b: "\n".join(copy_links(b)))
        if code != 200:
            raise ProviderError(f"Failed to search your LibraryThing catalog (HTTP {code})")
        return copy_in_links(links.split("\n"), work_id)

    def _find_copy(self, work_id, work):
        copy = self._search_catalog(work_id, work.get("title"))
        if copy:
            return copy
        author = work.get("author") or ""
        surname = author.split(",")[0].strip() if "," in author else (author.split() or [""])[-1]
        return self._search_catalog(work_id, surname)

    def _reading_date_rows(self, work_id, copy_id, usernum):
        code, body = self._req(f"/ajax_lb_newwork_edit_bookfields.php?work={quote_plus(str(work_id))}&book={quote_plus(str(copy_id))}"
                               f"&type=reading_dates&book_usernum={quote_plus(str(usernum))}", ajax=True,
                               form=[("forceLT2", 1), ("is_lightbox_request", 1), ("is_ajax", 1)])
        if code != 200:
            raise self._err(code)
        if "edit_type" not in body:
            raise ProviderError("LibraryThing's reading dates couldn't be read")
        rows = {}
        for tag in re.findall(r"<input[^>]*>", body):
            m = re.search(r'name="newdate([A-Za-z]+)A\[(\d+)\]"', tag)
            if m and int(m.group(2)) > 0:
                row = rows.setdefault(int(m.group(2)), {"started": "", "finished": ""})
                v = re.search(r'value="([^"]*)"', tag)
                val = htmllib.unescape(v.group(1)) if v else ""
                if m.group(1) == "started": row["started"] = val
                if m.group(1) == "ended": row["finished"] = val
        return [rows[i] for i in sorted(rows)]

    def _save_reading_date_rows(self, work_id, copy_id, usernum, rows):
        form = [("edit_work", work_id), ("edit_book", copy_id), ("book_usernum", usernum), ("edit_type", "reading_dates")]
        for i, row in enumerate(rows, 1):
            form += [(f"newdatestartedA[{i}]", row.get("started") or ""), (f"newdateendedA[{i}]", row.get("finished") or "")]
        form += [("newdatestartedA[0]", ""), ("newdateendedA[0]", "")]
        code, body = self._req("/ajax_newwork_edit_bookfields_submit.php", ajax=True, form=form)
        if code != 200:
            raise self._err(code)
        if "edit_field_reading_dates" not in body:
            raise ProviderError("LibraryThing didn't save the reading dates")

    def _copy_collections(self, copy_id):
        code, body = self._req("/ajax_collections_popup.php", ajax=True, form=[
            ("view_usernum", self._usernum() or ""), ("target", "shelfsync"), ("bookid", copy_id), ("is_ajax", 1)])
        if code != 200:
            raise self._err(code)
        allc = parse_collections_popup(body)
        if not allc:
            raise ProviderError("LibraryThing's collections couldn't be read")
        self._learn(allc)
        mine = [c for c in allc if c["checked"]]
        saved = self._ids()
        for c in mine:                       # renamed collections are known by id
            if not c["key"]:
                c["key"] = next((k for k, i in saved.items() if int(i) == c["id"]), None)
        return mine

    def _copy_status(self, work_id, copy_id, title):
        usernum = self._usernum()
        rows = self._reading_date_rows(work_id, copy_id, usernum)     # read first: redirects when signed out
        cols = self._copy_collections(copy_id)
        if not cols:
            return None
        reads = [{"started": r["started"] or None, "finished": r["finished"] or None}
                 for r in rows if r["started"] or r["finished"]]
        rid = self._read_collection_id()
        return {"id": str(copy_id), "book_id": str(work_id), "usernum": usernum, "title": title,
                "collections": cols, "reading_dates": reads,
                "status": status_for(cols, reads, rid if rid != self._ids()["library"] else None)}

    def _user_book(self, work_id):
        work_id = str(work_id)
        work = self._work(work_id)
        if work.get("copy"):
            st = self._copy_status(work_id, work["copy"], work.get("title"))
            if st:
                return st
            self._remember(work_id, copy=None)          # deleted since, or another account's
        if not work.get("title"):
            raise ProviderError("LibraryThing: no title known for this work, relink the book")
        copy = self._find_copy(work_id, work)
        if not copy:
            return {"book_id": work_id, "title": work["title"]}
        self._remember(work_id, copy=copy)
        return self._copy_status(work_id, copy, work["title"]) or {"book_id": work_id, "title": work["title"]}

    # ---- adding a work to the library
    def _best_edition(self, work_id):
        form_html = self._add_form(work_id)
        key = input_value(form_html, "memcache_key")
        if not key:
            raise ProviderError("LibraryThing's add form couldn't be read")
        code, body = self._req(f"/ajax_addbooks_express_show_bestwork.php?memcache_key={quote_plus(key)}"
                               f"&workcode={quote_plus(str(work_id))}&show_media_source=0", ajax=True,
                               form=[("fancy_type[#abx_data_info]", "full"), ("forceLT2", 1), ("is_ajax", 1)])
        if code != 200:
            raise self._err(code)
        return json_unescape(body or "")

    def _add_work(self, work_id, collection_ids):
        edition = self._best_edition(work_id)
        query = [("tags", ""), ("rating", 0), ("collection", ",".join(str(i) for i in collection_ids)),
                 ("record_id", input_value(edition, "record_id") or ""), ("source_type", input_value(edition, "source_type") or ""),
                 ("source_id", input_value(edition, "source_id") or ""), ("workcode", work_id),
                 ("isbn", input_value(edition, "isbn") or ""), ("idealbook", input_value(edition, "idealbook") or "")]
        from urllib.parse import urlencode
        code, body = self._req("/action_addbooks_express_add_book.php?" + urlencode(query), method="POST", ajax=True,
                               form=[("forceLT2", 1), ("is_ajax", 1)])
        if code != 200:
            raise self._err(code)
        if not (html_text(body) or "").startswith("Added"):
            raise ProviderError("LibraryThing couldn't add the book to your library")
        code, links = self._req("/response_list_lt2.php", ajax=True, extract=lambda b: "\n".join(copy_links(b)),
                                form=[("fragment", 1), ("limit", 5), ("forceLT2", 1), ("is_ajax", 1)])
        copy = copy_in_links(links.split("\n"), work_id) if code == 200 else None
        if not copy:
            raise ProviderError("The book was added, but its copy couldn't be found")
        self._remember(work_id, copy=copy)
        return copy

    # ---- status changes
    def _set_collection(self, copy_id, cid, add):
        code, body = self._req("/ajax_collectionsToggleBook.php", ajax=True, form=[
            ("bookid", copy_id), ("c_id", cid), ("addRemove", 1 if add else 0), ("returnUI", 1),
            ("containerID", f"collections{copy_id}"), ("excludeContainer", 1), ("is_catalogB", 0)])
        if code != 200:
            raise self._err(code)
        now_in = any(c["id"] == cid for c in parse_toggle_response(body))
        if now_in != bool(add):
            raise ProviderError("LibraryThing didn't update the collection")

    def _move_to_status_collection(self, st, status):
        keep = self._status_collection(status) if status else None
        have = lambda cid: any(c["id"] == cid for c in st.get("collections", []))
        if keep and not have(keep):
            self._set_collection(st["id"], keep, True)
            st["collections"].append({"id": keep})
        ids = self._ids(); status_ids = [ids["reading"], ids["to_read"]]
        others = sum(1 for c in st.get("collections", []) if c["id"] not in status_ids)
        if others == 0 and not keep:
            self._set_collection(st["id"], ids["library"], True)
        for cid in status_ids:
            if cid != keep and have(cid):
                self._set_collection(st["id"], cid, False)

    def _start_read(self, st):
        usernum = st.get("usernum") or self._usernum()
        rows = self._reading_date_rows(st["book_id"], st["id"], usernum)
        last = rows[-1] if rows else None
        if last and last["started"] and not last["finished"]:
            return
        rows.append({"started": time.strftime("%Y-%m-%d"), "finished": ""})
        self._save_reading_date_rows(st["book_id"], st["id"], usernum, rows)

    def _finish_read(self, st, date):
        usernum = st.get("usernum") or self._usernum()
        rows = self._reading_date_rows(st["book_id"], st["id"], usernum)
        last = rows[-1] if rows else None
        if last and last["started"] and not last["finished"]:
            last["finished"] = date
        elif last and last["finished"] == date:
            return
        else:
            rows.append({"started": "", "finished": date})
        self._save_reading_date_rows(st["book_id"], st["id"], usernum, rows)

    def set_status(self, remote_id, status, book):
        if not self._status_collection(status):
            raise ProviderError(f"LibraryThing has no {status} status")
        st = self._user_book(remote_id)
        if not st.get("id"):
            copy = self._add_work(remote_id, self._add_collection_ids(status))
            st = self._copy_status(remote_id, copy, st.get("title"))
            if not st:
                raise ProviderError("The book was added, but its copy couldn't be found")
        self._move_to_status_collection(st, status)
        if status == "reading":
            self._start_read(st)
        elif status == "read":
            date = book.finished_on[:10] if book.finished_on else time.strftime("%Y-%m-%d")
            self._finish_read(st, date)

    def normalize_rating(self, rating):
        return int(float(rating) * 2) / 2

    def read_state(self, remote_id):
        st = self._user_book(remote_id)
        state = {"status": st.get("status") if st.get("id") else None, "rating": None, "finished_dates": set(), "known": set()}
        if st.get("id"):
            for row in st.get("reading_dates") or []:
                m = re.match(r"^(\d{4}-\d{2}-\d{2})", row.get("finished") or "")
                if m:
                    state["finished_dates"].add(m.group(1))
            if state["status"] == "read":
                state["known"].add("finished_on")
        return state

    def set_progress(self, remote_id, book, by_pages, remote_pages):
        return        # LibraryThing keeps no reading progress

    def set_rating(self, remote_id, rating, book):
        half = int(float(rating) * 2)
        if half < 1:
            return
        st = self._user_book(remote_id)
        if not st.get("id"):
            raise ProviderError("The book isn't in your LibraryThing library")
        code, _ = self._req("/ajax_setBookRating.php", ajax=True, form=[
            ("uid", "shelfsync"), ("book", st["id"]), ("editable", 1), ("container", "rate-shelfsync"),
            ("style", 306), ("rating", half), ("is_catalogB", 0), ("is_addbooksB", 0)])
        if code != 200:
            raise self._err(code)
