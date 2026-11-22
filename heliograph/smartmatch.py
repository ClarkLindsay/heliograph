"""Smart matching: is this search result the book you have in Hardcover?

Runs entirely here: no network, no external model, no extra libraries. Three layers:

1. Normalisation. "Vol. 2", "Volume II", "Book 2" and "#2" all become the same volume number; series tags such as
   "(Mistborn, #2)", "A Novel" and edition noise are dropped before titles are compared.
2. Hard rules for things that are never the same book: a different volume number, a box set / omnibus / study
   guide / summary / dramatised adaptation when you have the book itself, or a different author.
3. A small learned model (logistic regression over a handful of features) that scores what's left, gives a
   confidence and the reasons for it, and learns from the editions you pick by hand ("Find edition…").

Auto-linking still needs a candidate that passes the same strict title/author checks as before; the model ranks
those, and turns near misses into suggestions you can accept with one click instead of searching yourself.
"""
import math
import re

from .matching import match_tokens, sequence_similarity, author_similarity, MIN_AUTHOR_SIMILARITY

ACCEPT_TITLE = 0.8              # full-title similarity needed to link automatically (with a matching author)
ACCEPT_TITLE_NO_AUTHOR = 0.95   # ...when either side has no usable author
SUGGEST = 0.5                   # confidence needed to offer a near miss as a suggestion
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
         "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15}
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
VOL_WORDS = r"(?:vol(?:ume)?s?|book|bk|part|pt|no|number|tome|issue|#)"

# what kind of thing a title is, beyond "the book": (kind, pattern, hard). Hard kinds are never auto-linked to a
# plain book and never suggested; soft kinds only lower the confidence.
KINDS = [
    ("collection", r"\b(box(ed)?[ -]?set|boxset|collection set|omnibus|complete (\w+ ){0,2}(series|collection|set|works|novels|saga|trilogy)"
                   r"|\d+[ -]book (set|collection|bundle)|\d[ -‐]in[ -‐]1|trilogy set|series set|collected works"
                   r"|anthology|slipcase|gift set)\b", True),
    ("range", r"(#\s*\d+\s*-\s*\d+|\bbooks? \d+\s*-\s*\d+)", False),
    ("study", r"\b(study guide|summary (of|and analysis)|analysis of|sparknotes|cliffsnotes|cliffs notes|"
              r"study companion|book ?rags|reader'?s guide|discussion guide|workbook|teacher'?s guide|"
              r"lesson plans?|articles on|works by)\b", True),
    ("adaptation", r"\b(dramati[sz]ed adaptation|full[- ]cast|radio (drama|play)|audio drama|screenplay)\b", True),
    ("companion", r"\b(companion|concordance|encyclopedia|the art of|coloring book|colouring book|journal|"
                  r"cookbook|guide to)\b", False),
    ("graphic", r"\b(graphic novel|comic adaptation|manga edition)\b", False),
    ("special", r"\b(illustrated|annotated|deluxe|collector'?s|anniversary|large print|abridged)\b", False),
]
_KIND_RX = [(k, re.compile(p, re.I), hard) for k, p, hard in KINDS]
BAD_AUTHORS = re.compile(r"^(unknown( author)?|anonymous|books llc|various|n/?a|)$", re.I)

DEFAULT_WEIGHTS = {
    "bias": -6.0, "title": 7.0, "main_title": 4.0, "author": 3.0, "no_author": 1.2, "isbn": 4.0,
    "extra_volume": -2.5, "soft_kind": -1.5, "subtitle_only": -1.0, "pages": 0.8, "rank": 0.6,
}
LEARN_RATE = 0.15
MAX_DRIFT = 1.5                 # learned weights stay within this distance of the defaults


# ---------------------------------------------------------------- normalisation
_SERIES_TAG = re.compile(r"\s*\((?=[^()]*(#|\bbook\s+\d|\bvol))[^()]*\)\s*$", re.I)      # "(Mistborn, #2)"
_NOISE = re.compile(r"\s*[\(\[](hardcover|paperback|kindle edition|ebook|mass market|unabridged|"
                    r"[^()\[\]]*edition)[\)\]]\s*$|:\s*a novel\s*$|\s*\ba novel\s*$", re.I)
_SUBTITLE = re.compile(r"\s*(?::|\s[-–—]\s|,\s*or\b|\(|\[)", re.I)


_EDITION_NOTE = re.compile(r"\s*[\(\[][^()\[\]]*\b(\d+\s*[-‐ ]\s*in\s*[-‐ ]\s*\d+|edition|omnibus)\b[^()\[\]]*[\)\]]", re.I)


def clean_title(title):
    t = _EDITION_NOTE.sub("", (title or "").strip())       # "(2-in-1 Omnibus)", "(3-in-1 Edition)", "[Deluxe Edition]"
    for _ in range(3):
        before = t
        t = _SERIES_TAG.sub("", t)
        t = _NOISE.sub("", t).strip()
        if t == before:
            break
    return t


