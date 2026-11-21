"""The StoryGraph via session cookies, ported from ShelfSync's storygraph/api.lua + provider.lua."""
import html as htmllib, re
from html.parser import HTMLParser
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from .base import Provider, AuthError, ProviderError, Match, mapped_page, clean_cover

BASE = "https://app.thestorygraph.com"
STATUS = {"want": "to-read", "reading": "currently-reading", "read": "read",
          "paused": "paused", "dnf": "did-not-finish"}
JS_ACCEPT = "text/javascript, application/javascript, application/ecmascript, application/x-ecmascript, */*; q=0.01"


class _Forms(HTMLParser):
    """Collects each <form>'s fields the way a browser would submit them."""
    SKIP = {"submit", "button", "image", "file", "reset"}

    def __init__(self):
        super().__init__(); self.forms, self.cur, self.sel, self.ta = [], None, None, None
    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag == "form":
            self.cur = {"action": a.get("action") or "", "fields": []}; self.forms.append(self.cur)
        elif self.cur is None:
            return
        elif tag == "input" and a.get("name"):
            t = (a.get("type") or "text").lower()
            if t in self.SKIP or (t in ("radio", "checkbox") and "checked" not in a): return
            self.cur["fields"].append([a["name"], a.get("value") or ""])
        elif tag == "textarea" and a.get("name"):
            self.ta = [a["name"], []]
        elif tag == "select" and a.get("name"):
            self.sel = {"name": a["name"], "first": None, "chosen": None}
        elif tag == "option" and self.sel is not None:
            v = a.get("value", "")
            if self.sel["first"] is None: self.sel["first"] = v
            if "selected" in a: self.sel["chosen"] = v
    def handle_data(self, d):
        if self.ta: self.ta[1].append(d)
    def handle_endtag(self, tag):
        if tag == "textarea" and self.ta and self.cur is not None:
            self.cur["fields"].append([self.ta[0], "".join(self.ta[1])]); self.ta = None
        elif tag == "select" and self.sel and self.cur is not None:
            s = self.sel; self.cur["fields"].append([s["name"], s["chosen"] if s["chosen"] is not None else (s["first"] or "")]); self.sel = None
        elif tag == "form":
            self.cur = None


def parse_forms(html):
    p = _Forms(); p.feed(html); return p.forms


def star_fields(rating):
    total = int(float(rating) * 100 + 0.5)
    whole, dec = divmod(total, 100)
    return str(whole), ("" if dec == 0 else str(dec))     # same encoding the plugin posts


def _format_of(scope, book_id):
    nodes = scope.select(f".edition-info[data-book-id='{book_id}'] p") or scope.select(".edition-info p")
    for p in nodes:
        t = p.get_text(" ", strip=True)
        if "Format:" in t:
            return re.sub(r".*Format:\s*", "", t).strip()
    return ""


def parse_search(html):
    """Search results minus audio editions, as findBooks does."""
    soup, out, seen = BeautifulSoup(html, "html.parser"), [], set()
    for el in soup.select(".book-title-author-and-series"):
        a = el.select_one("a[href^='/books/']")
        if not a:
            continue
        m = re.match(r"/books/([^/?]+)", a["href"])
        if not m or m.group(1) in seen:
            continue
        bid = m.group(1); seen.add(bid)
        au = el.select_one("a[href^='/authors/']")
        pane = el.parent
        if pane is not None and "audio" in _format_of(pane, bid).lower():
            continue
        pages = None
        info = pane.select_one("p[class*='text-xs']") if pane is not None else None
        pm = re.search(r"(\d+) pages", info.get_text(" ")) if info else None
        if pm:
            pages = int(pm.group(1))
        img = el.parent.parent.select_one("img") if el.parent is not None and el.parent.parent is not None else None
        out.append({"id": bid, "cover": clean_cover(img.get("src") if img else None, BASE),
                    "title": " ".join(htmllib.unescape(a.get_text()).split()),
                    "authors": [" ".join(htmllib.unescape(au.get_text()).split())] if au else ["Unknown Author"],
                    "pages": pages})
    return out


