"""Title/author matching, ported from ShelfSync's common/base_provider.lua (titleAuthorMatchScore)."""
import re

MIN_TITLE_SIMILARITY = 0.8
MIN_TITLE_SIMILARITY_WITHOUT_AUTHOR = 0.95
MIN_AUTHOR_SIMILARITY = 0.7
_PUNCT = re.compile(r"[!-/:-@\[-`{-~\x00-\x1f\x7f]")


def match_tokens(value, is_title=False):
    if not isinstance(value, str):
        return []
    v = (value.lower().replace("’", "'").replace("‘", "'").replace("‑", "-").replace("–", "-")
         .replace("—", "-").replace("&", " and ").replace("'", ""))
    tokens = _PUNCT.sub(" ", v).split()
    if is_title and len(tokens) > 1 and tokens[0] in ("a", "an", "the"):
        tokens = tokens[1:]
    return tokens


def sequence_similarity(left, right):
    if not left or not right:
        return 0.0
    prev = list(range(len(right) + 1))
    for row in range(1, len(left) + 1):
        cur = [row] + [0] * len(right)
        for col in range(1, len(right) + 1):
            sub = 0 if left[row - 1] == right[col - 1] else 1
            cur[col] = min(prev[col] + 1, cur[col - 1] + 1, prev[col - 1] + sub)
        prev = cur
    return 1 - prev[len(right)] / max(len(left), len(right))


def set_similarity(left, right):
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def _useful(tokens):
    return bool(tokens) and tokens[0] != "unknown"


_SUBTITLE = re.compile(r"\s*(?::|\s[-–—]\s|,\s*or\b|\(|\[)", re.I)


def main_title(title):
    """The title without its subtitle: 'The Hobbit, or There and Back Again' -> 'The Hobbit'."""
    t = title if isinstance(title, str) else ""
    m = _SUBTITLE.search(t)
    return t[:m.start()] if m and m.start() > 0 else t


def author_similarity(authors, cand_authors):
    """All names compared together, or one name on each side when a service lists fewer authors than Hardcover
    (Pagebound and others often keep only the main author of a co-written book)."""
    local, theirs = match_tokens(" ".join(authors or [])), match_tokens(" ".join(cand_authors or []))
    if not (_useful(local) and _useful(theirs)):
        return None
    best = set_similarity(local, theirs)
    for a in authors or []:
        for c in cand_authors or []:
            best = max(best, set_similarity(match_tokens(a), match_tokens(c)))
    return best


def score(title, authors, cand_title, cand_authors):
    """Returns a score, or None if the candidate isn't a confident match."""
    ts = sequence_similarity(match_tokens(title, True), match_tokens(cand_title, True))
    a = author_similarity(authors, cand_authors)
    if ts < MIN_TITLE_SIMILARITY and a is not None and a >= MIN_AUTHOR_SIMILARITY:
        # same author, and Hardcover's title carries a subtitle the service leaves off ("The Hobbit, or There and
        # Back Again" vs "The Hobbit"). Only ours is shortened: shortening theirs would let "The Hobbit (Part 1 and 2)
        # Slipcase" or "Vol. 1" pass for the book. Never applied without an author match.
        mt = sequence_similarity(match_tokens(main_title(title), True), match_tokens(cand_title, True))
        if mt >= MIN_TITLE_SIMILARITY_WITHOUT_AUTHOR:
            ts = MIN_TITLE_SIMILARITY            # a match, but ranked below any full-title match
    if ts < MIN_TITLE_SIMILARITY:
        return None
    if a is not None:
        if a < MIN_AUTHOR_SIMILARITY:
            return None
        return ts * 0.75 + a * 0.25
    return ts if ts >= MIN_TITLE_SIMILARITY_WITHOUT_AUTHOR else None


def best_match(title, authors, candidates):
    """candidates: dicts with at least title and authors (list of names)."""
    best, best_score = None, None
    for c in candidates:
        s = score(title, authors, c.get("title"), c.get("authors"))
        if s is not None and (best_score is None or s > best_score):
            best, best_score = c, s
    return best
