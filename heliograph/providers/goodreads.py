"""Goodreads via a replayed browser session, ported from ShelfSync's goodreads/api.lua + provider.lua.

Cookie handling (same as the plugin):
  * no cookie saved           -> GET  <refresher>/cookie   (the refresher's cached cookie), saved to config
  * WAF challenge / signed out -> POST <refresher>/refresh  (75s timeout), saved, request retried once
  * cookies Goodreads sets mid-session are kept in memory and replayed
Writes: shelf (/shelf/add_to_shelf), progress (/user_status.json), rating (/review/rate/<id>, quarter stars),
and the "date read" through the Next.js review editor's server action.
"""
import html as htmllib, json, re, time
from urllib.parse import quote, urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from .base import Provider, AuthError, ProviderError, Match, mapped_page, clean_cover

BASE = "https://www.goodreads.com"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:154.0) Gecko/20100101 Firefox/154.0"
SHELF = {"want": "to-read", "reading": "currently-reading", "read": "read",
         "paused": "paused", "dnf": "did-not-finish"}
REFRESHER_TIMEOUT = 75
REFRESH_TRIES = 3          # attempts at /refresh before giving up on a WAF/expired-session response
REFRESH_WAIT = 10          # seconds between those attempts
MAX_HOPS = 5
CSRF_TTL = 15 * 60         # reuse the homepage's CSRF token this long instead of re-fetching it before every write
PAGE_TTL = 120             # a book page fetched for matching is reused by the read-back right after it
JSON = json.JSONDecoder()


# ---------- small helpers (ported 1:1 where the plugin has an equivalent)
def merge_cookies(header, new):
    jar = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            jar[k] = v
    jar.update(new)
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _is_goodreads(url):
    p = urlparse(url)
    return p.scheme == "https" and p.netloc.lower() == "www.goodreads.com"


def _sign_in_url(u):
    u = (u or "").lower()
    return "/user/sign_in" in u or "signin" in u


def _sign_in_page(body):
    b = (body or "").lower()
    return "something wrong with your goodreads cookie" in b or ("/user/sign_in" in b and 'name="email"' in b)


def quarter_stars(rating):
    return int(float(rating) * 4 + 1e-9) / 4      # Goodreads takes 0.25-5 in quarter steps


def extract_csrf(html):
    m = (re.search(r'<meta\s+[^>]*name=["\']csrf-token["\']\s+[^>]*content=["\']([^"\']+)["\']', html)
         or re.search(r'<meta\s+[^>]*content=["\']([^"\']+)["\']\s+[^>]*name=["\']csrf-token["\']', html)
         or re.search(r'name=["\']authenticity_token["\']\s+value=["\']([^"\']+)["\']', html))
    return m.group(1) if m else None


def find_book_cache(data, query):
    if not isinstance(data, (dict, list)):
        return None
    if isinstance(data, dict):
        if ((data.get("ROOT_QUERY") or {}).get(query) or {}).get("__ref"):
            return data
        children = data.values()
    else:
        children = data
    for v in children:
        c = find_book_cache(v, query)
        if c:
            return c
    return None


def scripts(html, **attrs):
    """The page's <script> elements, found by a real HTML parser (a regex misses variants like </script >)."""
    return BeautifulSoup(html or "", "html.parser").find_all("script", **attrs)


def script_texts(html, **attrs):
    return [s.string or "" for s in scripts(html, **attrs)]


def book_page_pages(html, book_id):
    """numPages from the page's Apollo cache (findUserBook's scrape)."""
    query = 'getBookByLegacyId({"legacyId":"%s"})' % book_id
    for script in script_texts(html):
        if "getBookByLegacyId" not in script:
            continue
        try:
            data = json.loads(script)
        except ValueError:
            continue
        cache = find_book_cache(data, query)
        if cache:
            ref = cache["ROOT_QUERY"][query]["__ref"]
            n = ((cache.get(ref) or {}).get("details") or {}).get("numPages")
            return int(n) if isinstance(n, (int, float)) and n > 0 else None
    return None


