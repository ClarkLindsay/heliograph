"""The StoryGraph via session cookies, ported from ShelfSync's storygraph/api.lua + provider.lua."""
import html as htmllib, re
from html.parser import HTMLParser
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from .base import Provider, AuthError, ProviderError, Match, mapped_page, clean_cover, is_audio

FORMAT_RANK = ("paperback", "hardcover", "mass market", "digital", "ebook", "kindle")   # print first, then digital

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


def _meta(scope, book_id=None):
    """The edition's 'Format: …', 'Language: …', 'ISBN/UID: …' lines as a dict."""
    nodes = (scope.select(f".edition-info[data-book-id='{book_id}'] p") if book_id else []) or scope.select(".edition-info p")
    out = {}
    for p in nodes:
        k, sep, v = p.get_text(" ", strip=True).partition(":")
        if sep:
            out.setdefault(k.strip().lower(), v.strip())
    return out


def _format_of(scope, book_id):
    return _meta(scope, book_id).get("format", "")


def _pages_of(scope):
    info = scope.select_one("p[class*='text-xs']")
    m = re.search(r"(\d+) pages", info.get_text(" ")) if info else None
    return int(m.group(1)) if m else None


def parse_search(html):
    """Search results with each edition's language and format. Audio editions are kept (marked): their work may
    have a print edition in your language, which finalize() finds on the editions page."""
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
        meta = _meta(pane, bid) if pane is not None else {}
        img = el.parent.parent.select_one("img") if el.parent is not None and el.parent.parent is not None else None
        out.append({"id": bid, "cover": clean_cover(img.get("src") if img else None, BASE),
                    "title": " ".join(htmllib.unescape(a.get_text()).split()),
                    "authors": [" ".join(htmllib.unescape(au.get_text()).split())] if au else ["Unknown Author"],
                    "pages": _pages_of(pane) if pane is not None else None,
                    "language": meta.get("language", ""), "format": meta.get("format", ""),
                    "isbn": meta.get("isbn/uid", "")})
    return out


def parse_editions(html):
    """An editions page (/books/<id>/editions): every edition, and which one holds your status (if any)."""
    soup, eds, mine = BeautifulSoup(html, "html.parser"), [], None
    for pane in soup.select(".book-pane"):
        a = pane.select_one("a[href^='/books/']")
        m = re.match(r"/books/([^/?#]+)", a["href"]) if a else None
        if not m or any(e["id"] == m.group(1) for e in eds):
            continue
        meta = _meta(pane)
        eds.append({"id": m.group(1), "language": meta.get("language", ""), "format": meta.get("format", ""),
                    "isbn": meta.get("isbn/uid", ""), "pages": _pages_of(pane)})
        if pane.select("[class*=read-status-label]"):
            mine = mine or m.group(1)
        for x in pane.select("a[href^='/books/']"):
            if "another edition" in x.get_text(" ", strip=True).lower():
                mine = mine or re.match(r"/books/([^/?#]+)", x["href"]).group(1)
    return eds, mine


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
    meta = _meta(soup, book_id)
    fmt = meta.get("format", "")
    other, review = None, None
    for a in soup.select("a"):
        href, t = a.get("href") or "", a.get_text(" ", strip=True).lower()
        # "Want to read / Currently reading / You've read another edition": your status sits on that edition
        if not other and re.match(r"^/books/[^/]+$", href) and "another edition" in t:
            other = re.match(r"/books/([^/?]+)", href).group(1)
        if not review and re.search(r"see review|add a review|add review", t):
            review = href
    return {"has_button": bool(m), "status": status, "pages": pages, "audio": is_audio(fmt), "format": fmt,
            "language": meta.get("language", ""), "other_edition": other, "review_href": review, "csrf": _csrf(html)}


def _csrf(html):
    m = (re.search(r'<meta\s+[^>]*name=["\']csrf-token["\']\s+[^>]*content=["\']([^"\']+)["\']', html)
         or re.search(r'<meta\s+[^>]*content=["\']([^"\']+)["\']\s+[^>]*name=["\']csrf-token["\']', html)
         or re.search(r'name=["\']authenticity_token["\']\s+[^>]*value=["\']([^"\']+)["\']', html))
    return m.group(1) if m else None


