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


def score(title, authors, cand_title, cand_authors):
    """Returns a score, or None if the candidate isn't a confident match."""
    ts = sequence_similarity(match_tokens(title, True), match_tokens(cand_title, True))
    if ts < MIN_TITLE_SIMILARITY:
        return None
    local = match_tokens(" ".join(authors or []))
    theirs = match_tokens(" ".join(cand_authors or []))
    if _useful(local) and _useful(theirs):
        a = set_similarity(local, theirs)
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