def volume_numbers(title):
    """Volume/part numbers in a title: 'White Sand, Vol. 2' -> {2}; 'Volume II' -> {2}; 'Book One' -> {1}."""
    t = (title or "").lower().replace("’", "'")
    found = set()
    for m in re.finditer(VOL_WORDS + r"\.?\s*(\d+|[ivxl]+|one|two|three|four|five|six|seven|eight|nine|ten)\b", t):
        v = m.group(1)
        n = int(v) if v.isdigit() else ROMAN.get(v) or WORDS.get(v)
        if n is not None:
            found.add(n)
    m = re.search(r"[,:]\s*(\d{1,3})\s*$", t)                 # "Fullmetal Alchemist, 2"
    if m:
        found.add(int(m.group(1)))
    return found


def _tokens(title):
    """Comparable title tokens: series tags dropped, volume words unified, roman numerals as digits."""
    out = []
    for tok in match_tokens(clean_title(title), True):
        if re.fullmatch(VOL_WORDS.replace("#|", ""), tok) or tok in ("vol", "volume", "vols", "volumes"):
            out.append("vol")
        elif tok in ROMAN and out and out[-1] == "vol":
            out.append(str(ROMAN[tok]))
        elif tok in WORDS and out and out[-1] == "vol":
            out.append(str(WORDS[tok]))
        else:
            out.append(tok)
    return out


def _sim(a, b):
    return sequence_similarity(a, b)


def main_title(title):
    t = clean_title(title)
    m = _SUBTITLE.search(t)
    return t[:m.start()] if m and m.start() > 0 else t


def kinds(title, authors=()):
    """{kind: hard} for the special kinds a title (or its authors) suggests."""
    text = f"{title or ''} {' '.join(authors or [])}"
    return {k: hard for k, rx, hard in _KIND_RX if rx.search(text)}


def _useful_authors(authors):
    return [a for a in authors or [] if a and not BAD_AUTHORS.match(a.strip())]


# ---------------------------------------------------------------- features and rules
def assess(title, authors, cand, isbns=(), pages=None, rank=0):
    """-> {"ok": passes the strict auto-link checks, "blocked": a reason it can never be the book (or None),
           "features": {...}, "reasons": [str]}"""
    ctitle, cauthors = cand.get("title") or "", _useful_authors(cand.get("authors"))
    ours, theirs = _tokens(title), _tokens(ctitle)
    t = _sim(ours, theirs)
    mo, mt = _tokens(main_title(title)), _tokens(main_title(ctitle))
    main = max(_sim(mo, theirs), _sim(ours, mt), _sim(mo, mt))
    a = author_similarity(_useful_authors(authors), cauthors)
    reasons, blocked = [], None

    nv, cv = volume_numbers(clean_title(title)), volume_numbers(clean_title(ctitle))     # not series tags: "(Dune, #1)"
    extra_volume = 0.0
    if nv and cv and not (nv & cv):
        blocked = f"volume {', '.join(map(str, sorted(cv)))}, not {', '.join(map(str, sorted(nv)))}"
    elif cv and not nv:
        extra_volume = 1.0
        reasons.append(f"it's volume {min(cv)}; yours has no volume number")

    ok_kinds, their_kinds = kinds(title, authors), kinds(ctitle, cauthors)
    soft = 0.0
    for k, hard in their_kinds.items():
        if k in ok_kinds:
            continue
        if hard and not blocked:
            blocked = {"collection": "a box set / collection", "study": "a study guide or summary",
                       "adaptation": "a dramatised adaptation"}[k]
        elif not hard:
            soft = 1.0
            reasons.append({"companion": "looks like a companion book", "graphic": "a graphic-novel adaptation",
                            "special": "a special edition", "range": "covers several books of a series"}[k])
    if a is not None and a < MIN_AUTHOR_SIMILARITY and not blocked:
        blocked = "a different author"

    isbn = 1.0 if cand.get("isbn") and str(cand["isbn"]) in {str(i) for i in isbns or ()} else 0.0
    if isbn:
        reasons.append("same ISBN")
    subtitle_only = 1.0 if t < ACCEPT_TITLE and main >= 0.95 else 0.0
    if subtitle_only:
        reasons.append("same title apart from a subtitle")
    elif t >= 0.999:
        reasons.append("same title")
    elif t >= ACCEPT_TITLE:
        reasons.append(f"title {round(t * 100)}% alike")
    else:
        reasons.append(f"title only {round(max(t, main) * 100)}% alike")
    if a is None:
        reasons.append("no author to compare")
    elif a >= 0.999:
        reasons.append("same author")
    elif a >= MIN_AUTHOR_SIMILARITY:
        reasons.append("author matches")
    cp = cand.get("pages")
    page_fit = 0.0
    if pages and cp:
        try:
            ratio = min(float(pages), float(cp)) / max(float(pages), float(cp))
            page_fit = 1.0 if ratio >= 0.85 else 0.0
            if ratio < 0.5:
                reasons.append(f"{cp} pages vs your {pages}")
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # Hardcover's title is the longer one ("The Hobbit, or There and Back Again" vs "The Hobbit"): fine, as before.
    # The service's is ("Way of Kings: Prime" vs "The Way of Kings"): could be another book, so it only links
    # automatically with independent evidence (the same ISBN, or a page count that fits); otherwise it's suggested.
    ours_shortened = _sim(mo, theirs) >= 0.95
    if a is not None:
        ok = (t >= ACCEPT_TITLE or ours_shortened) and a >= MIN_AUTHOR_SIMILARITY
        if not ok and main >= 0.95 and a >= MIN_AUTHOR_SIMILARITY and (isbn or page_fit):
            ok = True
            reasons.append("the ISBN or page count confirms it")
    else:
        ok = t >= ACCEPT_TITLE_NO_AUTHOR
    ok = ok and not blocked and not extra_volume
    # similarity cubed: 0.9 alike is close, 0.6 alike is usually another book (often the next in the series)
    features = {"bias": 1.0, "title": t ** 3, "main_title": main ** 3, "author": a or 0.0, "no_author": 1.0 if a is None else 0.0,
                "isbn": isbn, "extra_volume": extra_volume, "soft_kind": soft, "subtitle_only": subtitle_only,
                "pages": page_fit, "rank": 1.0 / (1 + rank)}
    return {"ok": ok, "blocked": blocked, "features": features, "reasons": reasons}


