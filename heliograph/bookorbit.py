"""BookOrbit metadata cleanup: give every book in your BookOrbit library the right Hardcover book and edition, so
BookOrbit's own Hardcover sync logs your reading against the edition you actually have.

Rules (set by you):
- English editions only, and BookOrbit's language field is normalised to one code ("eng").
- The edition follows your files: an ebook file -> Hardcover's ebook edition; only audio files -> the audiobook
  edition; neither -> print. A book is never put on an audiobook edition when it has an ebook (or there's a print
  one) and audio isn't all you have. When you add an ebook later, the audiobook edition is swapped for the ebook one.
- Wrong links (the Hardcover book isn't this book) are replaced, using the smart matcher's rules.

Nothing is written to BookOrbit until you apply a change (except the ebook swap above, if you turn it on).
"""
import re
import requests

from . import smartmatch
from .hardcover import HardcoverError

EBOOK = {"epub", "kepub", "mobi", "azw3", "azw", "pdf", "fb2", "djvu", "cbz", "cbr", "cb7"}
AUDIO = {"m4b", "mp3", "m4a", "opus", "ogg", "flac", "aac", "wav"}
LANGUAGE = "eng"
ENGLISH = {"eng", "en", "english", "en-us", "en-gb", "en_us", "en_gb", "en-ca", "en-au"}
FORMAT = {1: "print", 2: "audio", 3: "print", 4: "ebook"}         # Hardcover reading_format_id
WANT = {"ebook": (4, 1, 3), "audio": (2, 1, 3, 4), "print": (1, 3, 4)}   # preferred formats, best first


class BookOrbitError(Exception):
    pass