def parse_book_page(html, book_id):
    """Status / pages / format / review link from a /books/<id> page (findUserBook's scrape)."""
    soup = BeautifulSoup(html, "html.parser")
    m = re.search(r'class="read-status-label[^"]*"[^>]*>\s*([^<]*?)\s*</button>', html)
    text = htmllib.unescape(m.group(1)).lower() if m else ""
    status = None
    if "currently reading" in text or "rereading" in text: status = "reading"
    elif "to-read" in text or "to read" in text: status = "want"
    elif "paused" in text: status = "paused"
    elif "did not finish" in text: status = "dnf"
    elif text == "read" or text.endswith(" read"): status = "read"
    pm = (re.search(r'name="read_status\[book_num_of_pages\]"\s+[^>]*value="([^"]+)"', html)
          or re.search(r'value="([^"]+)"\s+[^>]*name="read_status\[book_num_of_pages\]"', html)
          or re.search(r'class="read-status-book-num-of-pages"\s+[^>]*value="([^"]+)"', html))
    pages = int(pm.group(1)) if pm and pm.group(1).isdigit() else 0
    fmt = _format_of(soup, book_id)
    other, review = None, None
    for a in soup.select("a"):
        href, t = a.get("href") or "", a.get_text(" ", strip=True).lower()
        if (not other and re.match(r"^/books/[^/]+$", href)
                and ("want to read another edition" in t or "currently reading another edition" in t)):
            other = re.match(r"/books/([^/?]+)", href).group(1)
        if not review and re.search(r"see review|add a review|add review", t):
            review = href
    return {"has_button": bool(m), "status": status, "pages": pages, "audio": "audio" in fmt.lower(),
            "other_edition": other, "review_href": review, "csrf": _csrf(html)}


def _csrf(html):
    m = (re.search(r'<meta\s+[^>]*name=["\']csrf-token["\']\s+[^>]*content=["\']([^"\']+)["\']', html)
         or re.search(r'<meta\s+[^>]*content=["\']([^"\']+)["\']\s+[^>]*name=["\']csrf-token["\']', html)
         or re.search(r'name=["\']authenticity_token["\']\s+[^>]*value=["\']([^"\']+)["\']', html))
    return m.group(1) if m else None