def book_apollo(html, book_id):
    query = 'getBookByLegacyId({"legacyId":"%s"})' % book_id
    for script in script_texts(html):
        if "getBookByLegacyId" not in script:
            continue
        try:
            data = json.loads(script)
        except ValueError:
            continue
        cache = find_book_cache(data, query)
        if cache:
            return cache, cache.get(cache["ROOT_QUERY"][query]["__ref"]) or {}
    return None, None


SHELF_STATUS = {"to-read": "want", "currently-reading": "reading", "read": "read"}


def viewer_shelf(html, book_id):
    """The signed-in user's shelf (and rating, if the page carries it) for a book, from the page's Apollo cache."""
    cache, book = book_apollo(html, book_id)
    if cache is None or "viewerShelving" not in book:
        return {"readable": False}
    sh = book["viewerShelving"]
    if sh is None:                                   # JSON null: not on any shelf
        return {"readable": True, "status": None, "rating": None, "rating_known": False}
    node = cache.get((sh or {}).get("__ref")) or sh
    shelf = (node.get("shelf") or {}).get("name")
    if not isinstance(shelf, str) or not shelf:
        return {"readable": False}
    review = node.get("review")
    if isinstance(review, dict) and "__ref" in review:
        review = cache.get(review["__ref"])
    rating, known = None, False
    for holder in (node, review):
        if isinstance(holder, dict) and "rating" in holder:
            known = True
            r = holder["rating"]
            rating = float(r) if isinstance(r, (int, float)) and r > 0 else None
            break
    return {"readable": True, "status": SHELF_STATUS.get(shelf), "rating": rating, "rating_known": known}


def parse_search_results(html):
    """Results as [{id,title,authors,cover}]. The Next.js page keeps each cover in a separate section from its
    title, in the same order, so they're zipped by position (as the plugin does)."""
    kca = [m.start() for m in re.finditer(r'data-testid="book-item-kca://book/', html)]
    out, seen = [], set()
    for idx, m in enumerate(re.finditer(r'data-testid="book-item-title"><a href="/book/show/(\d+)[^"]*">(.*?)</a>', html, re.S)):
        bid = m.group(1)
        if bid in seen:
            continue
        seen.add(bid)
        a = re.search(r'data-testid="name">([^<]+)</span>', html[m.end(): m.end() + 600])
        cover = None
        if idx < len(kca):
            c = re.search(r'data-testid="responsive-image"[^>]*?src="([^"]+)"', html[kca[idx]: kca[idx] + 1500])
            cover = clean_cover(htmllib.unescape(c.group(1))) if c else None
        out.append({"id": bid, "title": htmllib.unescape(m.group(2)).strip(),
                    "authors": [htmllib.unescape(a.group(1))] if a else ["Unknown Author"], "cover": cover})
    return out


# ---------- Next.js review-editor helpers (date read)
def next_flight(html):
    rows = []
    for script in script_texts(html):
        pos = 0
        while True:
            i = script.find("self.__next_f.push", pos)
            if i < 0:
                break
            a = script.find("[", i + len("self.__next_f.push"))
            if a < 0:
                break
            try:
                payload, end = JSON.raw_decode(script, a)
            except ValueError:
                break
            if isinstance(payload, list) and len(payload) > 1 and payload[0] == 1 and isinstance(payload[1], str):
                rows.append(payload[1])
            pos = end
    return "\n".join(rows) or None


def json_property(text, name, typ=None):
    needle, pos = f'"{name}":', 0
    while True:
        i = text.find(needle, pos)
        if i < 0:
            return None
        j = i + len(needle)
        while j < len(text) and text[j].isspace():
            j += 1
        try:
            value, _ = JSON.raw_decode(text, j)
            if typ is None or isinstance(value, typ):
                return value
        except ValueError:
            pass
        pos = i + len(needle)


