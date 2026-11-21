"""Hardcover GraphQL client (official API). Read-only: Hardcover is the source of truth.
Field names follow the ones ShelfSync's own Hardcover provider queries."""
import time
from dataclasses import dataclass, field
from typing import Optional
import requests

API = "https://api.hardcover.app/v1/graphql"
STATUS = {1: "want", 2: "reading", 3: "read", 4: "paused", 5: "dnf"}

UB_FIELDS = """
  id book_id edition_id status_id rating updated_at
  book { id title pages contributions { contribution author { name } } }
  user_book_reads(order_by: {id: desc}, limit: 1) { id progress_pages started_at finished_at }
"""


class HardcoverError(Exception):
    pass


@dataclass
class Book:
    ub_id: int
    title: str
    authors: list = field(default_factory=list)
    isbn13: Optional[str] = None
    isbn10: Optional[str] = None
    status: Optional[str] = None     # want | reading | read | paused | dnf
    pages: Optional[int] = None      # current progress, in pages of the user's edition
    total_pages: Optional[int] = None
    rating: Optional[float] = None   # 0-5
    book_id: Optional[int] = None
    edition_id: Optional[int] = None
    finished_on: Optional[str] = None   # YYYY-MM-DD of the latest read
    hydrated: bool = False

    @property
    def percent(self):
        if self.pages is not None and self.total_pages:
            return max(0, min(100, int(self.pages * 100 / self.total_pages + 0.5)))
        return None


def book_from_ub(ub: dict) -> Book:
    book = ub.get("book") or {}
    reads = ub.get("user_book_reads") or []
    read = reads[0] if reads else {}
    authors = []
    for c in book.get("contributions") or []:
        role = (c.get("contribution") or "").strip().lower()
        if c.get("author") and role in ("", "author"):      # not narrators / illustrators / translators
            authors.append(c["author"]["name"])
    return Book(
        ub_id=ub["id"], title=book.get("title") or "", authors=authors,
        status=STATUS.get(ub.get("status_id")), pages=read.get("progress_pages"),
        total_pages=book.get("pages") or None, rating=ub.get("rating"),
        book_id=ub.get("book_id"), edition_id=ub.get("edition_id"),
        finished_on=(read.get("finished_at") or None),
    )


class HardcoverClient:
    def __init__(self, token: str):
        token = (token or "").strip()
        if token.lower().startswith("bearer "):   # the account page shows it with the prefix
            token = token[7:].strip()
        if not token:
            raise HardcoverError("No Hardcover API token configured")
        self.token = token
        self._uid = None
        self.username = None
        self._ed_cache = {}

    def _q(self, query, variables=None):
        for attempt in range(4):
            r = requests.post(API, json={"query": query, "variables": variables or {}}, timeout=30, headers={
                "authorization": f"Bearer {self.token}", "content-type": "application/json",
                "user-agent": "HardcoverSync/0.3"})
            if r.status_code == 429:               # Hardcover allows ~60 req/min
                time.sleep(10 * (attempt + 1)); continue
            if r.status_code in (401, 403):
                raise HardcoverError("Hardcover rejected the token (expired or wrong)")
            r.raise_for_status()
            j = r.json()
            if j.get("errors"):
                raise HardcoverError(str(j["errors"])[:400])
            return j["data"]
        raise HardcoverError("Hardcover rate limit; try again later")

    @property
    def user_id(self):
        if self._uid is None:
            me = self._q("query { me { id username } }")["me"]
            me = me[0] if isinstance(me, list) else me
            self._uid, self.username = me["id"], me.get("username")
        return self._uid

    def _books(self, where):
        out, offset = [], 0
        q = f"""query($w: user_books_bool_exp!, $o: Int!) {{
          user_books(where: $w, order_by: {{id: asc}}, limit: 100, offset: $o) {{ {UB_FIELDS} }} }}"""
        while True:
            rows = self._q(q, {"w": where, "o": offset})["user_books"]
            out += [book_from_ub(r) for r in rows]
            if len(rows) < 100:
                return out
            offset += 100

    def fetch_library(self):
        return self._books({"user_id": {"_eq": self.user_id}})

    def fetch_reading(self):
        return self._books({"user_id": {"_eq": self.user_id}, "status_id": {"_eq": 2}})

    def fetch_one(self, ub_id):
        rows = self._books({"user_id": {"_eq": self.user_id}, "id": {"_eq": int(ub_id)}})
        return rows[0] if rows else None

    def fetch_updated_since(self, iso_ts):
        if not iso_ts:
            return self.fetch_library()
        return self._books({"user_id": {"_eq": self.user_id}, "updated_at": {"_gt": iso_ts}})

    def cover_url(self, b: Book):
        """Cover of the user's edition (else the book), for the web UI only. None if unavailable."""
        key = ("e", b.edition_id) if b.edition_id else ("b", b.book_id)
        if key not in self._ed_cache.setdefault("covers", {}):
            url = None
            try:
                if b.edition_id:
                    q, var = "query($id: Int!) { editions(where: {id: {_eq: $id}}) { cached_image } }", b.edition_id
                else:
                    q, var = "query($id: Int!) { books(where: {id: {_eq: $id}}) { cached_image } }", b.book_id
                data = self._q(q, {"id": var})
                row = (data.get("editions") or data.get("books") or [{}])[0]
                img = row.get("cached_image")
                if isinstance(img, str):
                    import json as _json
                    img = _json.loads(img)
                url = img.get("url") if isinstance(img, dict) else None
            except Exception:
                url = None
            self._ed_cache["covers"][key] = url if isinstance(url, str) and url.startswith("https://") else None
        return self._ed_cache["covers"][key]

    def hydrate(self, b: Book):
        """Fill ISBNs and the user's edition page count (one cached query), as the plugin's editions do."""
        if b.hydrated:
            return
        key = ("e", b.edition_id) if b.edition_id else ("b", b.book_id)
        if key not in self._ed_cache and key[1]:
            if b.edition_id:
                q = "query($id: Int!) { editions(where: {id: {_eq: $id}}) { id pages isbn_10 isbn_13 } }"
            else:
                q = """query($id: Int!) { editions(where: {book_id: {_eq: $id}, _or: [{reading_format_id: {_is_null: true}},
                       {reading_format_id: {_neq: 2}}]}, order_by: {users_count: desc_nulls_last}, limit: 1)
                       { id pages isbn_10 isbn_13 } }"""
            rows = self._q(q, {"id": key[1]}).get("editions") or []
            self._ed_cache[key] = rows[0] if rows else {}
        ed = self._ed_cache.get(key) or {}
        b.isbn13, b.isbn10 = ed.get("isbn_13") or b.isbn13, ed.get("isbn_10") or b.isbn10
        if ed.get("pages"):
            b.total_pages = ed["pages"]
        b.hydrated = True
