"""Reading dates from Goodreads -> Hardcover: pair each Goodreads book with your Hardcover copy, then each Goodreads
reading session with a Hardcover read, and propose the changes. Nothing here talks to a service."""
import re
from .matching import best_match

FULL = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def full(d):
    return bool(d and FULL.match(d))


def gr_title(t):
    """'A Game of Thrones (A Song of Ice and Fire, #1)' -> 'A Game of Thrones'."""
    return re.sub(r"\s*\([^()]*#\s*[\d.]+[^()]*\)\s*$", "", t or "").strip()


def gr_author(a):
    """'Martin, George R.R. *' -> 'George R.R. Martin'."""
    a = re.sub(r"\s*\*+\s*$", "", (a or "").strip())
    last, sep, first = a.partition(", ")
    return f"{first} {last}".strip() if sep else a


def pair_books(gr_rows, hc_books, linked):
    """-> [(hc_book, gr_row)], one per Hardcover book. Each Goodreads row is matched by the edition Heliograph linked,
    then ISBN, then title and author. Several Goodreads editions of one book (shelved twice) are merged: their reads
    are combined. `linked`: Goodreads book id -> Hardcover user_book id."""
    by_ub = {b["id"]: b for b in hc_books}
    by_isbn = {}
    for b in hc_books:
        for i in b.get("isbns") or ():
            by_isbn.setdefault(i, b)
    found = {}                                               # ub id -> Goodreads rows
    for g in gr_rows:
        b = by_ub.get(linked.get(g["id"])) or next((by_isbn[i] for i in g.get("isbns") or () if i in by_isbn), None) \
            or best_match(gr_title(g["title"]), [gr_author(a) for a in g.get("authors") or ()], hc_books)
        if b:
            found.setdefault(b["id"], []).append(g)
    pairs = []
    for ub, rows in found.items():
        reads = []
        for g in rows:
            reads += [r for r in g["reads"] if r not in reads]
        pairs.append((by_ub[ub], dict(rows[0], reads=reads)))
    return pairs


def plan(hc_reads, gr_reads):
    """Proposed changes for one book. hc_reads: [{id, started, finished}]; gr_reads: [{started, finished}].
    Each change: {op: start|dates|add, read_id, started, finished, was: (started, finished), pick, why}."""
    out = []
    used = set()
    gr = []
    for g in gr_reads:
        s, f = g.get("started"), g.get("finished")
        s, f = (s if full(s) else None), (f if full(f) else None)
        if s and f and s > f:
            s = None                                        # a start after the finish is a typo: keep the finish only
        if s or f:
            gr.append({"started": s, "finished": f})
    rest = []
    for g in gr:
        r = None
        if g["finished"]:
            r = next((r for r in hc_reads if r["id"] not in used and r.get("finished") == g["finished"]), None)
        else:                                               # still reading on Goodreads: the open Hardcover read
            r = next((r for r in hc_reads if r["id"] not in used and not r.get("finished")), None)
        if not r:
            rest.append(g); continue
        used.add(r["id"])
        if g["started"] and g["started"] != r.get("started"):
            same_day = g["started"] == g["finished"]
            out.append({"op": "start", "read_id": r["id"], "started": g["started"], "finished": r.get("finished"),
                        "was": (r.get("started"), r.get("finished")), "pick": not r.get("started") and not same_day,
                        "why": "Goodreads started and finished it the same day (often a placeholder)" if same_day else
                               "replaces the start date Hardcover has" if r.get("started") else "start date missing"})
    left = [r for r in hc_reads if r["id"] not in used]
    if len(rest) == 1 and len(left) == 1 and rest[0]["finished"] and left[0].get("finished"):
        g, r = rest[0], left[0]                             # one read each, finish dates differ
        out.append({"op": "dates", "read_id": r["id"], "started": g["started"] or r.get("started"), "finished": g["finished"],
                    "was": (r.get("started"), r.get("finished")), "pick": False, "why": "finish dates differ"})
    else:
        for g in rest:
            if not g["finished"]:
                continue
            same_day = g["started"] == g["finished"]
            out.append({"op": "add", "read_id": None, "started": g["started"], "finished": g["finished"], "was": None,
                        "pick": not hc_reads and not same_day,
                        "why": "Hardcover has no reads of it" if not hc_reads else "a read Hardcover doesn't have"})
    return out
