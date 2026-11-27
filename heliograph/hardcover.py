"""Hardcover GraphQL client (official API). Hardcover is the source of truth: syncing only reads it. The only writes
are set_edition() and set_read_dates(), used by the web UI's Tools → Hardcover cleanup when you press Apply.
Field names follow the ones ShelfSync's own Hardcover provider queries."""
import time
from dataclasses import dataclass, field
from typing import Optional
import requests

API = "https://api.hardcover.app/v1/graphql"
STATUS = {1: "want", 2: "reading", 3: "read", 4: "paused", 5: "dnf"}
AUDIO_FORMAT = 2       # editions.reading_format_id: 1 Read, 2 Listened, 4 Ebook
FORMAT_NAMES = {1: "Print", 2: "Audiobook", 3: "Print", 4: "Ebook"}

UB_FIELDS = """
  id book_id edition_id status_id rating updated_at review_raw review_has_spoilers
  book { id slug title pages contributions { contribution author { name } } }
  edition { reading_format_id audio_seconds }
  user_book_reads(order_by: {id: desc}, limit: 1) { id progress_pages progress_seconds started_at finished_at }
  last_finished: user_book_reads(where: {finished_at: {_is_null: false}}, order_by: {finished_at: desc}, limit: 1) { started_at finished_at }
"""


class HardcoverError(Exception):
    pass


class HardcoverDailyLimit(HardcoverError):
    """Hardcover's daily request allowance is used up: waiting a few seconds won't help."""
    def __init__(self, seconds, limit=None):
        self.seconds = seconds
        h, m = divmod(max(1, round(seconds / 60)), 60)
        super().__init__(f"Hardcover's daily limit{f' of {limit} requests' if limit else ''} is used up; "
                         f"it resets in {f'{h}h ' if h else ''}{m}m")


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
    started_on: Optional[str] = None    # when that read started, if Hardcover knows
    audio: bool = False                 # your Hardcover edition is an audiobook ("Listened")
    seconds: Optional[int] = None       # audiobook progress: time listened
    total_seconds: Optional[int] = None # audiobook length
    slug: Optional[str] = None          # the Hardcover book's slug (not stored)
    review: Optional[str] = None        # your Hardcover review, as plain text
    review_spoilers: bool = False
    hydrated: bool = False

    @property
    def timed(self):
        """An audiobook whose progress Hardcover keeps as time listened: it's synced as a percentage."""
        return bool(self.audio and self.seconds is not None and self.total_seconds)

    @property
    def percent(self):
        if self.timed:
            return max(0, min(100, int(self.seconds * 100 / self.total_seconds + 0.5)))
        if self.pages is not None and self.total_pages:
            return max(0, min(100, int(self.pages * 100 / self.total_pages + 0.5)))
        return None

    @property
    def position(self):
        """How far along you are, as progress is tracked: the percentage for timed audiobooks, else the page."""
        return self.percent if self.timed else self.pages