class StoryGraph(Provider):
    key, label = "storygraph", "StoryGraph"
    HOME = "https://app.thestorygraph.com/"
    SUPPORTED = ("want", "reading", "read", "paused", "dnf")
    CHECKS_EDITIONS = True

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
        res = parse_search(r.text)
        # editions you'd want first; the rest stay so their work's other editions can still be reached
        return sorted(res, key=lambda c: self.edition_problem(c["language"], c["format"]) is not None)

    def lookup_isbn(self, isbn, want_audio=False):
        res = self.search(isbn)
        return self.finalize(dict(res[0], want_isbns=[isbn], want_audio=want_audio)) if res else None

    def profile_url(self):
        r = self._req("GET", "/")
        for a in BeautifulSoup(r.text, "html.parser").select("a[href^='/profile/']"):     # the menu's own link
            m = re.fullmatch(r"/profile/([^/?#]+)", a.get("href") or "")
            if m and m.group(1) != "edit":
                return f"{BASE}/profile/{m.group(1)}"
        return None

    def check_edition(self, remote_id):
        info = self._page(remote_id)
        return info["language"], info["format"]

    def _problem(self, info, want_audio=False):
        return self.edition_problem(info.get("language"), info.get("format"), want_audio)

    def editions(self, rid):
        r = self._req("GET", f"/books/{rid}/editions")
        if r.status_code != 200:
            raise ProviderError(f"StoryGraph: editions page HTTP {r.status_code}")
        return parse_editions(r.text)

    def _best_edition(self, eds, isbns=(), want_audio=False):
        """The edition to link among a work's editions: Hardcover's own ISBN if it's acceptable, else your language
        in print, then digital (or an audiobook first, when your Hardcover edition is one). Editions whose language
        isn't given rank after ones that say yours."""
        ok = [e for e in eds if not self._problem(e, want_audio)]
        if not ok:
            return None
        want = (self.config["common"].get("preferred_language") or "").strip().lower()
        for e in ok:
            if e["isbn"] and e["isbn"] in isbns:
                return e
        def rank(e):
            fmt = e["format"].lower()
            return (0 if not want or e["language"].lower() == want else 1,
                    0 if is_audio(fmt) == want_audio else 1,
                    next((i for i, f in enumerate(FORMAT_RANK) if f in fmt), len(FORMAT_RANK)))
        return sorted(ok, key=rank)[0]

    def finalize(self, cand):
        """Pick the edition to link, and spot an existing entry on the wrong edition.
        - Your status already on an acceptable edition of this work: link that one (as ShelfSync does).
        - Otherwise link this candidate if it's acceptable, else the best acceptable edition of its work.
          A book you listened to (audiobook edition in Hardcover) goes to an audiobook edition when there is one.
        - Your status on an unwanted edition of the work (audiobook / other language): switch_from tells the engine
          to move it."""
        rid, wa = cand["id"], bool(cand.get("want_audio"))
        info = self._page(rid)
        mine = rid if info["status"] else (info["other_edition"] if info["other_edition"] != rid else None)
        mine_info = info if mine == rid else (self._page(mine) if mine else None)
        if mine and not mine_info["status"]:
            mine, mine_info = None, None                     # the link said so, but no status there after all
        isbns = cand.get("want_isbns") or []
        if mine and not self._problem(mine_info, wa):
            if not (wa and not mine_info["audio"]):
                return Match(mine, mine_info["pages"] or cand.get("pages"))
            # you listened, but your entry is on a print/ebook edition: move it to an audiobook edition if there is one
            eds, _ = self.editions(mine)
            aud = self._best_edition([e for e in eds if is_audio(e["format"])], isbns, True)
            if not aud:
                return Match(mine, mine_info["pages"] or cand.get("pages"))
            return Match(aud["id"], aud.get("pages") or mine_info["pages"], switch_from=mine,
                         note=f"chose the audiobook edition (not the {mine_info['format'] or 'print'} edition; you listened)")
        if not self._problem(info, wa) and not (wa and not info["audio"]):
            target = {"id": rid, "pages": info["pages"] or cand.get("pages")}
        else:
            eds, mine_ed = self.editions(rid)
            mine = mine or mine_ed
            target = self._best_edition(eds, isbns, wa)
            if not target and not self._problem(info, wa):
                target = {"id": rid, "pages": info["pages"] or cand.get("pages")}    # e.g. no audiobook edition: print
            if not target:
                self.log.info("StoryGraph: no %s edition among the editions of %s ('%s'); trying other results",
                              self._wanted(wa), rid, cand.get("title") or "?")
                return None
        switch = mine if mine and mine != target["id"] else None
        note = None
        if target["id"] != rid or switch:
            why = self._problem(mine_info, wa) if switch and mine_info else self._problem(info, wa)
            note = f"chose another edition{f' (not the {why})' if why else ''}"
        return Match(target["id"], target.get("pages") or info["pages"] or cand.get("pages"), switch_from=switch, note=note)

    def _wanted(self, want_audio=False):
        c = self.config["common"]
        lang = (c.get("preferred_language") or "").strip()
        kind = "" if want_audio else ("non-audiobook" if c.get("avoid_audiobooks", True) else "")
        return " ".join(x for x in (lang, kind) if x) or "usable"

    CAN_REMOVE = True

    def remove_book(self, remote_id):
        """StoryGraph's own "remove book" button (removes your status, dates and tags for this edition)."""
        info = self._page(remote_id)
        if not info["status"]:
            return False
        r = self._req("POST", f"/remove-book/{remote_id}", params={"remove_tags": "true"},
                      headers=self._xhr(remote_id, info["csrf"]))
        if not (200 <= r.status_code < 300 or r.status_code in (302, 303)):
            raise ProviderError(f"StoryGraph remove -> HTTP {r.status_code}")
        if self._page(remote_id)["status"]:
            raise ProviderError("StoryGraph didn't confirm the removal")
        return True

    def switch_edition(self, from_id, to_id):
        """Move your entry (status, dates, review/rating) from one edition to another of the same work, with
        StoryGraph's own "switch to this edition" button."""
        page = self._req("GET", f"/books/{to_id}/editions")
        csrf = _csrf(page.text)
        if not csrf:
            raise ProviderError("StoryGraph: CSRF token not found on the editions page")
        r = self._req("POST", "/switch-editions", data={"authenticity_token": csrf, "from_book_id": from_id,
                                                        "to_book_id": to_id}, headers={
            "X-CSRF-Token": csrf, "Referer": f"{BASE}/books/{to_id}/editions",
            "Accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        if r.status_code not in (200, 302, 303):
            raise ProviderError(f"StoryGraph: switching edition -> HTTP {r.status_code}")
        if not self._page(to_id)["status"]:
            raise ProviderError("StoryGraph didn't confirm the edition switch")

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