class StoryGraph(Provider):
    key, label = "storygraph", "StoryGraph"
    HOME = "https://app.thestorygraph.com/"
    SUPPORTED = ("want", "reading", "read", "paused", "dnf")

    def _cookie(self):
        s, t = self.cfg.get("session_cookie"), self.cfg.get("remember_user_token")
        if not s:
            raise AuthError("StoryGraph: _storygraph_session not set")
        return f"remember_user_token={t or ''}; cookies_popup_seen=yes; plus_popup_seen=yes; _storygraph_session={s}"

    def _headers(self, extra=None):
        h = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/147.0.0.0 Safari/537.36",
             "Cookie": self._cookie(), "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
             "Accept-Language": "en", "Origin": BASE, "DNT": "1", "Sec-Fetch-Dest": "empty",
             "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
        h.update(extra or {})
        return h

    def _req(self, method, url, data=None, params=None, headers=None):
        r = requests.request(method, urljoin(BASE, url), headers=self._headers(headers), data=data,
                             params=params, allow_redirects=(method == "GET"), timeout=30)
        for resp in [*r.history, r]:                       # keep a refreshed session cookie, like the plugin
            new = resp.cookies.get("_storygraph_session")
            if new and new != self.cfg.get("session_cookie"):
                self.cfg["session_cookie"] = new; self.config.save()
        redirect = (r.headers.get("location") or "") + " " + (r.headers.get("turbo-location") or "")
        if r.status_code == 401 or (method == "GET" and "/users/sign_in" in r.url):
            raise AuthError("StoryGraph rejected the cookies (expired?)")
        r.sign_in_redirect = "/users/sign_in" in redirect
        return r

    def _page(self, rid):
        r = self._req("GET", f"/books/{rid}")
        if r.status_code != 200:
            raise ProviderError(f"StoryGraph: book page HTTP {r.status_code}")
        info = parse_book_page(r.text, rid)
        if not info["csrf"]:
            raise ProviderError("StoryGraph: CSRF token not found")
        return info

    def _xhr(self, rid, csrf):
        return {"X-CSRF-Token": csrf, "X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE}/books/{rid}",
                "Accept": JS_ACCEPT}

    def book_url(self, remote_id):
        return f"{BASE}/books/{remote_id}"

    def parse_ref(self, text):
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", (text or "").lower())
        return m.group(1) if m else None

    # ---- lookup / linking
    def check_auth(self):
        r = self._req("GET", "/")
        if r.status_code != 200:
            raise AuthError(f"StoryGraph: HTTP {r.status_code}")
        return "StoryGraph cookies OK"

    def search(self, query):
        r = self._req("GET", "/browse", params={"search_term": query + " "})
        if r.status_code != 200:
            raise ProviderError(f"StoryGraph search failed (HTTP {r.status_code})")
        return parse_search(r.text)

    def lookup_isbn(self, isbn):
        res = self.search(isbn)
        return self.finalize(res[0]) if res else None

    def finalize(self, cand):
        """linkBook: follow to the edition you actually have a status on; refuse audio editions."""
        rid = cand["id"]
        info = self._page(rid)
        if not info["has_button"] and info["other_edition"] and info["other_edition"] != rid:
            other = self._page(info["other_edition"])
            if other["status"] and not other["audio"]:
                rid, info = info["other_edition"], other
        if info["audio"]:
            self.log.warning("StoryGraph: '%s' resolves to an audio edition, not linking", cand["title"])
            return None
        return Match(rid, info["pages"] or cand.get("pages"))

    def read_state(self, remote_id):
        info = self._page(remote_id)
        state = {"status": info["status"], "rating": None, "finished_dates": set(), "known": {"rating"}}
        href = info["review_href"]
        if href and "/new" not in href:                       # "see review": read the stars off the edit form
            href = href if href.endswith("/edit") else href + "/edit"
            page = self._req("GET", urljoin(BASE, href))
            for form in parse_forms(page.text):
                f = dict(form["fields"])
                if "stars_integer" in f:
                    whole = int(f["stars_integer"] or 0)
                    dec = int(f.get("stars_decimal") or 0)
                    total = whole + dec / 100
                    state["rating"] = total if total > 0 else None
                    break
        return state

    # ---- writes
    def set_status(self, remote_id, status, book):
        csrf = self._page(remote_id)["csrf"]
        def send(s):
            return self._req("POST", "/update-status.js", params={"book_id": remote_id, "status": s},
                             data={"authenticity_token": csrf}, headers=self._xhr(remote_id, csrf))
        r = send(STATUS[status])
        if status == "reading" and (r.status_code == 422 or (r.status_code == 302 and r.sign_in_redirect)):
            r = send("rereading")            # already-read books are "re-read" on StoryGraph
        if not (200 <= r.status_code < 300 or r.status_code == 302):
            raise ProviderError(f"StoryGraph status update -> HTTP {r.status_code}")

    def set_progress(self, remote_id, book, by_pages, remote_pages):
        info = self._page(remote_id)
        total = info["pages"] or remote_pages
        page = mapped_page(book, total)
        if by_pages and page is not None:
            kind, val = "pages", page
        elif book.percent is not None:
            kind, val = "percentage", book.percent
        else:
            return
        r = self._req("POST", "/update-progress", headers=self._xhr(remote_id, info["csrf"]), data={
            "read_status[progress_number]": val, "read_status[progress_type]": kind,
            "read_status[book_num_of_pages]": info["pages"] or 0, "book_id": remote_id,
            "on_book_page": "true", "authenticity_token": info["csrf"]})
        if not (200 <= r.status_code < 300 or r.status_code == 302):
            raise ProviderError(f"StoryGraph progress update -> HTTP {r.status_code}")

    def set_rating(self, remote_id, rating, book):
        """No standalone rating: load the review form, change only the stars, resubmit (plugin saveReview)."""
        href = self._page(remote_id)["review_href"]
        if href and "/new" not in href and not href.endswith("/edit"):
            href += "/edit"
        form_url = urljoin(BASE, href or f"/reviews/new?book_id={remote_id}")
        page = self._req("GET", form_url)
        forms = [f for f in parse_forms(page.text) if "/reviews" in f["action"]] or \
                [f for f in parse_forms(page.text) if any(n.startswith("review[") for n, _ in f["fields"])]
        if not forms:
            raise ProviderError("StoryGraph: review form not found (page layout changed?)")
        form = forms[0]
        whole, dec = star_fields(rating)
        fields = [(n, v) for n, v in form["fields"] if n not in ("stars_integer", "stars_decimal")]
        fields += [("stars_integer", whole), ("stars_decimal", dec)]
        names = {n for n, _ in fields}
        for k, v in (("review[book_id]", remote_id), ("return_to", f"/books/{remote_id}"), ("button", "")):
            if k not in names:
                fields.append((k, v))
        csrf = next((v for n, v in fields if n == "authenticity_token"), None) or _csrf(page.text) or ""
        if not any(n == "authenticity_token" for n, _ in fields):
            fields.append(("authenticity_token", csrf))
        r = self._req("POST", urljoin(form_url, form["action"] or "/reviews"), data=fields, headers={
            "X-CSRF-Token": csrf, "Referer": form_url,
            "Accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        if r.status_code not in (200, 302, 303):
            raise ProviderError(f"StoryGraph rating -> HTTP {r.status_code}")
