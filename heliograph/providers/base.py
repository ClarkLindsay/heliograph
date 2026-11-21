from urllib.parse import urljoin
from .. import matching


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
    def __init__(self, id, pages=None):
        self.id, self.pages = str(id), pages


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

    HOME = ""     # the service's homepage (dashboard icon link); override per service with the home_url setting

    def home_url(self):
        custom = (self.cfg.get("home_url") or "").strip()
        if custom.startswith(("https://", "http://")):
            return custom
        return self.HOME or None

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
    def lookup_isbn(self, isbn): return None          # -> Match | None
    def search(self, query): return []                # -> [{"id","title","authors","pages"}]
    def set_status(self, remote_id, status, book): raise NotImplementedError
    def set_progress(self, remote_id, book, by_pages, remote_pages): raise NotImplementedError
    def set_rating(self, remote_id, rating, book): raise NotImplementedError

    def finalize(self, cand):
        """Hook after a candidate is chosen (e.g. StoryGraph edition resolution). -> Match | None"""
        return Match(cand["id"], cand.get("pages"))

    def find(self, book, by_isbn=True, by_title=True):
        """ISBN first, then scored title+author search, like the plugin's autolink order."""
        if by_isbn:
            for isbn in dict.fromkeys(filter(None, (book.isbn13, book.isbn10))):
                m = self.lookup_isbn(isbn)
                if m:
                    return m
        if by_title and book.title.strip():
            cands = self.search(" ".join([book.title] + book.authors))
            best = matching.best_match(book.title, book.authors, cands)
            if best:
                return self.finalize(best)
        return None