def confidence(features, weights=None):
    w = weights or DEFAULT_WEIGHTS
    z = sum(w.get(k, 0.0) * v for k, v in features.items())
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


class Matcher:
    """Scores candidates with the default weights, or ones learned from your own choices."""

    def __init__(self, weights=None):
        self.weights = dict(DEFAULT_WEIGHTS)
        for k, v in (weights or {}).items():
            if k in DEFAULT_WEIGHTS and isinstance(v, (int, float)):
                self.weights[k] = float(v)

    def evaluate(self, title, authors, candidates, isbns=(), pages=None):
        """Every candidate with its verdict, best first: dicts {cand, ok, blocked, confidence, reasons, features}."""
        out = []
        for i, c in enumerate(candidates):
            r = assess(title, authors, c, isbns, pages, rank=i)
            r.update(cand=c, confidence=0.0 if r["blocked"] else confidence(r["features"], self.weights), index=i)
            out.append(r)
        out.sort(key=lambda r: (not r["ok"], -r["confidence"], r["index"]))
        return out

    def ranked(self, title, authors, candidates, isbns=(), pages=None):
        """Candidates confident enough to link automatically, best first."""
        return [r["cand"] for r in self.evaluate(title, authors, candidates, isbns, pages) if r["ok"]]

    def suggestion(self, title, authors, candidates, isbns=(), pages=None):
        """The best near miss worth showing you (never a blocked one), or None."""
        for r in self.evaluate(title, authors, candidates, isbns, pages):
            if not r["blocked"] and r["confidence"] >= SUGGEST:
                return r
        return None

    def learn(self, title, authors, candidates, chosen_index, isbns=(), pages=None):
        """You picked candidates[chosen_index]: move the weights so it scores above the ones ranked over it.
        Logistic-regression steps, kept within MAX_DRIFT of the defaults. Returns True if anything changed."""
        if not 0 <= chosen_index < len(candidates):
            return False
        evals = {r["index"]: r for r in self.evaluate(title, authors, candidates, isbns, pages)}
        pos = evals[chosen_index]
        if pos["blocked"]:
            return False                              # a rule says no; one pick doesn't overrule it
        negs = [r for r in evals.values() if r["index"] != chosen_index and not r["blocked"]
                and r["confidence"] >= pos["confidence"]]
        changed = False
        for r, label in [(pos, 1.0)] + [(n, 0.0) for n in negs]:
            err = label - confidence(r["features"], self.weights)
            for k, v in r["features"].items():
                if k in DEFAULT_WEIGHTS and v:
                    new = self.weights[k] + LEARN_RATE * err * v
                    lo, hi = DEFAULT_WEIGHTS[k] - MAX_DRIFT, DEFAULT_WEIGHTS[k] + MAX_DRIFT
                    new = max(lo, min(hi, new))
                    if abs(new - self.weights[k]) > 1e-9:
                        self.weights[k] = new; changed = True
        return changed


def query_variants(title, authors):
    """Searches to try, best first, when the plain "title author" search finds nothing usable."""
    au = _useful_authors(authors)
    surname = au[0].split()[-1] if au else ""
    full, main = clean_title(title), main_title(title)
    out = [f"{full} {' '.join(au)}".strip()]
    if main and main != full:
        out.append(f"{main} {surname}".strip())
    out.append(full)
    if surname:
        out.append(f"{full} {surname}")
    seen, uniq = set(), []
    for q in out:
        k = q.lower()
        if q and k not in seen:
            seen.add(k); uniq.append(q)
    return uniq