def parse_review_state(html):
    flight = next_flight(html)
    if not flight:
        return None
    sessions = json_property(flight, "readingSessions", list) or []
    book_id = ((sessions[0].get("bookId") if sessions and isinstance(sessions[0], dict) else None)
               or (re.search(r'"bookId":"(kca://book/.*?)"', flight) or [None, None])[1]
               or (re.search(r'"id":"(kca://book/.*?)"', flight) or [None, None])[1])
    if not book_id:
        return None
    first = lambda *names: next((v for n in names if (v := json_property(flight, n, str)) is not None), "")
    return {
        "book_id": book_id, "reading_sessions": sessions,
        "review_text": first("userFormattedText", "reviewText"),
        "private_notes": first("initialPrivateNotes", "privateNotes"),
        "post_to_blog": json_property(flight, "initialPostToBlog", bool) is True,
        "add_to_feed": json_property(flight, "initialAddToUpdateFeed", bool) is not False,
        "spoiler_status": json_property(flight, "spoilerStatus", bool) is True,
        "is_owned_edition": json_property(flight, "isAlreadyOwned", bool) is True,
    }


def chunk_paths(html):
    paths, seen = [], set()
    for tag in scripts(html, src=True):
        src = (tag.get("src") or "").strip()                 # the parser has already decoded &amp; etc.
        mm = re.match(r"^https://www\.goodreads\.com(/_next/static/chunks/[^?#]+)$", src)
        path = mm.group(1) if mm else src
        unrelated = ("/polyfills-" in path or "/webpack-" in path or "/app/not-found-" in path
                     or ("/app/review/edit/" in path and "/error-" in path))
        if path.startswith("/_next/static/chunks/") and path.endswith(".js") and ".." not in path \
                and not unrelated and path not in seen:
            paths.append(path); seen.add(path)
    return paths


def find_server_action_id(source, name):
    label, frm = f'"{name}"', 0
    while True:
        at = source.find(label, frm)
        if at < 0:
            return None
        prefix = source[max(0, at - 220): at]
        marker = prefix.rfind("createServerReference")
        if marker >= 0:
            m = re.search(r"createServerReference[^(]*\(\s*[\"']([0-9a-fA-F]+)[\"']", prefix[marker:] + label)
            if m and len(m.group(1)) >= 32:
                return m.group(1)
        frm = at + len(label)


def date_parts(v):
    if not isinstance(v, dict):
        return None
    try:
        y, m, d = int(v["year"]), int(v["month"]), int(v["day"])
    except (KeyError, TypeError, ValueError):
        return None
    return (y, m, d) if y >= 1 and 1 <= m <= 12 and 1 <= d <= 31 else None


def prepare_finished_sessions(sessions, book_id, finished, stamp_ms):
    """-> (new_sessions, already_has_date, error)"""
    fd = date_parts(finished)
    out = list(sessions)
    for s in sessions:
        if isinstance(s, dict) and date_parts(s.get("endedDate")) == fd:
            return out, True, None
    for i, s in enumerate(sessions):
        if (isinstance(s, dict) and s.get("bookId") == book_id and s.get("state") == "READING"
                and not date_parts(s.get("endedDate"))
                and not ((date_parts(s.get("startedDate")) or (0, 0, 0)) > fd)):
            out[i] = {**s, "endedDate": finished}
            return out, False, None
    if len(sessions) >= 25:
        return None, False, "Goodreads already has the maximum number of reading sessions"
    out.append({"__typename": "ReadingSession", "id": f"new-{stamp_ms}", "bookId": book_id,
                "state": "COMPLETED", "startedDate": finished, "endedDate": finished})
    return out, False, None


def next_action_ok(response):
    m = re.search(r'"errors":"\$Q(\w+)"', response)
    if not m:
        return True
    row = re.search(r"[\r\n]" + re.escape(m.group(1)) + r":([^\r\n]+)", response)
    if not row:
        return False
    try:
        errors = json.loads(row.group(1))
    except ValueError:
        return False
    return isinstance(errors, list) and not errors


