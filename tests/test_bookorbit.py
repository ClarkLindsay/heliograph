import logging, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from heliograph import bookorbit as bo


def ed(i, fmt, lang="English", isbn=None, users=0, label=None):
    return {"id": i, "title": "x", "isbn13": isbn, "isbn10": None, "reading_format_id": fmt, "format": bo.FORMAT[fmt],
            "edition_format": label, "users": users, "pages": None, "language": lang}


class Look:
    """Hardcover, faked."""
    def __init__(self):
        self.books = {"dune": {"id": 1, "slug": "dune", "title": "Dune", "authors": ["Frank Herbert"]},
                      "the-hobbit": {"id": 2, "slug": "the-hobbit", "title": "The Hobbit", "authors": ["J.R.R. Tolkien"]}}
        self.eds = {1: [ed(11, 1, users=900), ed(12, 4, users=500), ed(13, 2, users=700, label="Audio Cassette"),
                        ed(14, 2, users=400, label="Audible Audio"), ed(15, 4, "German", users=999)],
                    2: [ed(21, 1, users=50), ed(22, 1, "Spanish", users=999)]}
    def book(self, ref): return self.books.get(str(ref)) or next((b for b in self.books.values() if str(b["id"]) == str(ref)), None)
    def editions(self, bid): return self.eds.get(bid, [])
    def by_isbn(self, isbn): return self.books["dune"] if isbn == "9780441013593" else None
    def search(self, title, authors): return [dict(b, users=1) for b in self.books.values() if title.lower() in b["title"].lower()]
    def edition(self, eid):
        for bid, eds in self.eds.items():
            for e in eds:
                if str(e["id"]) == str(eid):
                    return e, next(b for b in self.books.values() if b["id"] == bid)
        return None, None


def book(i, title, authors, files, **kw):
    b = {"id": i, "title": title, "authors": authors, "files": [{"format": f, "role": "primary"} for f in files],
         "hardcoverId": None, "hardcoverEditionId": None, "language": "eng", "isbn13": None, "lockedFields": []}
    b.update(kw)
    return b


def test_kind_and_edition_rules():
    assert bo.kind(book(1, "x", [], ["epub", "m4b"])) == "ebook"          # an ebook wins over audio
    assert bo.kind(book(1, "x", [], ["m4b"])) == "audio" and bo.kind(book(1, "x", [], [])) == "print"
    eds = Look().eds[1]
    assert bo.pick_edition(eds, "ebook")["id"] == 12                    # English ebook, not the German one
    assert bo.pick_edition(eds, "audio")["id"] == 14                    # digital audiobook before a cassette
    assert bo.pick_edition(eds, "print")["id"] == 11
    assert bo.pick_edition(Look().eds[2], "ebook")["id"] == 21          # no ebook: print, never Spanish
    assert bo.pick_edition([ed(9, 4, "Spanish")], "ebook") is None
    assert bo.pick_edition([ed(9, 4, None)], "ebook")["id"] == 9        # no language recorded: allowed after English
    assert bo.pick_edition(eds, "ebook", isbns=["777"])["id"] == 12
    assert bo._format_id({"edition_format": "Kindle Edition", "reading_format_id": 1}) == 4
    assert bo._format_id({"edition_format": "Audible Audio", "reading_format_id": 1}) == 2


def test_plan_book():
    look = Look()
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"], language="English"), look)    # not linked yet
    assert p["fix"] == "link" and p["changes"] == {"hardcoverId": "dune", "hardcoverEditionId": "12", "language": "eng"}
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["m4b"], hardcoverId="dune", hardcoverEditionId="12"), look)
    assert p["fix"] == "edition" and p["changes"] == {"hardcoverEditionId": "14"}                # audio only: audiobook
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="the-hobbit", hardcoverEditionId="21"), look)
    assert p["fix"] == "relink" and p["changes"]["hardcoverId"] == "dune" and "isn't this book" in p["why"][0]
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="dune", hardcoverEditionId="12"), look)
    assert p["fix"] == "ok" and not p["changes"] and not p["problem"]
    p = bo.plan_book(book(1, "Some Unknown Book", ["Nobody"], ["epub"]), look)
    assert p["problem"] == "not found on Hardcover" and not p["changes"]
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"], language="fre"), look)
    assert "not English" in p["problem"] and "language" not in p["changes"]
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"], lockedFields=["hardcoverEditionId"]), look)
    assert "hardcoverEditionId" not in p["changes"]                     # a field you locked in BookOrbit is left alone