class BookOrbitClient:
    def __init__(self, url, api_key, timeout=60):
        self.url, self.key, self.timeout = (url or "").rstrip("/"), (api_key or "").strip(), timeout
        if not self.url or not self.key:
            raise BookOrbitError("Set the BookOrbit URL and API key in Settings first")

    def _req(self, method, path, **kw):
        try:
            r = requests.request(method, self.url + path, timeout=self.timeout,
                                 headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}, **kw)
        except requests.RequestException as e:
            raise BookOrbitError(f"BookOrbit unreachable at {self.url}: {e}")
        if r.status_code in (401, 403):
            raise BookOrbitError("BookOrbit refused the API key")
        if r.status_code >= 400:
            raise BookOrbitError(f"BookOrbit {method} {path.split('?')[0]} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    def me(self):
        return self._req("GET", "/api/v1/auth/me")

    def books(self):
        out, page = [], 0
        while True:
            d = self._req("POST", "/api/v1/books/query", json={"pagination": {"page": page, "size": 100}})
            out += d.get("items") or []
            if not d.get("items") or len(out) >= (d.get("total") or 0):
                return out
            page += 1

    def update(self, book_id, fields):
        return self._req("PATCH", f"/api/v1/books/{int(book_id)}/metadata", json=fields)


# ---------------------------------------------------------------- what a book is
def kind(book):
    """ebook / audio / print, from the files BookOrbit has for it."""
    fmts = {(f.get("format") or "").lower() for f in book.get("files") or [] if f.get("role") in (None, "primary", "content")}
    if fmts & EBOOK:
        return "ebook"
    if fmts & AUDIO:
        return "audio"
    return "print"


def is_english(lang):
    return (lang or "").strip().lower() in ENGLISH


def signature(book):
    """What a scan checked: if none of it changes, the book needn't be checked again."""
    return "|".join(str(x or "") for x in (kind(book), book.get("hardcoverId"), book.get("hardcoverEditionId"),
                                           book.get("language")))


# ---------------------------------------------------------------- Hardcover lookups (read-only)
class HardcoverLookup:
    """Cached Hardcover reads for the planner."""

    def __init__(self, hc):
        self.hc, self._eds, self._books = hc, {}, {}

    def book(self, ref):
        """A Hardcover book by slug or numeric id: {id, slug, title, authors} or None."""
        key = str(ref)
        if key not in self._books:
            where = {"id": {"_eq": int(key)}} if key.isdigit() else {"slug": {"_eq": key}}
            rows = self.hc._q("""query($w: books_bool_exp!) { books(where: $w, limit: 1) { id slug title
                contributions { contribution author { name } } } }""", {"w": where})["books"]
            self._books[key] = _book(rows[0]) if rows else None
        return self._books[key]

    def editions(self, book_id):
        """The most-used editions of each reading format (a popular book has hundreds; the top 25 per format is plenty)."""
        if book_id not in self._eds:
            fields = "id title isbn_13 isbn_10 reading_format_id edition_format users_count pages language { language }"
            part = lambda name, fmt: (f"{name}: editions(where: {{book_id: {{_eq: $b}}, reading_format_id: {{_eq: {fmt}}}}}, "
                                      f"order_by: {{users_count: desc_nulls_last}}, limit: 25) {{ {fields} }}")
            d = self.hc._q(f"query($b: Int!) {{ {part('p', 1)} {part('a', 2)} {part('e', 4)} {part('u', 3)} }}", {"b": int(book_id)})
            rows = d["p"] + d["a"] + d["e"] + d["u"]
            self._eds[book_id] = [{"id": e["id"], "title": e.get("title"), "isbn13": e.get("isbn_13"), "isbn10": e.get("isbn_10"),
                                   "reading_format_id": _format_id(e), "edition_format": e.get("edition_format"),
                                   "users": e.get("users_count") or 0, "pages": e.get("pages"),
                                   "language": (e.get("language") or {}).get("language")} for e in rows]
            for e in self._eds[book_id]:
                e["format"] = FORMAT.get(e["reading_format_id"])
        return self._eds[book_id]

    def by_isbn(self, isbn):
        rows = self.hc._q("""query($i: String!) { editions(where: {_or: [{isbn_13: {_eq: $i}}, {isbn_10: {_eq: $i}}]}, limit: 1)
            { id book { id slug title contributions { contribution author { name } } } } }""", {"i": isbn})["editions"]
        return _book(rows[0]["book"]) if rows and rows[0].get("book") else None

    def search(self, title, authors):
        q = f"{title} {' '.join(authors[:1])}".strip()
        found = (self.hc._q("""query($q: String!) { search(query: $q, query_type: "Book", per_page: 10) { results } }""",
                            {"q": q}).get("search") or {}).get("results")   # Hardcover sends null for some queries
        hits = (found.get("hits") if isinstance(found, dict) else None) or []
        out = []
        for h in hits:
            d = h.get("document") or {}
            if d.get("id"):
                out.append({"id": int(d["id"]), "slug": d.get("slug"), "title": d.get("title") or "",
                            "authors": d.get("author_names") or [], "users": d.get("users_count") or 0,
                            "pages": d.get("pages"), "isbns": [str(i) for i in d.get("isbns") or []],
                            "cover": (d.get("image") or {}).get("url") if isinstance(d.get("image"), dict) else None})
        return out

    def edition(self, edition_id):
        """One edition by id, with its book: (edition, book) or (None, None)."""
        rows = self.hc._q("""query($e: Int!) { editions(where: {id: {_eq: $e}}, limit: 1) { id title isbn_13 isbn_10
            reading_format_id edition_format users_count pages language { language }
            book { id slug title contributions { contribution author { name } } } } }""", {"e": int(edition_id)})["editions"]
        if not rows or not rows[0].get("book"):
            return None, None
        e = rows[0]
        ed = {"id": e["id"], "title": e.get("title"), "isbn13": e.get("isbn_13"), "isbn10": e.get("isbn_10"),
              "reading_format_id": _format_id(e), "edition_format": e.get("edition_format"), "users": e.get("users_count") or 0,
              "pages": e.get("pages"), "language": (e.get("language") or {}).get("language")}
        ed["format"] = FORMAT.get(ed["reading_format_id"])
        return ed, _book(e["book"])


HC_URL = re.compile(r"hardcover\.app/books/([^/?#\s]+)(?:/editions/(\d+))?", re.I)


def parse_ref(ref):
    """What you pasted -> ("edition", id) or ("book", slug-or-id). Accepts a Hardcover book or edition URL,
    "edition:123", a slug, or a book id."""
    ref = (ref or "").strip()
    m = HC_URL.search(ref)
    if m:
        return ("edition", m.group(2)) if m.group(2) else ("book", m.group(1))
    m = re.fullmatch(r"(?:edition[:\s#]*)(\d+)", ref, re.I)
    if m:
        return "edition", m.group(1)
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", ref, re.I):
        return "book", ref.lower() if not ref.isdigit() else ref
    raise ValueError("Paste a Hardcover book or edition URL, a book slug or id, or edition:<id>")


def _format_id(e):
    """Hardcover's reading format, corrected by the edition's own label when they disagree ("Kindle Edition" filed
    as print, "Audible Audio" filed as print)."""
    label = (e.get("edition_format") or "").lower()
    if re.search(r"kindle|e-?book|epub|digital|nook|kobo", label) and "audio" not in label:
        return 4
    if re.search(r"audible|audiobook|audio|mp3|cassette|\bcd\b", label):
        return 2
    return e.get("reading_format_id")


def _book(row):
    authors = [c["author"]["name"] for c in row.get("contributions") or []
               if c.get("author") and (c.get("contribution") or "").strip().lower() in ("", "author")]
    return {"id": row["id"], "slug": row.get("slug"), "title": row.get("title") or "", "authors": authors}


def pick_edition(editions, want, isbns=()):
    """The edition to use: English, in the format your files call for (falling back as WANT says); your own ISBN
    first when it fits, else the most used. None if there's no English edition at all."""
    for fmt in WANT[want]:
        same = [e for e in editions if e["reading_format_id"] == fmt]
        # labelled English first; Hardcover often leaves the language blank, so those come next (never another language)
        for pool in ([e for e in same if is_english(e["language"])], [e for e in same if not e["language"]]):
            if pool:
                mine = [e for e in pool if {e["isbn13"], e["isbn10"]} & set(isbns or ())]
                if fmt == 2:                      # a digital audiobook before cassettes and CDs
                    pool = sorted(pool, key=lambda e: bool(re.search(r"cassette|\bcd\b|compact disc|vinyl|mp3 cd",
                                                                      e["edition_format"] or "", re.I)))
                return (mine or pool)[0]
    return None


# ---------------------------------------------------------------- the plan for one book
def plan_book(bo, look, matcher=None):
    """-> {"id", "title", "authors", "kind", "changes": {field: value}, "why": [..], "kind_of_fix": str, "problem": str|None}"""
    matcher = matcher or smartmatch.Matcher()
    title = bo.get("title") or ""
    authors = list(bo.get("authors") or [])
    isbns = [i for i in (bo.get("isbn13"), bo.get("isbn10")) if i]
    want = kind(bo)
    out = {"id": bo["id"], "title": title, "authors": authors, "kind": want, "changes": {}, "why": [], "problem": None,
           "isbn13": bo.get("isbn13"), "isbn10": bo.get("isbn10"),
           "source": {k: bo.get(k) for k in ("files", "hardcoverId", "hardcoverEditionId", "language")},
           "current": {"hardcoverId": bo.get("hardcoverId"), "hardcoverEditionId": bo.get("hardcoverEditionId"),
                       "language": bo.get("language")}}
    locked = set(bo.get("lockedFields") or [])

    def fits(hb):
        if not hb:
            return None
        r = matcher.evaluate(title, authors, [hb])[0]
        return r if r["ok"] else None

    # 1. the Hardcover book: the current link if it's right, else ISBN, else a search
    current = look.book(bo["hardcoverId"]) if bo.get("hardcoverId") else None
    book, how = None, None
    if current and fits(current):
        book, how = current, "kept"
    else:
        for isbn in isbns:
            hb = look.by_isbn(isbn)
            if fits(hb):
                book, how = hb, "isbn"
                break
        if not book:
            cands = look.search(smartmatch.clean_title(title), authors)
            for c in cands:                     # the search result lists the book's ISBNs: yours among them is evidence
                c["isbn"] = next((i for i in isbns if i in c.get("isbns", ())), None)
            ranked = matcher.evaluate(title, authors, cands, isbns, bo.get("pageCount"))
            english = lambda c: pick_edition(look.editions(c["id"]), want, isbns) is not None
            best = next((r for r in ranked if r["ok"] and english(r["cand"])), None)
            if best:
                book, how = best["cand"], "search"
            else:
                near = next((r for r in ranked if not r["blocked"] and r["confidence"] >= smartmatch.SUGGEST
                             and english(r["cand"])), None)
                if near:
                    out["suggestion"] = {"id": near["cand"]["id"], "slug": near["cand"].get("slug"),
                                         "title": near["cand"]["title"], "authors": near["cand"]["authors"],
                                         "confidence": round(near["confidence"], 2), "reasons": near["reasons"]}
    if current and how != "kept":
        out["why"].append(f"linked to “{current['title']}” by {', '.join(current['authors'][:2]) or '?'}, which isn't this book")
    elif bo.get("hardcoverId") and not current:
        out["why"].append(f"linked to Hardcover “{bo['hardcoverId']}”, which doesn't exist")

    # 2. the edition
    if book:
        ed = pick_edition(look.editions(book["id"]), want, isbns)
        if not ed:
            out["problem"] = "Hardcover has no English edition of it"
        else:
            if str(book.get("slug") or book["id"]) != str(bo.get("hardcoverId") or "") and "hardcoverId" not in locked:
                out["changes"]["hardcoverId"] = book.get("slug") or str(book["id"])
                if not bo.get("hardcoverId"):
                    out["why"].append(f"found on Hardcover by {'ISBN' if how == 'isbn' else 'title and author'}")
            if str(ed["id"]) != str(bo.get("hardcoverEditionId") or "") and "hardcoverEditionId" not in locked:
                out["changes"]["hardcoverEditionId"] = str(ed["id"])
                fmt = {"ebook": "ebook", "audio": "audiobook", "print": "print"}[ed["format"] or "print"]
                if want != (ed["format"] or "print"):
                    note = f" (no English {'ebook' if want == 'ebook' else 'audiobook'} edition on Hardcover)"
                else:
                    note = ""
                out["why"].append(f"{fmt} edition{note}: {ed['edition_format'] or ed['title'] or ed['id']}"
                                  + (f", ISBN {ed['isbn13']}" if ed["isbn13"] else ""))
            out["edition"] = ed
            out["book"] = book
    elif not out.get("suggestion"):
        out["problem"] = "not found on Hardcover"
    else:
        out["problem"] = "no confident match on Hardcover"

    # 3. language
    if (bo.get("language") or "") != LANGUAGE and "language" not in locked:
        if not bo.get("language") or is_english(bo.get("language")):
            out["changes"]["language"] = LANGUAGE
            out["why"].append(f"language “{bo.get('language') or 'none'}” → “{LANGUAGE}”")
        else:
            out["problem"] = out["problem"] or f"language is “{bo['language']}”, not English: check the book"

    c = out["changes"]
    out["fix"] = ("relink" if current and how != "kept" and "hardcoverId" in c else
                  "link" if "hardcoverId" in c else
                  "edition" if "hardcoverEditionId" in c else
                  "language" if c else "ok")
    return out