def book_from_ub(ub: dict) -> Book:
    book = ub.get("book") or {}
    reads = ub.get("user_book_reads") or []
    read = reads[0] if reads else {}
    authors = []
    for c in book.get("contributions") or []:
        role = (c.get("contribution") or "").strip().lower()
        if c.get("author") and role in ("", "author"):      # not narrators / illustrators / translators
            authors.append(c["author"]["name"])
    done = read if read.get("finished_at") else (ub.get("last_finished") or [{}])[0]
    return Book(
        ub_id=ub["id"], title=book.get("title") or "", authors=authors,
        status=STATUS.get(ub.get("status_id")), pages=read.get("progress_pages"),
        total_pages=book.get("pages") or None, rating=ub.get("rating"),
        book_id=ub.get("book_id"), edition_id=ub.get("edition_id"),
        finished_on=done.get("finished_at") or None, started_on=done.get("started_at") or None,
        audio=(ub.get("edition") or {}).get("reading_format_id") == AUDIO_FORMAT,
        seconds=read.get("progress_seconds"), total_seconds=(ub.get("edition") or {}).get("audio_seconds") or None,
        slug=book.get("slug"), review=(ub.get("review_raw") or "").strip() or None, review_spoilers=bool(ub.get("review_has_spoilers")),
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
            if r.status_code == 429:               # Hardcover allows ~60 req/min, and 5,000 a day
                try:
                    wait = int(r.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0
                if wait > 120:                     # the daily allowance, not the per-minute one
                    raise HardcoverDailyLimit(wait, r.headers.get("X-Ratelimit-Daily-Limit"))
                time.sleep(10 * (attempt + 1)); continue
            if r.status_code == 401:
                raise HardcoverError("Hardcover rejected the token (expired or wrong)")
            if r.status_code == 403:
                raise HardcoverError("Hardcover refused the request (HTTP 403): the token is wrong or expired, "
                                     "or the query isn't allowed")
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

    # ---- your "Owned" list: what Hardcover shows as books you own (user_books.owned isn't connected to it)
    def list_books(self, slug):
        """One of your lists by slug ("owned", "favorites"): (its id, {book id: list_book id}); (None, {}) if none."""
        d = self._q("""query($s: String!) { me { lists(where: {slug: {_eq: $s}}) { id list_books { id book_id } } } }""",
                    {"s": slug})
        me = d["me"][0] if isinstance(d.get("me"), list) else d.get("me") or {}
        lst = (me.get("lists") or [None])[0]
        if not lst:
            return None, {}
        return lst["id"], {lb["book_id"]: lb["id"] for lb in lst.get("list_books") or []}

    def owned_list(self):
        """(the Owned list's id, {book id: list_book id}); (None, {}) if your account has no Owned list."""
        return self.list_books("owned")

    def add_owned(self, book_id, edition_id=None):
        """Put a book on your Owned list. -> True if it was added, False if it was already there. Verifies."""
        list_id, have = self.owned_list()
        if not list_id:
            raise HardcoverError("Your Hardcover account has no Owned list")
        if int(book_id) in have:
            return False
        obj = {"list_id": list_id, "book_id": int(book_id)}
        if edition_id:
            obj["edition_id"] = int(edition_id)
        r = self._q("mutation($o: ListBookInput!) { insert_list_book(object: $o) { id } }", {"o": obj})
        if not (r.get("insert_list_book") or {}).get("id") or int(book_id) not in self.owned_list()[1]:
            raise HardcoverError("Hardcover didn't add the book to your Owned list")
        return True

    def fetch_reviewed(self):
        return self._books({"user_id": {"_eq": self.user_id}, "has_review": {"_eq": True}})

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

    # ---- edition cleanup: the only place Heliograph writes to Hardcover, and only when you press Apply
    def edition_cleanup(self, language):
        """Your books whose Hardcover edition is in another language, each with editions of the same book in
        `language` (most-used first) and a suggestion: the most-used one in the same reading format."""
        want = (language or "").strip().lower()
        if not want:
            return []
        q = """query($u: Int!, $o: Int!) { user_books(where: {user_id: {_eq: $u}}, order_by: {id: asc}, limit: 100, offset: $o) {
          id book_id book { title }
          edition { id title reading_format_id edition_format language { language } } } }"""
        rows, offset = [], 0
        while True:
            page = self._q(q, {"u": self.user_id, "o": offset})["user_books"]
            rows += page
            if len(page) < 100:
                break
            offset += 100
        eq = """query($b: Int!, $l: String!) { editions(where: {book_id: {_eq: $b}, language: {language: {_eq: $l}}},
          order_by: {users_count: desc_nulls_last}, limit: 8) { id title reading_format_id edition_format pages isbn_13 users_count } }"""
        out = []
        for r in rows:
            e = r.get("edition") or {}
            lang = ((e.get("language") or {}).get("language") or "").strip()
            if not lang or lang.lower() == want:
                continue
            cands = self._q(eq, {"b": r["book_id"], "l": language.strip()})["editions"]
            same = [c for c in cands if c.get("reading_format_id") == e.get("reading_format_id")]
            out.append({"id": r["id"], "title": (r.get("book") or {}).get("title") or e.get("title") or "",
                        "current": {"id": e.get("id"), "title": e.get("title"), "language": lang,
                                    "format": FORMAT_NAMES.get(e.get("reading_format_id"), ""), "edition_format": e.get("edition_format")},
                        "candidates": [{"id": c["id"], "title": c.get("title"), "format": FORMAT_NAMES.get(c.get("reading_format_id"), ""),
                                        "edition_format": c.get("edition_format"), "pages": c.get("pages"),
                                        "isbn": c.get("isbn_13"), "users": c.get("users_count")} for c in cands],
                        "suggested": (same or cands or [{}])[0].get("id")})
        return out

    def set_edition(self, ub_id, edition_id, language=None):
        """Point one of your Hardcover books (and any reading session on the old edition) at another edition of the
        same book. Checks the edition really belongs to that book (and is in `language`, if given) before writing."""
        q = """query($id: Int!, $e: Int!) {
          user_books(where: {id: {_eq: $id}}) { id user_id book_id edition_id
            user_book_reads { id edition_id started_at finished_at progress_pages progress_seconds } }
          editions(where: {id: {_eq: $e}}) { id book_id language { language } } }"""
        d = self._q(q, {"id": int(ub_id), "e": int(edition_id)})
        ub, ed = (d.get("user_books") or [None])[0], (d.get("editions") or [None])[0]
        if not ub or ub.get("user_id") != self.user_id:
            raise HardcoverError("That book isn't in your Hardcover library")
        if not ed or ed["book_id"] != ub["book_id"]:
            raise HardcoverError("That edition belongs to a different book")
        got = ((ed.get("language") or {}).get("language") or "")
        if language and got.lower() != language.strip().lower():
            raise HardcoverError(f"That edition is {got or 'of unknown language'}, not {language}")
        old = ub["edition_id"]
        res = self._q("""mutation($id: Int!, $e: Int!) { update_user_book(id: $id, object: {edition_id: $e}) { id error } }""",
                      {"id": int(ub_id), "e": int(edition_id)})["update_user_book"]
        if not res or res.get("error"):
            raise HardcoverError(f"Hardcover refused the change: {(res or {}).get('error') or 'no response'}")
        moved = 0
        for rd in ub.get("user_book_reads") or []:
            if rd.get("edition_id") != old or old == int(edition_id):
                continue
            # the update takes the whole record: send the existing dates and progress back unchanged
            obj = {k: rd.get(k) for k in ("started_at", "finished_at", "progress_pages", "progress_seconds")}
            obj["edition_id"] = int(edition_id)
            r2 = self._q("""mutation($id: Int!, $o: DatesReadInput!) { update_user_book_read(id: $id, object: $o) { id error } }""",
                         {"id": rd["id"], "o": obj})["update_user_book_read"]
            if not r2 or r2.get("error"):
                raise HardcoverError(f"Edition changed, but a reading session wasn't: {(r2 or {}).get('error') or 'no response'}")
            moved += 1
        for key in (("e", old), ("e", int(edition_id))):
            self._ed_cache.pop(key, None)
            self._ed_cache.get("covers", {}).pop(key, None)
        return {"old": old, "new": int(edition_id), "reads_moved": moved}

    # ---- reading dates (from Goodreads): read everything, write one read at a time when you press Apply
    def reading_dates(self):
        """Every book in your library with its edition ISBNs and all its reads (oldest first)."""
        q = """query($u: Int!, $o: Int!) { user_books(where: {user_id: {_eq: $u}}, order_by: {id: asc}, limit: 100, offset: $o) {
          id status_id book { title contributions { contribution author { name } } } edition { isbn_10 isbn_13 }
          user_book_reads(order_by: {id: asc}) { id started_at finished_at } } }"""
        out, offset = [], 0
        while True:
            page = self._q(q, {"u": self.user_id, "o": offset})["user_books"]
            for ub in page:
                b = book_from_ub(ub)
                ed = ub.get("edition") or {}
                out.append({"id": ub["id"], "title": b.title, "authors": b.authors, "status": b.status,
                            "isbns": [i for i in (ed.get("isbn_13"), ed.get("isbn_10")) if i],
                            "reads": [{"id": r["id"], "started": r.get("started_at"), "finished": r.get("finished_at")}
                                      for r in ub.get("user_book_reads") or []]})
            if len(page) < 100:
                return out
            offset += 100

    def set_read_dates(self, ub_id, read_id, started, finished, was=None):
        """Set one read's start and finish dates (read_id None: add a read). `was` = the (started, finished) you
        were shown; if Hardcover has something else now, nothing is written."""
        q = """query($id: Int!) { user_books(where: {id: {_eq: $id}}) { id user_id edition_id
          user_book_reads { id edition_id started_at finished_at progress_pages progress_seconds } } }"""
        ub = (self._q(q, {"id": int(ub_id)}).get("user_books") or [None])[0]
        if not ub or ub.get("user_id") != self.user_id:
            raise HardcoverError("That book isn't in your Hardcover library")
        if read_id is None:
            obj = {"started_at": started, "finished_at": finished, "edition_id": ub.get("edition_id")}
            res = self._q("""mutation($id: Int!, $o: DatesReadInput!) { insert_user_book_read(user_book_id: $id, user_book_read: $o) { id error } }""",
                          {"id": int(ub_id), "o": obj})["insert_user_book_read"]
        else:
            rd = next((r for r in ub.get("user_book_reads") or [] if r["id"] == int(read_id)), None)
            if not rd:
                raise HardcoverError("That read is no longer on the book in Hardcover")
            if was is not None and (rd.get("started_at"), rd.get("finished_at")) != tuple(was):
                raise HardcoverError("Hardcover's dates changed since the scan: scan again")
            # the update takes the whole record: send the rest back unchanged
            obj = {k: rd.get(k) for k in ("edition_id", "progress_pages", "progress_seconds")}
            obj.update(started_at=started, finished_at=finished)
            res = self._q("""mutation($id: Int!, $o: DatesReadInput!) { update_user_book_read(id: $id, object: $o) { id error } }""",
                          {"id": int(read_id), "o": obj})["update_user_book_read"]
        if not res or res.get("error"):
            raise HardcoverError(f"Hardcover refused the change: {(res or {}).get('error') or 'no response'}")
        return res.get("id")

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