class Goodreads(Provider):
    key, label = "goodreads", "Goodreads"
    HOME = "https://www.goodreads.com/"
    SUPPORTED = ("want", "reading", "read", "paused", "dnf")   # paused/dnf are write-only on Goodreads

    def __init__(self, config, log):
        super().__init__(config, log)
        self._jar = None          # (saved cookie it belongs to, merged cookie)
        self._action_cache = None
        self._last_request = 0.0  # monotonic time of the last request to Goodreads (pacing)
        self._cooldown_until = 0.0
        self._csrf = None         # (token, fetched at)
        self._page_cache = {}     # book id -> (fetched at, response)

    def _pace(self):
        """Keep a steady gap between Goodreads requests. Rapid bursts (an import does ~10 per book) are what
        set off its WAF challenges, so this is the main way to avoid them."""
        gap = max(0.0, float(self.cfg.get("request_delay_seconds") or 0))
        wait = self._last_request + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _check_cooldown(self):
        left = self._cooldown_until - time.monotonic()
        if left > 0:
            raise AuthError(f"Goodreads: pausing after a WAF challenge the refresher couldn't clear; "
                            f"trying again in {int(left // 60) + 1} min")

    # ---- cookie plumbing
    def _refresher(self, force):
        base = (self.cfg.get("cookie_refresh_url") or "").rstrip("/")
        if not base:
            return None
        tok = self.cfg.get("cookie_refresh_token")
        url = base + ("/refresh" if force else "/cookie")
        try:
            r = requests.request("POST" if force else "GET", url, headers={"X-Auth-Token": tok} if tok else {},
                                 timeout=REFRESHER_TIMEOUT if force else 10)
        except requests.RequestException as e:
            self.log.warning("Goodreads cookie refresher %s unreachable: %s", url, e)
            return None
        cookie = r.text.strip() if r.status_code == 200 else ""
        if not cookie:
            hints = {401: "check Cookie Auto-Refresh Token matches REFRESHER_AUTH_TOKEN",
                     404: "check the Cookie Auto-Refresh URL", 503: "the refresher's browser isn't logged in (noVNC)"}
            self.log.warning("Goodreads refresher %s returned HTTP %s - %s", url, r.status_code,
                             hints.get(r.status_code, "check the refresher logs"))
            return None
        self.cfg["cookie"] = cookie
        self.config.save()
        return cookie

    def _refresh_with_retry(self):
        """The refresher can time out while its browser is busy (or sitting behind a WAF page); give it a few chances."""
        tries, wait = REFRESH_TRIES, REFRESH_WAIT
        try:
            for i in range(tries):
                self.waiting_on = f"waiting for the cookie refresher, attempt {i + 1}/{tries}"   # shown in "busy" messages
                fresh = self._refresher(True)
                if fresh:
                    return fresh
                if i + 1 < tries:
                    self.log.info("Goodreads: cookie refresh failed, trying again in %ds (%d/%d)", wait, i + 2, tries)
                    time.sleep(wait)
            return None
        finally:
            self.waiting_on = None

    def _current_cookie(self):
        saved = self.cfg.get("cookie") or self._refresher(False) or ""
        if self._jar and self._jar[0] == saved:
            return self._jar[1]
        self._jar = (saved, saved)
        return saved

    def _headers(self, custom=None):
        h = {"User-Agent": self.cfg.get("user_agent") or UA,
             "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
             "Accept-Language": "en-US,en;q=0.9", "Referer": BASE + "/", "Sec-Fetch-Site": "same-origin",
             "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Sec-Fetch-User": "?1",
             "Upgrade-Insecure-Requests": "1", "DNT": "1"}
        h.update(custom or {})
        return h

    def _req(self, method, url, data=None, params=None, headers=None):
        self._check_cooldown()
        if method != "GET":
            self._page_cache.clear()                      # a write may change what the cached pages show
        cookie = self._current_cookie()
        if not cookie:
            raise AuthError("Goodreads: no cookie set and the refresher gave none")
        sent_saved = self.cfg.get("cookie", "")
        refreshed, cur, meth = False, url, method
        r = None
        for hop in range(MAX_HOPS + 1):
            h = {**self._headers(headers), "Cookie": cookie}
            self._pace()
            r = requests.request(meth, cur, headers=h, data=data if hop == 0 else None,
                                 params=params if hop == 0 else None, allow_redirects=False, timeout=30)
            if _is_goodreads(cur) and r.cookies.get_dict():
                cookie = merge_cookies(cookie, r.cookies.get_dict())
                if self.cfg.get("cookie", "") == sent_saved:
                    self._jar = (sent_saved, cookie)
            loc = r.headers.get("location")
            waf = bool(r.headers.get("x-amzn-waf-action")) or r.status_code == 202
            expired = (r.status_code in (401, 403) or _sign_in_url(loc) or _sign_in_url(cur)
                       or _sign_in_page(r.text))
            if (waf or expired) and _is_goodreads(cur):
                if not refreshed:
                    refreshed = True
                    self.log.info("Goodreads: %s, asking the cookie refresher", "WAF challenge" if waf else "expired session")
                    fresh = self._refresh_with_retry()
                    if fresh and fresh != cookie:
                        cookie, self._jar = fresh, (fresh, fresh)
                        sent_saved = fresh
                        self._csrf = None                 # the token belongs to the old session
                        continue
                if waf:
                    mins = max(0.0, float(self.cfg.get("waf_cooldown_minutes") or 0))
                    if mins:                              # stop instead of retrying into the same challenge
                        self._cooldown_until = time.monotonic() + mins * 60
                        self.log.warning("Goodreads: WAF challenge not cleared - leaving Goodreads alone for %g min", mins)
                raise AuthError("Goodreads rejected the cookie (WAF challenge or signed out)"
                                + ("" if self.cfg.get("cookie_refresh_url") else "; no Cookie Auto-Refresh URL configured"))
            is_redirect = r.status_code in (301, 302, 303, 307, 308) or (r.status_code == 200 and loc and loc != cur)
            if is_redirect and loc and hop < MAX_HOPS and meth in ("GET", "HEAD"):
                nxt = urljoin(cur, loc)
                if not _is_goodreads(nxt):
                    break
                cur = nxt
                continue
            break
        r.final_url = cur
        return r

    def _csrf_token(self, fresh=False):
        if not fresh and self._csrf and time.monotonic() - self._csrf[1] < CSRF_TTL:
            return self._csrf[0]
        r = self._req("GET", BASE + "/")
        tok = extract_csrf(r.text)
        if not tok:
            raise ProviderError("Goodreads: CSRF token not found on the homepage")
        self._csrf = (tok, time.monotonic())
        return tok

    def _post(self, url, data, custom, params=None, retry_403=True):
        r = self._req("POST", url, data=data, params=params, headers={**custom, "X-CSRF-Token": self._csrf_token()})
        if r.status_code == 403 and retry_403:           # plugin: retry once with a fresh session token
            r = self._req("POST", url, data=data, params=params,
                          headers={**custom, "X-CSRF-Token": self._csrf_token(fresh=True)})
        if not 200 <= r.status_code < 300:
            raise ProviderError(f"Goodreads POST {urlparse(url).path} -> HTTP {r.status_code}")
        return r

    XHR = {"X-Requested-With": "XMLHttpRequest", "Origin": BASE, "Sec-Fetch-Site": "same-origin",
           "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}

    def book_url(self, remote_id):
        return f"{BASE}/book/show/{remote_id}"

    def parse_ref(self, text):
        m = re.search(r"/book/show/(\d+)", text or "") or re.fullmatch(r"\s*(\d+)\s*", text or "")
        return m.group(1) if m else None

    # ---- lookup
    def check_auth(self):
        r = self._req("GET", BASE + "/review/list")
        if r.status_code == 200 and "csrf-token" in r.text:
            return "Goodreads cookie OK"
        raise AuthError(f"Goodreads: unexpected response checking the session (HTTP {r.status_code})")

    def _book_page(self, book_id, keep=False):
        """GET /book/show/<id>. Matching keeps the page (keep=True) so the read-back that follows right after uses
        it instead of fetching it again; that read-back consumes it, so later reads always see a fresh page."""
        hit = self._page_cache.pop(str(book_id), None)
        if hit and time.monotonic() - hit[0] < PAGE_TTL:
            return hit[1]
        r = self._req("GET", f"{BASE}/book/show/{book_id}")
        if keep and r.status_code == 200:
            if len(self._page_cache) > 20:
                self._page_cache.clear()
            self._page_cache[str(book_id)] = (time.monotonic(), r)
        return r

    def _pages(self, book_id):
        try:
            r = self._book_page(book_id, keep=True)
            return book_page_pages(r.text, book_id) if r.status_code == 200 else None
        except (requests.RequestException, ProviderError):
            return None

    def search(self, query):
        r = self._req("GET", BASE + "/search", params={"q": query})
        if r.status_code != 200:
            raise ProviderError(f"Goodreads search failed (HTTP {r.status_code})")
        m = re.search(r"/book/show/(\d+)", r.final_url)      # an exact single match redirects to the book page
        if m:
            ld = next(iter(script_texts(r.text, type="application/ld+json")), "")
            t = re.search(r'"name":"(.*?)","image"', ld)
            a = re.search(r'"author":\[\{"@type":"Person","name":"(.*?)"', ld)
            if not t:
                return []
            img = re.search(r'"image":"(.*?)","bookFormat"', ld)
            return [{"id": m.group(1), "title": htmllib.unescape(t.group(1)),
                     "authors": [htmllib.unescape(a.group(1))] if a else ["Unknown Author"],
                     "cover": clean_cover(htmllib.unescape(img.group(1))) if img else None}]
        return parse_search_results(r.text)

    def lookup_isbn(self, isbn):
        res = self.search(isbn)
        return Match(res[0]["id"], self._pages(res[0]["id"])) if res else None

    def finalize(self, cand):
        return Match(cand["id"], self._pages(cand["id"]))

    def normalize_rating(self, rating):
        return quarter_stars(rating)

    def read_state(self, remote_id):
        r = self._book_page(remote_id)
        if r.status_code != 200:
            raise ProviderError(f"Goodreads book page HTTP {r.status_code}")
        v = viewer_shelf(r.text, remote_id)
        if not v["readable"]:
            raise ProviderError("Couldn't read your shelf from the Goodreads book page")
        state = {"status": v["status"], "rating": v["rating"], "finished_dates": set(),
                 "known": {"rating"} if v["rating_known"] else set()}
        if v["status"] == "read":
            ed = self._req("GET", f"{BASE}/review/edit/{remote_id}")
            rs = parse_review_state(ed.text) if ed.status_code == 200 else None
            if rs:
                state["known"].add("finished_on")
                for sess in rs["reading_sessions"]:
                    d = date_parts(sess.get("endedDate")) if isinstance(sess, dict) else None
                    if d:
                        state["finished_dates"].add(f"{d[0]:04d}-{d[1]:02d}-{d[2]:02d}")
        return state

    # ---- writes
    def set_status(self, remote_id, status, book):
        self._post(BASE + "/shelf/add_to_shelf", {"book_id": remote_id, "name": SHELF[status], "a": ""}, {
            **self.XHR, "X-Prototype-Version": "1.7", "Referer": BASE + "/",
            "Accept": "text/javascript, text/html, application/xml, text/xml, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        if status == "read":      # plugin's onMarkedFinished -> setDateFinished; best effort, like its pending-retry
            try:
                self.set_date_finished(remote_id, book.finished_on)
            except (ProviderError, requests.RequestException, ValueError) as e:
                self.log.warning("Goodreads: finish date for '%s' not saved: %s", book.title, e)

    def set_progress(self, remote_id, book, by_pages, remote_pages):
        page = mapped_page(book, remote_pages)
        if by_pages and page is not None:
            field, val = "user_status[page]", page
        elif book.percent is not None:
            field, val = "user_status[percent]", book.percent
        else:
            return
        self._post(BASE + "/user_status.json",
                   {"user_status[book_id]": remote_id, field: val, "user_status[body]": ""},
                   {**self.XHR, "Accept": "*/*", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})

    def set_rating(self, remote_id, rating, book):
        stars = quarter_stars(rating)
        if not 0.25 <= stars <= 5:
            self.log.warning("Goodreads: rating %s out of range, skipped", rating)
            return
        self._post(BASE + f"/review/rate/{remote_id}", "",
                   {"X-Requested-With": "XMLHttpRequest", "Accept": "*/*", "Origin": BASE,
                    "Referer": BASE + f"/review/edit/{remote_id}",
                    "Content-Type": "application/x-www-form-urlencoded"},
                   params={"no_lightbox": "true", "queue": "false", "stars_click": "true",
                           "rating": f"{stars:g}", "ref": "undefined"})

    # ---- date read (Next.js server action)
    def _action_id(self, html, referer):
        paths = chunk_paths(html)
        if not paths:
            return None
        key = "\n".join(paths)
        if not self._action_cache or self._action_cache["key"] != key:
            self._action_cache = {"key": key, "id": None, "checked": set()}
        c = self._action_cache
        if c["id"]:
            return c["id"]
        for path in reversed(paths):
            if path in c["checked"]:
                continue
            r = self._req("GET", BASE + path, headers={
                "Accept": "*/*", "Referer": referer, "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "script"})
            if r.status_code == 200:
                c["checked"].add(path)
                c["id"] = find_server_action_id(r.text, "submitReviewFormAction") or c["id"]
            elif r.status_code == 202:
                return None
            if c["id"]:
                return c["id"]
        return None

    def set_date_finished(self, book_id, finished_on=None):
        if finished_on and re.match(r"^\d{4}-\d{2}-\d{2}", finished_on):
            y, m, d = (int(x) for x in finished_on[:10].split("-"))
        else:
            t = time.localtime()
            y, m, d = t.tm_year, t.tm_mon, t.tm_mday
        finished = {"__typename": "NullableDate", "year": y, "month": m, "day": d}
        edit_url = f"{BASE}/review/edit/{book_id}"
        r = self._req("GET", edit_url)
        if r.status_code != 200:
            raise ProviderError(f"review editor HTTP {r.status_code}")
        state = parse_review_state(r.text)
        if not state:
            raise ProviderError("couldn't read the Goodreads review editor state")
        sessions, already, err = prepare_finished_sessions(state["reading_sessions"], state["book_id"],
                                                           finished, int(time.time() * 1000))
        if err:
            raise ProviderError(err)
        if already:
            return True
        action = self._action_id(r.text, edit_url)
        if not action:
            raise ProviderError("couldn't identify Goodreads' review-save action")
        payload = {"bookId": state["book_id"], "reviewText": state["review_text"],
                   "spoilerStatus": state["spoiler_status"], "isOwnedEdition": state["is_owned_edition"],
                   "privateNotes": state["private_notes"], "postToBlog": state["post_to_blog"],
                   "addToUpdateFeed": state["add_to_feed"], "readingSessions": sessions,
                   "initialReadingSessions": state["reading_sessions"]}
        tree = ('["",{"children":["review",{"children":["edit",{"children":[["id","%s","d"],'
                '{"children":["__PAGE__",{},null,null]},null,null]}]},null,null]},null,null,true]' % book_id)
        resp = self._req("POST", edit_url, data=json.dumps([payload, "/review/edit/[id]"], separators=(",", ":")).encode(),
                         headers={"Accept": "text/x-component", "Content-Type": "text/plain;charset=UTF-8",
                                  "Next-Action": action, "Next-Router-State-Tree": quote(tree, safe=""),
                                  "Referer": edit_url, "Origin": BASE, "Sec-Fetch-Site": "same-origin",
                                  "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"})
        if resp.status_code != 200 or not next_action_ok(resp.text):
            raise ProviderError(f"Goodreads didn't confirm the finish date (HTTP {resp.status_code})")
        v = self._req("GET", edit_url)
        vs = parse_review_state(v.text) if v.status_code == 200 else None
        if vs and any(isinstance(s, dict) and date_parts(s.get("endedDate")) == date_parts(finished)
                      for s in vs["reading_sessions"]):
            return True
        raise ProviderError("Goodreads didn't confirm the finish date")