def test_engine_scan_apply_and_ebook_switch():
    from heliograph.config import Config
    from heliograph.engine import Engine
    from heliograph.state import State

    class Client:
        def __init__(self, books): self.lib, self.updates = books, []
        def books(self): return self.lib
        def update(self, i, fields):
            self.updates.append((i, dict(fields)))
            b = next(b for b in self.lib if b["id"] == i); b.update(fields)
        def me(self): return {"username": "me"}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = Config(os.path.join(tmp, "c.json"))
        cfg["bookorbit"].update(url="http://bo", api_key="k", auto_ebook=True)
        eng = Engine(cfg, [], State(os.path.join(tmp, "s.db")), logging.getLogger("t"), hardcover=object())
        client = Client([book(1, "Dune", ["Frank Herbert"], ["m4b"]), book(2, "The Hobbit", ["J.R.R. Tolkien"], ["epub"],
                                                                      hardcoverId="the-hobbit", hardcoverEditionId="21")])
        eng.bookorbit = lambda: client
        orig = bo.HardcoverLookup
        bo.HardcoverLookup = lambda hc: Look()
        try:
            eng.start_bookorbit_scan()
            for _ in range(100):
                if not eng.bo_job["running"]: break
                time.sleep(0.05)
            j = eng.bo_job
            assert [i["title"] for i in j["items"]] == ["Dune"] and j["items"][0]["pick"], j["message"]   # the Hobbit is right
            eng.apply_bookorbit([j["items"][0]["key"]])
            for _ in range(100):
                if not eng.bo_job["running"]: break
                time.sleep(0.05)
            assert client.updates == [(1, {"hardcoverId": "dune", "hardcoverEditionId": "14"})]
            # a second scan skips both: nothing changed
            eng.start_bookorbit_scan()
            while eng.bo_job["running"]: time.sleep(0.05)
            assert eng.bo_job["skipped"] == 2 and not eng.bo_job["items"]
            # you add an ebook to Dune: the next check moves it to the ebook edition on its own
            client.lib[0]["files"].append({"format": "epub", "role": "primary"})
            assert eng.bookorbit_ebooks(force=True) == 1
            assert client.updates[-1] == (1, {"hardcoverEditionId": "12"})
            assert eng.bookorbit_ebooks(force=True) == 0                  # once is enough
            cfg["bookorbit"]["auto_ebook"] = False
            client.lib[1]["files"] = [{"format": "m4b", "role": "primary"}]
            assert eng.bookorbit_ebooks(force=True) == 0
        finally:
            bo.HardcoverLookup = orig


def test_scan_pauses_at_hardcovers_daily_limit():
    from heliograph.config import Config
    from heliograph.engine import Engine
    from heliograph.hardcover import HardcoverDailyLimit
    from heliograph.state import State

    class Tired(Look):
        def editions(self, bid):
            if bid == 2: raise HardcoverDailyLimit(3 * 3600 + 120, "5000")
            return super().editions(bid)

    class Client:
        def books(self): return [book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="dune", hardcoverEditionId="12"),
                                 book(2, "The Hobbit", ["J.R.R. Tolkien"], ["epub"], hardcoverId="the-hobbit")]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = Config(os.path.join(tmp, "c.json"))
        eng = Engine(cfg, [], State(os.path.join(tmp, "s.db")), logging.getLogger("t"), hardcover=object())
        eng.bookorbit = lambda: Client()
        orig = bo.HardcoverLookup
        bo.HardcoverLookup = lambda hc: Tired()
        try:
            eng.start_bookorbit_scan()
            while eng.bo_job["running"]: time.sleep(0.05)
            j = eng.bo_job
            assert j["message"].startswith("Paused after 1 of 2 books") and "resets in 3h 2m" in j["message"], j["message"]
            assert not j["items"]                                           # the Hobbit wasn't marked as an error
            assert "1" in eng._bo_seen() and "2" not in eng._bo_seen()       # Dune is kept: the next scan skips it
        finally:
            bo.HardcoverLookup = orig


