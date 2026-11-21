import json, sqlite3, threading
from pathlib import Path
from .hardcover import Book


class State:
    """What we last pushed to each service per Hardcover user_book, plus a local copy of the library for the web UI."""
    LINK_COLS = ("remote_id", "remote_pages", "status", "pages", "rating", "pushed_at", "link_failed_at",
                 "manual", "skip", "error", "error_at")
    DEFAULT = dict(remote_id=None, remote_pages=None, status=None, pages=None, rating=None, pushed_at=0,
                   link_failed_at=0, manual=0, skip=0, error=None, error_at=0)

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.RLock()
        self.db.executescript("""
          create table if not exists kv(k text primary key, v text);
          create table if not exists links(
            ub_id integer, service text, remote_id text, remote_pages integer, status text, pages integer,
            rating real, pushed_at real default 0, link_failed_at real default 0,
            manual integer default 0, skip integer default 0, error text, error_at real default 0,
            primary key(ub_id, service));
          create table if not exists books(
            ub_id integer primary key, title text, authors text, status text, pages integer, total_pages integer,
            rating real, isbn13 text, isbn10 text, book_id integer, edition_id integer, finished_on text, seen_at real);""")
        have = {r[1] for r in self.db.execute("pragma table_info(links)")}      # upgrade databases from older versions
        for name, ddl in (("manual", "integer default 0"), ("skip", "integer default 0"),
                          ("error", "text"), ("error_at", "real default 0")):
            if name not in have:
                self.db.execute(f"alter table links add column {name} {ddl}")
        self.db.commit()

    # ---- kv
    def get(self, k, default=None):
        with self.lock:
            r = self.db.execute("select v from kv where k=?", (k,)).fetchone()
        return r[0] if r else default

    def set(self, k, v):
        with self.lock:
            self.db.execute("insert or replace into kv values(?,?)", (k, v)); self.db.commit()

    # ---- links
    def _row(self, r):
        d = dict(zip(self.LINK_COLS, r))
        for k in ("manual", "skip", "pushed_at", "link_failed_at", "error_at"):
            d[k] = d[k] or 0
        return d

    def link(self, ub_id, service):
        with self.lock:
            r = self.db.execute(f"select {','.join(self.LINK_COLS)} from links where ub_id=? and service=?",
                                (ub_id, service)).fetchone()
        return self._row(r) if r else dict(self.DEFAULT)

    def save_link(self, ub_id, service, **fields):
        cur = self.link(ub_id, service); cur.update(fields)
        with self.lock:
            self.db.execute(
                f"insert or replace into links(ub_id,service,{','.join(self.LINK_COLS)}) "
                f"values(?,?,{','.join('?' * len(self.LINK_COLS))})",
                (ub_id, service, *[cur[c] for c in self.LINK_COLS])); self.db.commit()

    def links_by_book(self):
        with self.lock:
            rows = self.db.execute(f"select ub_id,service,{','.join(self.LINK_COLS)} from links").fetchall()
        out = {}
        for r in rows:
            out.setdefault(r[0], {})[r[1]] = self._row(r[2:])
        return out

    # ---- books (local copy of what Hardcover last told us)
    def upsert_book(self, b, now):
        with self.lock:
            self.db.execute("insert or replace into books values(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                b.ub_id, b.title, json.dumps(b.authors), b.status, b.pages, b.total_pages, b.rating,
                b.isbn13, b.isbn10, b.book_id, b.edition_id, b.finished_on, now)); self.db.commit()

    @staticmethod
    def _book(r):
        return Book(ub_id=r[0], title=r[1] or "", authors=json.loads(r[2] or "[]"), status=r[3], pages=r[4],
                    total_pages=r[5], rating=r[6], isbn13=r[7], isbn10=r[8], book_id=r[9], edition_id=r[10],
                    finished_on=r[11])

    def get_book(self, ub_id):
        with self.lock:
            r = self.db.execute("select * from books where ub_id=?", (ub_id,)).fetchone()
        return self._book(r) if r else None

    def books(self, q=None, status=None):
        sql, args = "select * from books where 1=1", []
        if q:
            sql += " and (lower(title) like ? or lower(authors) like ?)"; args += [f"%{q.lower()}%"] * 2
        if status:
            sql += " and status=?"; args.append(status)
        with self.lock:
            return [self._book(r) for r in self.db.execute(sql, args).fetchall()]

    def book_count(self):
        with self.lock:
            return self.db.execute("select count(*) from books").fetchone()[0]
