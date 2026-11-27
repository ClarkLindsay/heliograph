"""Pagebound via Firebase login + token exchange, ported from ShelfSync's pagebound/api.lua."""
import re, time
from datetime import datetime
from urllib.parse import quote
import requests
from . import firebase
from .base import Provider, AuthError, ProviderError, Match, mapped_page, clean_cover

API = "https://prod-pagebound-api.onrender.com"
FIREBASE_KEY = "AIzaSyDCfBJ51pRZgHueBfBz0KNDiPNev1ClnGg"   # Pagebound's public Firebase web key (shipped to every browser; not a secret)
TYPESENSE_URL = "https://hztadco4ku1vqi6lp.a1.typesense.net"
TYPESENSE_KEY = "SgSrp2Vx4V4wjJAwnME6uWUufNdi9BxM"
SIGN_IN = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
SYSTEM = {"want": "interested", "reading": "current", "read": "finished", "paused": "paused", "dnf": "dnf"}
STATUS_BY = {v: k for k, v in SYSTEM.items()}
UA = "HardcoverSync/0.2 (ShelfSync-compatible)"


def pb_date():
    n = datetime.now()
    return f"{n.month}/{n.day}/{n.year}"          # un-padded m/d/Y, as Pagebound's own requests


def form_date(v):
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(v or ""))
    return f"{int(m.group(2))}/{int(m.group(3))}/{m.group(1)}" if m else (str(v) if v else None)


def status_payload(status, existing, finished_on=None):
    today = pb_date()
    started = existing and existing.get("started_reading_at")
    finished = existing and existing.get("finished_reading_at")
    was_finished = existing and existing.get("status") == "finished"
    if status == "reading" and (not started or was_finished):
        started = today
    if status == "read" and finished_on:
        finished = form_date(finished_on) or finished    # Hardcover's own finish date; none there = none invented
    e = existing or {}
    body = {
        "user_book": {"owned": bool(e.get("owned")), "muted": bool(e.get("muted"))},
        "shelf_ids": [s.get("id", s.get("shelf_id")) if isinstance(s, dict) else s for s in e.get("shelves") or []],
        "status": SYSTEM[status], "date": e.get("date_added") or today,
        "started_reading_at": started or "", "finished_reading_at": finished or "",
        "challenge_year": e.get("challenge_year"), "format": e.get("format") or "print",
        "tracking_mode": e.get("tracking_mode") or "pages", "total_page_count": "",
        "total_minutes": (e.get("total_minutes") or 0) if existing else None,
    }
    for k in ("started_reading_at", "finished_reading_at"):
        if not body[k]:
            body.pop(k)                                   # Pagebound rejects a blank date; leaving it out is fine
    return body