def test_parse_ref():
    assert bo.parse_ref("https://hardcover.app/books/dune") == ("book", "dune")
    assert bo.parse_ref("https://hardcover.app/books/dune/editions/30530618?x=1") == ("edition", "30530618")
    assert bo.parse_ref("edition:12") == ("edition", "12") and bo.parse_ref("Edition 12") == ("edition", "12")
    assert bo.parse_ref(" The-Hobbit ") == ("book", "the-hobbit") and bo.parse_ref("427") == ("book", "427")
    try:
        bo.parse_ref("not a link!"); assert False
    except ValueError:
        pass


def test_find_on_hardcover_and_paste():
    from heliograph.config import Config
    from heliograph.engine import Engine
    from heliograph.state import State

    class Client:
        def __init__(self): self.updates = []
        def books(self): return [book(1, "Dune", ["Frank Herbert"], ["m4b"], language="English"),
                                 book(2, "Some Unknown Book", ["Nobody"], ["epub"])]
        def update(self, i, fields): self.updates.append((i, dict(fields)))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg, db = Config(os.path.join(tmp, "c.json")), os.path.join(tmp, "s.db")
        eng = Engine(cfg, [], State(db), logging.getLogger("t"), hardcover=object())
        client = Client()
        eng.bookorbit = lambda: client
        orig = bo.HardcoverLookup
        bo.HardcoverLookup = lambda hc: Look()
        try:
            eng.start_bookorbit_scan()
            while eng.bo_job["running"]: time.sleep(0.05)
            assert [i["title"] for i in eng.bo_job["items"]] == ["Dune", "Some Unknown Book"]
            # searching with your own words; the verdict says the Hobbit isn't this book
            r = eng.bookorbit_search("2", "hobbit")["results"]
            assert [c["title"] for c in r] == ["The Hobbit"] and r[0]["url"].endswith("/the-hobbit")
            p = eng.bookorbit_link("2", "the-hobbit")                     # you link it anyway: your call
            assert client.updates[-1] == (2, {"hardcoverId": "the-hobbit", "hardcoverEditionId": "21"}) and p["result"] == "✓ updated"
            # a pasted edition URL is used exactly, even an audiobook, and the language fix goes with it
            eng.bookorbit_link("1", "https://hardcover.app/books/dune/editions/13")
            assert client.updates[-1] == (1, {"hardcoverId": "dune", "hardcoverEditionId": "13", "language": "eng"})
            for bad in ("nope-not-a-book", "edition:999"):
                try:
                    eng.bookorbit_link("1", bad); assert False
                except ValueError:
                    pass
            # the list survives a restart, and both books are skipped by the next scan
            eng2 = Engine(cfg, [], State(db), logging.getLogger("t"), hardcover=object())
            assert [i["result"] for i in eng2.bo_job["items"]] == ["✓ updated", "✓ updated"] and not eng2.bo_job["running"]
            assert set(eng2._bo_seen()) == {"1", "2"}
        finally:
            bo.HardcoverLookup = orig


