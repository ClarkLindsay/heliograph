"""Fable via its Firebase login + api.fable.co, ported from ShelfSync's fable/api.lua."""
import time
import requests
from . import firebase
from urllib.parse import quote
from .base import Provider, AuthError, ProviderError, Match, mapped_page, clean_cover

API = "https://api.fable.co"
FIREBASE_KEY = "AIzaSyDEnLDTjMQ9v-XaQ7pk6pJxKrpVbX_rr6U"      # Fable's public Firebase web key (shipped to every browser; not a secret)
SIGN_IN = "https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword"
SYSTEM_TYPE = {"want": "want_to_read", "reading": "current_reading", "read": "finished", "dnf": "did_not_finish"}
STATUS_BY_TYPE = {v: k for k, v in SYSTEM_TYPE.items()}
UA = "HardcoverSync/0.2 (ShelfSync-compatible)"


class Fable(Provider):
    key, label = "fable", "Fable"
    HOME = "https://fable.co/"
    SUPPORTED = ("want", "reading", "read", "dnf")          # Fable has no Paused list
    CHECKS_EDITIONS = True

    def __init__(self, config, log):
        super().__init__(config, log)
        self._lists = None

    # ---- auth
    def _store(self, s):
        self.cfg.update(id_token=s["id_token"], refresh_token=s["refresh_token"],
                        token_expires_at=time.time() + s["expires_in"])
        if s.get("email"):
            self.cfg["email"] = s["email"]
        self.config.save()

    def _login(self):
        pw = self.cfg.get("password")
        if not self.cfg.get("email") or not pw:
            raise AuthError("Fable: email/password not set")
        self._store(firebase.sign_in(SIGN_IN, FIREBASE_KEY, {"email": self.cfg["email"], "password": pw,
                                                             "returnSecureToken": True}))

    def _ensure_token(self, force=False):
        c = self.cfg
        if not force and c.get("id_token") and time.time() < c.get("token_expires_at", 0) - 60:
            return
        if c.get("refresh_token") and (s := firebase.refresh(FIREBASE_KEY, c["refresh_token"])):
            return self._store(s)
        self._login()

    def _req(self, path, method="GET", body=None, _retry=True):
        self._ensure_token()
        r = requests.request(method, API + path, json=body, timeout=30, headers={
            "Authorization": "JWT " + self.cfg["id_token"], "Content-Type": "application/json", "User-Agent": UA})
        if r.status_code in (401, 403):
            if _retry:
                self._ensure_token(force=True)
                return self._req(path, method, body, False)
            raise AuthError("Fable rejected the login")
        try:
            data = r.json() if r.content else None
        except ValueError:
            data = None
        return r.status_code, data

    def _uid(self):
        if not self.cfg.get("user_id"):
            code, d = self._req("/api/settings/profile/")
            if code != 200 or not d or not d.get("id"):
                raise ProviderError(f"Fable: profile lookup failed (HTTP {code})")
            self.cfg["user_id"] = d["id"]; self.config.save()
        return self.cfg["user_id"]

    # ---- Provider API
    def profile_url(self):
        code, d = self._req("/api/settings/profile/")
        url = (d or {}).get("url") if code == 200 else None             # e.g. https://fable.co/fabler/<name>-<id>
        return url if isinstance(url, str) else None

    def check_auth(self):
        self._uid()
        return "Fable login OK"

    def _books(self, q):
        code, d = self._req(f"/api/books/search/?auto={quote(q, safe='')}&include=out_of_catalog&type=book&limit=20&offset=0")
        if code != 200 or d is None:
            raise ProviderError(f"Fable search failed (HTTP {code})")
        return ((d or {}).get("response") or {}).get("books") or []

    @staticmethod
    def _cand(b):
        return {"id": str(b["id"]), "title": b.get("title") or "", "isbn": b.get("isbn") or b.get("display_isbn"),
                "cover": clean_cover(b.get("cover_image")),
                "authors": [a.get("name") for a in (b.get("authors") or []) if a.get("name")] or ["Unknown Author"]}

    def search(self, query):
        return [self._cand(b) for b in self._books(query)] if query.strip() else []

    def lookup_isbn(self, isbn, want_audio=False):
        cands = [self._cand(b) for b in self._books(isbn)]
        hit = next((c for c in cands if c["isbn"] == isbn), cands[0] if cands else None)   # plugin: first if no exact
        return self.finalize(dict(hit, isbn=isbn, want_audio=want_audio)) if hit else None

    def finalize(self, cand):
        """Refuse a book whose own edition is an audiobook or in another language (Settings)."""
        eds = self._editions(cand["id"])
        cur = next((e for e in eds if e.get("is_current_book")), None)
        problem = self.edition_problem(*self._meta(cur), want_audio=bool(cand.get("want_audio"))) if cur else None
        if problem:
            self.log.info("Fable: skipping %s for '%s': %s", cand["id"], cand.get("title") or "?", problem)
            return None
        return Match(cand["id"], self._edition_pages(cand["id"], cand.get("isbn"), eds))

    @staticmethod
    def _meta(e):
        return (((e or {}).get("language") or {}).get("name") or ""), (((e or {}).get("format") or {}).get("name") or "")

    def _editions(self, rid):
        code, d = self._req(f"/api/books/{rid}/editions/")
        return ((d or {}).get("results") if code == 200 else None) or []

    def check_edition(self, remote_id):
        cur = next((e for e in self._editions(remote_id) if e.get("is_current_book")), None)
        return self._meta(cur) if cur else None

    def _edition_pages(self, rid, isbn, eds=None):
        """findEditionPageCount: edition by ISBN, else the current one, else any with a page count."""
        eds = eds if eds is not None else self._editions(rid)
        if not eds:
            return None
        ok = lambda e: (e.get("page_count") or 0) > 0
        for pred in (lambda e: isbn and e.get("display_isbn") == isbn, lambda e: e.get("is_current_book"), lambda e: True):
            for e in eds:
                if pred(e) and ok(e):
                    return e["page_count"]
        return None

    def _status(self, rid):
        code, d = self._req(f"/api/books/{rid}")
        return STATUS_BY_TYPE.get(((d or {}).get("response") or {}).get("status")) if code == 200 else None

    def read_state(self, remote_id):
        state = {"status": self._status(remote_id), "rating": None, "finished_dates": set(), "known": {"rating"}}
        code, ex = self._req(f"/api/users/{self._uid()}/reviews/{remote_id}")
        if code == 200 and isinstance(ex, dict) and isinstance(ex.get("rating"), (int, float)) and ex["rating"] > 0:
            state["rating"] = float(ex["rating"])
        elif code not in (200, 404):
            raise ProviderError(f"Fable: couldn't read your review (HTTP {code})")
        return state

    def _list_ids(self):
        if self._lists is None:
            code, d = self._req(f"/api/v2/users/{self._uid()}/book_lists?limit=20&offset=0&media_type=book")
            if code != 200 or not d or "results" not in d:
                raise ProviderError(f"Fable: could not load shelves (HTTP {code})")
            self._lists = {l["system_type"]: l["id"] for l in d["results"]
                           if l.get("type") == "system" and l.get("system_type")}
        return self._lists

    CAN_REMOVE = True

    def remove_book(self, remote_id):
        """Take the book off every Fable list (the same multiselect call the app uses, with no list selected)."""
        if self._status(remote_id) is None:
            return False
        ids = self._list_ids()
        code, _ = self._req(f"/api/v2/users/{self._uid()}/book_lists/book", "POST", {
            "type": "multiselect", "book_id": remote_id, "book_list_ids": [], "exclude_from": list(ids.values())})
        if not (200 <= (code or 0) < 300):
            raise ProviderError(f"Fable remove -> HTTP {code}")
        if self._status(remote_id) is not None:
            raise ProviderError("Fable didn't confirm the removal")
        return True

    def set_status(self, remote_id, status, book):
        target = SYSTEM_TYPE.get(status)
        if not target:
            return
        if self._status(remote_id) == status:       # Fable 409s on a repeat add
            return
        ids = self._list_ids()
        if target not in ids:
            raise ProviderError(f"Fable: no '{target}' list on this account")
        code, _ = self._req(f"/api/v2/users/{self._uid()}/book_lists/book", "POST", {
            "type": "multiselect", "book_id": remote_id, "book_list_ids": [ids[target]],
            "exclude_from": [ids[t] for t in SYSTEM_TYPE.values() if t != target and t in ids]})
        if not (200 <= (code or 0) < 300 or code == 409):
            raise ProviderError(f"Fable status update -> HTTP {code}")

    def set_progress(self, remote_id, book, by_pages, remote_pages):
        body = {"status": "reading", "social_accounts": []}
        total = remote_pages or book.total_pages
        page = mapped_page(book, total)
        if by_pages and page is not None and total:
            body.update(current_page=int(page), page_count=int(total), selected_mode="page_count")
        elif book.percent is not None:
            body.update(current_percentage=int(book.percent), selected_mode="percentage")
        else:
            return
        code, _ = self._req(f"/api/books/{remote_id}/reading_progress", "POST", body)
        if code not in (200, 201):
            raise ProviderError(f"Fable progress -> HTTP {code}")

    def set_rating(self, remote_id, rating, book):
        code, ex = self._req(f"/api/users/{self._uid()}/reviews/{remote_id}")
        if code not in (200, 404):
            raise ProviderError(f"Fable: couldn't load existing review (HTTP {code})")
        ex = ex if code == 200 and ex else {}
        code, _ = self._req(f"/api/books/{remote_id}/reviews", "POST", {
            "rating": float(rating), "labels": ex.get("labels") or [], "review": ex.get("review") or "",
            "contains_spoilers": bool(ex.get("contains_spoilers")), "did_not_finish": bool(ex.get("did_not_finish"))})
        if not (code and 200 <= code < 300):
            raise ProviderError(f"Fable rating -> HTTP {code}")
