from urllib.parse import urljoin, urlparse
from .. import smartmatch


def clean_cover(url, base=None):
    """Only https image URLs reach the browser."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif base and not url.startswith(("http://", "https://")):
        url = urljoin(base, url)
    return url if url.startswith("https://") else None


class AuthError(Exception):
    """Credentials missing/expired; skip this service for the cycle."""


class ProviderError(Exception):
    pass


class Match:
    """switch_from: your existing entry sits on this other (wrong-language / audio) edition of the same work and
    should be moved to `id` before writing (the engine does it, never in a preview). note: why this edition."""
    def __init__(self, id, pages=None, switch_from=None, note=None):
        self.id, self.pages, self.switch_from, self.note = str(id), pages, switch_from, note


AUDIO_WORDS = ("audio", "audible", "mp3", "cd")


def is_audio(fmt):
    f = (fmt or "").lower()
    return any(w in f for w in AUDIO_WORDS)


def mapped_page(book, remote_pages):
    """Hardcover page -> the linked edition's page scale (ShelfSync's page_mapper)."""
    if book.pages is None:
        return None
    if remote_pages and book.total_pages and remote_pages != book.total_pages:
        frac = min(1.0, book.pages / book.total_pages)
        return min(remote_pages, int(frac * remote_pages + 0.5))
    return book.pages


class Provider:
    key = ""
    label = ""
    SUPPORTED = ("want", "reading", "read")
    tracks_progress = True      # False for services that only store status/rating
    state = None                # set by the engine: a tiny kv store for providers that need to remember things
    waiting_on = None           # set while blocked on something slow (e.g. the Goodreads cookie refresher), for UI messages
    CHECKS_EDITIONS = False     # True when check_edition() can say an edition's language / format

    def __init__(self, config, log):
        self.config, self.log = config, log

    @property
    def cfg(self):
        return self.config[self.key]

    @property
    def enabled(self):
        return bool(self.cfg.get("enabled"))

    def supports(self, status):
        return status in self.SUPPORTED

    # ---- edition preferences (Settings: preferred edition language, avoid audiobook editions)
    def edition_problem(self, language, fmt, want_audio=False):
        """Why an edition with this language/format shouldn't be linked, or None if it's fine.
        want_audio: your Hardcover edition is an audiobook, so audiobook editions are welcome (and preferred).
        Unknown values ("", None, "Not specified") pass: only a known mismatch is refused."""
        c = self.config["common"]
        if is_audio(fmt) and not want_audio and c.get("avoid_audiobooks", True):
            return f"audiobook edition ({fmt})"
        want = (c.get("preferred_language") or "").strip().lower()
        lang = (language or "").strip()
        if want and lang and lang.lower() not in ("not specified", "unknown") and lang.lower() != want:
            return f"{lang} edition"
        return None

    CAN_REMOVE = False          # True when remove_book() is implemented

    def remove_book(self, remote_id):
        """Take the book off your shelves on this service. -> True if removed, False if it wasn't there.
        Raises if the service didn't confirm the removal."""
        raise NotImplementedError

    def check_edition(self, remote_id):
        """-> (language, format) of a linked edition, for the editions check; None if the service can't tell."""
        return None

    HOME = ""     # the service's homepage (dashboard icon link); override per service with the home_url setting

    def home_url(self):
        """Where the dashboard icon links: your own link (Settings) > your profile on the service > its homepage."""
        custom = (self.cfg.get("home_url") or "").strip()
        if custom.startswith(("https://", "http://")):
            return custom
        found = self.state.get(f"profile_url:{self.key}") if self.state else None
        return found if self.is_own_url(found) else (self.HOME or None)

    def profile_url(self):
        """Your own profile page on this service, worked out from the session Heliograph already has (None if
        unknown). Fetched about once a week by the engine and kept in the state store."""
        return None

    def is_own_url(self, url):
        """Only links into this service's own site are used for its icon."""
        home = urlparse(self.HOME or "").hostname or ""
        domain = ".".join(home.split(".")[-2:])
        host = urlparse(url or "").hostname or ""
        return bool(domain) and (url or "").startswith("https://") and (host == domain or host.endswith("." + domain))

    def book_url(self, remote_id):
        """Link to the book's page on the service, for the web UI (None if unknown)."""
        return None

    def parse_ref(self, text):
        """Pull a remote id out of a pasted URL or id (None if it doesn't look like one)."""
        t = (text or "").strip()
        return t if t.isdigit() else None

    def normalize_rating(self, rating):
        """The rating as this service would store it (its steps), for 'already up to date' comparisons."""
        return float(rating)

    def read_state(self, remote_id):
        """What the service holds for this book now, or None if it can't be read:
        {"status": want|reading|read|None, "rating": float|None, "finished_dates": {"YYYY-MM-DD"},
         "known": subset of {"rating", "finished_on"} that is actually readable from this service}"""
        return None

    # --- subclasses implement these
    def check_auth(self) -> str: raise NotImplementedError
    def lookup_isbn(self, isbn, want_audio=False): return None          # -> Match | None
    def search(self, query): return []                # -> [{"id","title","authors","pages"}]
    def set_status(self, remote_id, status, book): raise NotImplementedError
    def set_progress(self, remote_id, book, by_pages, remote_pages): raise NotImplementedError
    def set_rating(self, remote_id, rating, book): raise NotImplementedError

    supports_reviews = False
    def get_review(self, remote_id, book): return None                  # -> your review text there, or None
    def set_review(self, remote_id, text, spoilers, book): raise NotImplementedError

    supports_owned = False                                               # the service can mark a book "owned"
    def set_owned(self, remote_id, book, owned=True):
        """Mark the book as one you own (owned=False: no longer). -> True if it changed, False if it already was.
        Verifies."""
        raise NotImplementedError

    def finalize(self, cand):
        """Hook after a candidate is chosen (e.g. StoryGraph edition resolution). -> Match | None"""
        return Match(cand["id"], cand.get("pages"))

    def search_book(self, title, authors):
        """Candidates for a known book. Services whose search handles "title author" well use the default;
        override it where a smarter query finds more (see Pagebound)."""
        return self.search(" ".join([title] + list(authors)))

    def library(self):
        """Every entry on your own shelves/lists: [{"id", "title", "authors", "isbn"}], or None if the service
        can't list them. Used to link books recorded at the first run to the entries you already have."""
        return None

    SEARCHES = 3          # searches find() may run per book when the first finds nothing usable (fewer where costly)

    def find(self, book, by_isbn=True, by_title=True, matcher=None):
        """ISBN first, then title+author search scored by the smart matcher (see smartmatch). If nothing passes,
        more searches are tried (main title + surname, title alone); the best near miss is left in
        self.last_suggestion for you to accept with one click."""
        matcher = matcher or getattr(self, "matcher", None) or smartmatch.Matcher()
        self.last_suggestion = None
        audio = bool(getattr(book, "audio", False))
        if by_isbn:
            for isbn in dict.fromkeys(filter(None, (book.isbn13, book.isbn10))):
                m = self.lookup_isbn(isbn, want_audio=audio)
                if m:
                    return m
        if not (by_title and book.title.strip()):
            return None
        hints = [i for i in (book.isbn13, book.isbn10) if i]
        pages = getattr(book, "total_pages", None)
        near, seen = None, set()
        for n, query in enumerate(smartmatch.query_variants(book.title, book.authors)[:max(1, self.SEARCHES)]):
            cands = self.search_book(book.title, book.authors) if n == 0 else self.search(query)
            cands = [c for c in cands if str(c.get("id")) not in seen]
            seen.update(str(c.get("id")) for c in cands)
            evals = matcher.evaluate(book.title, book.authors, cands, hints, pages)
            # best first; finalize() may refuse one (audiobook / other language with no usable edition), then try the next
            for r in [r for r in evals if r["ok"]][:5]:
                m = self.finalize(dict(r["cand"], want_isbns=hints, want_audio=audio))
                if m:
                    note = "; ".join(r["reasons"][:3])
                    m.note = f"{m.note}; {note}" if m.note else note
                    return m
            for r in evals:
                if not r["blocked"] and r["confidence"] >= smartmatch.SUGGEST and (not near or r["confidence"] > near["confidence"]):
                    near = r
        if near:
            c = near["cand"]
            self.last_suggestion = {"candidate": c, "id": str(c.get("id")), "title": c.get("title") or "",
                                    "authors": c.get("authors") or [], "confidence": round(near["confidence"], 2),
                                    "reasons": near["reasons"]}
        return None