class Pagebound(Provider):
    key, label = "pagebound", "Pagebound"
    HOME = "https://pagebound.co/"

    # ---- auth (Firebase sign-in -> Pagebound token exchange)
    def _store(self, **kw):
        self.cfg.update(kw); self.config.save()

    def _sign_in(self):
        pw = self.cfg.get("password")
        if not self.cfg.get("email") or not pw:
            raise AuthError("Pagebound: email/password not set")
        s = firebase.sign_in(SIGN_IN, FIREBASE_KEY, {"clientType": "CLIENT_TYPE_WEB", "email": self.cfg["email"],
                                                    "password": pw, "returnSecureToken": True})
        self._store(id_token=s["id_token"], refresh_token=s["refresh_token"],
                    token_expires_at=time.time() + s["expires_in"])

    def _refresh_firebase(self):
        c = self.cfg
        if c.get("refresh_token") and (s := firebase.refresh(FIREBASE_KEY, c["refresh_token"])):
            return self._store(id_token=s["id_token"], refresh_token=s["refresh_token"],
                               token_expires_at=time.time() + s["expires_in"])
        if c.get("id_token") and c.get("token_expires_at", 0) > time.time() + 60:
            return
        self._sign_in()

    def _exchange(self):
        r = requests.post(API + "/api/v1/auth/firebase_auth", json={"id_token": self.cfg["id_token"]},
                          headers={"Authorization": "Bearer null"}, timeout=90)     # Render cold starts are slow
        d = r.json() if r.content else {}
        if r.status_code != 200 or not d.get("token"):
            raise ProviderError(f"Pagebound token exchange failed (HTTP {r.status_code})")
        self._store(api_token=d["token"], user_id=(d.get("user") or {}).get("id") or self.cfg.get("user_id"))

    def _ensure(self, force=False):
        c = self.cfg
        expiring = not c.get("id_token") or c.get("token_expires_at", 0) <= time.time() + 60
        if force or expiring:
            self._refresh_firebase()
        if force or expiring or not c.get("api_token"):
            self._exchange()

    def _req(self, path, method="GET", body=None, _retry=True):
        self._ensure()
        r = requests.request(method, API + path, json=body, timeout=45, headers={
            "Authorization": "Bearer " + self.cfg["api_token"], "Accept": "application/json, text/plain, */*",
            "User-Agent": UA})
        if r.status_code in (401, 403):
            if _retry:
                self._ensure(force=True)
                return self._req(path, method, body, False)
            raise AuthError("Pagebound rejected the login")
        try:
            data = r.json() if r.content else None
        except ValueError:
            data = None
        return r.status_code, data

    def check_auth(self):
        self._ensure(force=True)
        return "Pagebound login OK"

    def profile_url(self):
        self._ensure()
        uid = self.cfg.get("user_id")
        if not uid:
            return None
        code, d = self._req(f"/api/v1/users/{uid}")
        name = ((d or {}).get("user", d) or {}).get("username") if code == 200 and isinstance(d, dict) else None
        return f"https://pagebound.co/users/{quote(name, safe='')}" if name else None

    # ---- search (Typesense: title/author only, no ISBN lookup - same as the plugin)
    def search(self, query):
        """Free-text search, as typed into the web UI's search box."""
        return self._typesense(query, "title,author_name", limit=15)

    def search_book(self, title, authors):
        """Search for a known book. Putting the author into the query makes Typesense rank books with that name in
        their *title* (comics, box sets, biographies) above the book itself, so search the title only, restricted to
        the book's authors, then the title alone in case Pagebound spells the author differently."""
        out, seen = [], set()
        names = [a.replace("`", "") for a in authors if a and a.strip()][:5]
        queries = [dict(filter_by="author_name:=[" + ",".join(f"`{a}`" for a in names) + "]")] if names else []
        for extra in queries + [{}]:
            for c in self._typesense(title, "title", limit=15, **extra):
                if c["id"] not in seen:
                    seen.add(c["id"]); out.append(c)
        return out

    def _typesense(self, query, query_by, limit, filter_by=None):
        if not query.strip():
            return []
        params = {"q": query, "query_by": query_by, "limit": limit, "num_typos": 2,
                  "split_join_tokens": "always", "typo_tokens_threshold": 6}
        if filter_by:
            params["filter_by"] = filter_by
        r = requests.get(TYPESENSE_URL + "/collections/books/documents/search", timeout=30,
                         headers={"X-TYPESENSE-API-KEY": TYPESENSE_KEY, "Accept": "application/json"},
                         params=params)
        if r.status_code != 200:
            raise ProviderError(f"Pagebound search failed (HTTP {r.status_code})")
        out = []
        for h in r.json().get("hits", []):
            d = h.get("document") or {}
            if d.get("id") and d.get("uuid"):
                pc = d.get("page_count")
                out.append({"id": f"{d['id']}:{d['uuid']}", "title": d.get("title") or "Untitled",
                            "authors": [d.get("author_name") or d.get("author") or "Unknown Author"],
                            "pages": int(pc) if pc else None,
                            "cover": clean_cover(d.get("image_url") or d.get("image_url_medium") or d.get("image_url_small"))})
        return out

    def parse_ref(self, text):
        """Accepts "<id>:<uuid>" (as shown in the picker), a book link (pagebound.co/books/<uuid>) or a bare uuid.
        A link only carries the uuid; finalize() looks up the numeric id that writes need."""
        t = (text or "").strip()
        if re.fullmatch(r"\d+:[0-9a-fA-F-]{8,}", t):
            return t
        m = re.search(r"/books/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b", t) \
            or re.fullmatch(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", t)
        return ":" + m.group(1).lower() if m else None

    def finalize(self, cand):
        bid, uuid = self._split(str(cand["id"]))
        if bid:
            return Match(cand["id"], cand.get("pages"))
        code, d = self._req(f"/api/v1/books/{uuid}")                 # pasted link: fetch the numeric id
        b = (d or {}).get("book") if code == 200 else None
        if not b or not b.get("id"):
            raise ValueError(f"Pagebound has no book with id {uuid} (HTTP {code})")
        pc = b.get("page_count")
        return Match(f"{b['id']}:{b.get('uuid') or uuid}", int(pc) if pc else None)

    def book_url(self, remote_id):
        _, uuid = self._split(str(remote_id or ""))
        return f"https://pagebound.co/books/{uuid}" if uuid else None

    @staticmethod
    def _split(rid):
        bid, _, uuid = rid.partition(":")
        return bid, uuid

    def _user_book(self, rid):
        bid, uuid = self._split(rid)
        code, d = self._req(f"/api/v1/books/{uuid}")
        if code != 200 or not d or not d.get("book"):
            raise ProviderError(f"Pagebound: book lookup failed (HTTP {code})")
        b = d["book"]; ub = b.get("user_book")
        page_count = b.get("page_count")
        if not ub:
            return None, page_count
        cur = ub.get("current_reading_instance") if (ub.get("current_reading_instance") or {}).get("current") is not False else None
        cur = cur if cur and cur.get("id") else next((r for r in ub.get("reading_instances") or [] if r.get("current") and r.get("id")), None)
        return {
            "id": ub.get("uuid") or str(ub.get("id")), "user_book_id": ub.get("id"), "status": ub.get("status"),
            "progress": ub.get("progress") or 0, "current_page": ub.get("current_page"),
            "total_page_count": ub.get("total_page_count") or (cur or {}).get("total_page_count") or page_count,
            "owned": ub.get("owned"), "muted": ub.get("muted"), "date_added": ub.get("date_added"),
            "started_reading_at": form_date((cur or {}).get("started_reading_at_date") or (cur or {}).get("started_reading_at") or ub.get("started_reading_at")),
            "finished_reading_at": form_date((cur or {}).get("finished_reading_at_date") or (cur or {}).get("finished_reading_at") or ub.get("finished_reading_at")),
            "challenge_year": (cur or {}).get("challenge_year") or ub.get("challenge_year"),
            "format": (cur or {}).get("format") or ub.get("format"),
            "tracking_mode": (cur or {}).get("tracking_mode") or ub.get("tracking_mode"),
            "total_minutes": ub.get("total_minutes"), "shelves": ub.get("shelves") or [], "reading": cur,
        }, page_count

    def library(self):
        out, page = [], 1
        while True:
            code, d = self._req(f"/api/v1/user_books?page={page}")
            if code != 200 or not isinstance(d, dict):
                raise ProviderError(f"Pagebound: couldn't read your reading list (HTTP {code})")
            for ub in d.get("user_books") or []:
                out.append({"id": f"{ub['book_id']}:{ub['book_uuid']}", "title": (ub.get("title") or "").strip(),
                            "authors": [ub["author_name"]] if ub.get("author_name") else [], "isbn": None})
            if page >= (d.get("total_pages") or 1):
                return out
            page += 1

    def normalize_rating(self, rating):
        return int(float(rating) * 2) / 2

    def read_state(self, remote_id):
        existing, _ = self._user_book(remote_id)
        state = {"status": STATUS_BY.get((existing or {}).get("status")), "rating": None, "finished_dates": set(), "known": set()}
        fin = (existing or {}).get("finished_reading_at")
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", str(fin or ""))
        if m:
            state["finished_dates"].add(f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}")
        if state["status"] == "read":
            state["known"].add("finished_on")
        return state

    CAN_REMOVE = True

    def remove_book(self, remote_id):
        """Delete your reading-list entry for this book."""
        existing, _ = self._user_book(remote_id)
        if not existing:
            return False
        code, _d = self._req(f"/api/v1/user_books/{existing['id']}", "DELETE")
        if not (code and (200 <= code < 300)):
            raise ProviderError(f"Pagebound remove -> HTTP {code}")
        if self._user_book(remote_id)[0]:
            raise ProviderError("Pagebound didn't confirm the removal")
        return True

    def set_status(self, remote_id, status, book):
        bid, _ = self._split(remote_id)
        existing, _ = self._user_book(remote_id)
        if existing:
            code, _d = self._req(f"/api/v1/user_books/{existing['id']}", "PUT",
                                 status_payload(status, existing, book.finished_on))
        else:
            p = status_payload(status, None, book.finished_on)
            p["user_book"]["book_id"] = int(bid)
            p["user_book"]["status"] = p.pop("status")
            code, _d = self._req("/api/v1/user_books", "POST", p)
        if not (code and 200 <= code < 300):
            raise ProviderError(f"Pagebound status update -> HTTP {code}")

    supports_favorites = True
    FAVORITES = "favorites"

    def _favorites_shelf(self, create=False):
        code, shelves = self._req("/api/v1/shelves")
        if code != 200 or not isinstance(shelves, list):
            raise ProviderError(f"Pagebound: couldn't load your shelves (HTTP {code})")
        mine = next((s for s in shelves if (s.get("name") or "").strip().lower() == self.FAVORITES), None)
        if mine or not create:
            return mine and mine["id"]
        code, made = self._req("/api/v1/shelves", "POST", {"shelf": {"name": self.FAVORITES}})
        if not (code and 200 <= code < 300) or not (made or {}).get("id"):
            raise ProviderError(f"Pagebound: couldn't make a favorites shelf (HTTP {code})")
        return made["id"]

    @staticmethod
    def _shelf_ids(existing):
        return [s.get("id", s.get("shelf_id")) if isinstance(s, dict) else s for s in existing.get("shelves") or []]

    def set_favorite(self, remote_id, book, on=True):
        existing, _ = self._user_book(remote_id)
        if not existing:
            raise ProviderError("Pagebound: the book isn't on your shelves")
        fav = self._favorites_shelf(create=on)
        ids = self._shelf_ids(existing)
        if not fav or (fav in ids) == bool(on):
            return False
        status = STATUS_BY.get(existing.get("status"))
        if not status:
            raise ProviderError(f"Pagebound: unknown status {existing.get('status')!r}")
        body = status_payload(status, existing)          # everything as it is, the favorites shelf added/removed
        body["shelf_ids"] = ids + [fav] if on else [i for i in ids if i != fav]
        code, _d = self._req(f"/api/v1/user_books/{existing['id']}", "PUT", body)
        if not (code and 200 <= code < 300):
            raise ProviderError(f"Pagebound favorites -> HTTP {code}")
        if (fav in self._shelf_ids(self._user_book(remote_id)[0] or {})) != bool(on):
            raise ProviderError("Pagebound didn't keep the change to favorites")
        return True

    supports_owned = True

    def set_owned(self, remote_id, book, owned=True):
        existing, _ = self._user_book(remote_id)
        if not existing:
            raise ProviderError("Pagebound: the book isn't on your shelves")
        if bool(existing.get("owned")) == bool(owned):
            return False
        status = STATUS_BY.get(existing.get("status"))
        if not status:
            raise ProviderError(f"Pagebound: unknown status {existing.get('status')!r}")
        body = status_payload(status, existing)          # everything as it is, owned switched on
        body["user_book"]["owned"] = bool(owned)
        code, _d = self._req(f"/api/v1/user_books/{existing['id']}", "PUT", body)
        if not (code and 200 <= code < 300):
            raise ProviderError(f"Pagebound owned -> HTTP {code}")
        if bool((self._user_book(remote_id)[0] or {}).get("owned")) != bool(owned):
            raise ProviderError("Pagebound didn't keep the change to owned")
        return True

    def set_progress(self, remote_id, book, by_pages, remote_pages):
        existing, page_count = self._user_book(remote_id)
        if not existing or not (existing.get("reading") or {}).get("id"):
            self.set_status(remote_id, "reading", book)
            existing, page_count = self._user_book(remote_id)
        cur = (existing or {}).get("reading")
        if not cur:
            raise ProviderError("Pagebound: no active reading session")
        total = existing.get("total_page_count") or page_count or remote_pages or book.total_pages
        total = int(total) if total and int(total) > 0 else None
        mapped = mapped_page(book, total)
        pages_mode = bool(by_pages and mapped is not None)
        req_pct = book.percent if book.percent is not None else int(existing.get("progress") or 0)
        req_pct = int(max(0, min(100, req_pct)) + 0.5)
        page = mapped if pages_mode else (int(req_pct / 100 * total + 0.5) if total else existing.get("current_page"))
        if page is not None:
            page = max(0, int(float(page) + 0.5))
            if total:
                page = min(total, page)
        pct = int(page * 100 / total) if (page is not None and total) else (
            int(max(0, min(100, existing.get("progress") or 0))) if pages_mode else req_pct)
        payload = {
            "reading_update": {"user_book_id": int(cur.get("user_book_id") or existing["user_book_id"]),
                               "date": pb_date(), "reading_instance_id": cur["id"], "total_progress": pct,
                               "total_pages_read": str(page) if page is not None else None},
            "user_book": {"current_page": str(page) if page is not None else None,
                          "total_page_count": total if total else ("" if pages_mode else None),
                          "current_minute": None, "total_minutes": None},
            "no_broadcast": False, "progress_method": "pages" if pages_mode else "percent"}
        code, _d = self._req("/api/v1/reading_updates", "POST", payload)
        if code not in (200, 201):
            raise ProviderError(f"Pagebound progress -> HTTP {code}")

    def set_rating(self, remote_id, rating, book):
        bid, _ = self._split(remote_id)
        existing, _pc = self._user_book(remote_id)
        if not existing:
            raise ProviderError("Pagebound: no reading-list entry for this book yet")
        stars = int(float(rating) * 2) / 2      # Pagebound takes half stars (floor), like the plugin
        rv = self._review(int(bid), getattr(book, "title", None))
        if rv:                                  # already rated: change that review (a second POST is refused)
            if rv.get("overall_rating") is not None and float(rv["overall_rating"]) == stars:
                return
            code, _d = self._req(f"/api/v1/reviews/{rv['id']}", "PUT", {"review": {"overall_rating": stars}})
        else:
            n = None
            code, _d = self._req("/api/v1/reviews", "POST", {"review": {
                "overall_rating": stars, "quality_rating": n, "entertainment_rating": n, "plot_rating": n,
                "audiobook_rating": n, "character_rating": n, "is_spoiler": False, "review": "", "emojis": [],
                "book_id": int(bid), "user_book_id": int(existing["user_book_id"]), "is_dnf": False}})
        if not (code and 200 <= code < 300):
            raise ProviderError(f"Pagebound rating -> HTTP {code}")

    supports_reviews = True

    def get_review(self, remote_id, book):
        bid, _ = self._split(remote_id)
        return (self._review(int(bid), getattr(book, "title", None)) or {}).get("review") or None

    def set_review(self, remote_id, text, spoilers, book):
        bid, _ = self._split(remote_id)
        rv = self._review(int(bid), getattr(book, "title", None))
        if rv:
            code, _d = self._req(f"/api/v1/reviews/{rv['id']}", "PUT", {"review": {"review": text, "is_spoiler": bool(spoilers)}})
        else:
            existing, _pc = self._user_book(remote_id)
            if not existing:
                raise ProviderError("Pagebound: no reading-list entry for this book yet")
            rating = getattr(book, "rating", None)
            n = None
            code, _d = self._req("/api/v1/reviews", "POST", {"review": {
                "overall_rating": int(float(rating) * 2) / 2 if rating else None, "quality_rating": n,
                "entertainment_rating": n, "plot_rating": n, "audiobook_rating": n, "character_rating": n,
                "is_spoiler": bool(spoilers), "review": text, "emojis": [], "book_id": int(bid),
                "user_book_id": int(existing["user_book_id"]), "is_dnf": False}})
        if not (code and 200 <= code < 300):
            raise ProviderError(f"Pagebound review -> HTTP {code}")

    def _review(self, book_id, title=None):
        """Your review of a book, or None. The book endpoints don't include it; the reading list does (search by
        title first, else every page)."""
        def find(query):
            for page in range(1, 100):
                code, d = self._req(f"/api/v1/user_books?page={page}" + (f"&q={quote(query)}" if query else ""))
                if code != 200 or not isinstance(d, dict):
                    return None
                for ub in d.get("user_books") or []:
                    if ub.get("book_id") == book_id:
                        return ub.get("review") or {}
                if page >= (d.get("total_pages") or 1):
                    return None
        found = find(title) if title else None
        if found is None:
            found = find(None)
        return found or None