def test_apply_uses_a_ticked_suggestion_and_hardcover_url():
    from heliograph.config import Config
    from heliograph.engine import Engine
    from heliograph.hardcover import Book
    from heliograph.state import State

    class Client:
        def __init__(self): self.updates = []
        def books(self): return [book(2, "Some Unknown Book", ["Nobody"], ["epub"])]
        def update(self, i, fields): self.updates.append((i, dict(fields)))

    class HC:
        calls = 0
        def _q(self, q, v):
            HC.calls += 1
            return {"books": [{"slug": "dune"}] if v["b"] == 1 else []}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        st = State(os.path.join(tmp, "s.db"))
        eng = Engine(Config(os.path.join(tmp, "c.json")), [], st, logging.getLogger("t"), hardcover=HC())
        client = Client()
        eng.bookorbit = lambda: client
        orig = bo.HardcoverLookup
        bo.HardcoverLookup = lambda hc: Look()
        try:
            eng.start_bookorbit_scan()
            while eng.bo_job["running"]: time.sleep(0.05)
            item = eng.bo_job["items"][0]
            item["suggestion"] = {"id": 2, "slug": "the-hobbit", "title": "The Hobbit", "authors": ["J.R.R. Tolkien"],
                                  "confidence": 0.6, "reasons": []}
            eng.apply_bookorbit(["2"])                                   # ticked with only a suggestion: it's used
            while eng.bo_job["running"]: time.sleep(0.05)
            assert client.updates == [(2, {"hardcoverId": "the-hobbit", "hardcoverEditionId": "21"})], client.updates
            assert item["result"] == "✓ updated" and eng.bo_job["message"].startswith("Done: 1 book(s)")
            assert eng._bo_seen().get("2")                               # and remembered, so the next scan skips it
        finally:
            bo.HardcoverLookup = orig
        # the Books tab's Hardcover link: the edition you have, looked up once
        st.upsert_book(Book(ub_id=7, title="Dune", authors=["Frank Herbert"], book_id=1, edition_id=12), time.time())
        assert eng.hardcover_url(7) == "https://hardcover.app/books/dune/editions/12"
        assert eng.hardcover_url(7) and HC.calls == 1
        st.upsert_book(Book(ub_id=8, title="Lost Book", authors=[], book_id=99), time.time())
        assert eng.hardcover_url(8) == "https://hardcover.app/search?q=Lost%20Book"


def test_set_aside_a_book_hardcover_doesnt_have():
    from heliograph.config import Config
    from heliograph.engine import Engine
    from heliograph.state import State

    class Client:
        def books(self): return [book(2, "Some Unknown Book", ["Nobody"], ["epub"])]

    class Counting(Look):
        searches = 0
        def search(self, title, authors):
            Counting.searches += 1
            return super().search(title, authors)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        eng = Engine(Config(os.path.join(tmp, "c.json")), [], State(os.path.join(tmp, "s.db")), logging.getLogger("t"), hardcover=object())
        eng.bookorbit = lambda: Client()
        orig = bo.HardcoverLookup
        bo.HardcoverLookup = lambda hc: Counting()
        scan = lambda full=False: (eng.start_bookorbit_scan(full), [time.sleep(0.05) for _ in iter(lambda: eng.bo_job["running"], False)])
        try:
            scan()
            assert eng.bo_job["items"][0]["problem"] == "not found on Hardcover" and Counting.searches == 1
            p = eng.bookorbit_set_aside("2")
            assert p["fix"] == "aside" and not p["problem"]
            scan(full=True)                                              # even a full recheck leaves it alone
            assert Counting.searches == 1 and eng.bo_job["items"][0]["fix"] == "aside"
            assert "1 set aside" in eng.bo_job["message"], eng.bo_job["message"]
            eng.bookorbit_set_aside("2", False)                          # back in: the next scan looks again
            scan()
            assert Counting.searches == 2 and eng.bo_job["items"][0]["fix"] != "aside"
        finally:
            bo.HardcoverLookup = orig


ALL = [test_set_aside_a_book_hardcover_doesnt_have, test_kind_and_edition_rules, test_plan_book, test_engine_scan_apply_and_ebook_switch,
       test_scan_pauses_at_hardcovers_daily_limit, test_parse_ref, test_find_on_hardcover_and_paste,
       test_apply_uses_a_ticked_suggestion_and_hardcover_url]

if __name__ == "__main__":
    for t in ALL:
        try:
            t()
        except Exception:
            print("FAILED:", t.__name__); raise
    print("bookorbit tests passed")
