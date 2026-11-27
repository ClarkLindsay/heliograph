import logging, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from heliograph import bookorbit as bo, smartmatch


def ed(i, fmt, lang="English", isbn=None, users=0, label=None):
    return {"id": i, "title": "x", "isbn13": isbn, "isbn10": None, "reading_format_id": fmt, "format": bo.FORMAT[fmt],
            "edition_format": label, "users": users, "pages": None, "language": lang}


class NoLib:
    """A Hardcover account with an empty library."""
    def __init__(self, books=()): self.lib = list(books)
    def fetch_library(self): return list(self.lib)


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
        eng = Engine(cfg, [], State(os.path.join(tmp, "s.db")), logging.getLogger("t"), hardcover=NoLib())
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
            assert [(i["title"], i["fix"]) for i in j["items"]] == [("Dune", "link"), ("The Hobbit", "right")], j["message"]
            assert j["items"][0]["pick"] and j["items"][1]["result"] == "✓ already right"            # the Hobbit is listed as done
            eng.apply_bookorbit([j["items"][0]["key"]])
            for _ in range(100):
                if not eng.bo_job["running"]: break
                time.sleep(0.05)
            assert client.updates == [(1, {"hardcoverId": "dune", "hardcoverEditionId": "14"})]
            # a second scan skips both: nothing changed
            eng.start_bookorbit_scan()
            while eng.bo_job["running"]: time.sleep(0.05)
            assert eng.bo_job["skipped"] == 2 and {i["fix"] for i in eng.bo_job["items"]} == {"right"}, (eng.bo_job["skipped"], eng.bo_job["items"], eng.bo_job["message"])
            assert eng.bo_job["message"].startswith("All 2 books have the right")
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
        eng = Engine(cfg, [], State(os.path.join(tmp, "s.db")), logging.getLogger("t"), hardcover=NoLib())
        eng.bookorbit = lambda: Client()
        orig = bo.HardcoverLookup
        bo.HardcoverLookup = lambda hc: Tired()
        try:
            eng.start_bookorbit_scan()
            while eng.bo_job["running"]: time.sleep(0.05)
            j = eng.bo_job
            assert j["message"].startswith("Paused after 1 of 2 books") and "resets in 3h 2m" in j["message"], j["message"]
            assert [i["fix"] for i in j["items"]] == ["right"]                # Dune is done; the Hobbit wasn't marked as an error
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
        eng = Engine(cfg, [], State(db), logging.getLogger("t"), hardcover=NoLib())
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
            eng2 = Engine(cfg, [], State(db), logging.getLogger("t"), hardcover=NoLib())
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
        def fetch_library(self): return []
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
        eng = Engine(Config(os.path.join(tmp, "c.json")), [], State(os.path.join(tmp, "s.db")), logging.getLogger("t"), hardcover=NoLib())
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


class OwnedHC(NoLib):
    """A Hardcover account with an Owned list."""
    def __init__(self, books=(), owned=()):
        super().__init__(books); self.owned, self.adds = set(owned), []
    def owned_list(self): return 1, {i: 100 + i for i in self.owned}
    def add_owned(self, book_id, edition_id=None):
        if book_id in self.owned: return False
        self.owned.add(book_id); self.adds.append(book_id); return True


def owned_setup(tmp, books, owned=()):
    from heliograph.config import Config
    from heliograph.engine import Engine
    from heliograph.providers.base import Provider
    from heliograph.state import State

    class Owns(Provider):
        supports_owned = True
        def __init__(self, cfg, log, key, label):
            self.key, self.label = key, label
            super().__init__(cfg, log)
            self.calls, self.owned, self.fail = [], set(), None
        def set_owned(self, rid, b, owned=True):
            self.calls.append(rid if owned else "-" + rid)
            if self.fail: raise self.fail
            if (rid in self.owned) == owned: return False
            (self.owned.add if owned else self.owned.discard)(rid); return True

    cfg = Config(os.path.join(tmp, "c.json"))
    cfg["bookorbit"].update(url="http://bo", api_key="k")
    for k in ("goodreads", "storygraph", "fable"):
        cfg[k]["enabled"] = True
    log = logging.getLogger("t")
    gr, sg = Owns(cfg, log, "goodreads", "Goodreads"), Owns(cfg, log, "storygraph", "StoryGraph")
    plain = Owns(cfg, log, "fable", "Fable"); plain.supports_owned = False
    hc = OwnedHC(books, owned)
    eng = Engine(cfg, [gr, sg, plain], State(os.path.join(tmp, "s.db")), log, hardcover=hc)
    for x in books:
        eng.state.upsert_book(x, time.time())
    return eng, hc, gr, sg, plain


def wait(eng, attr):
    for _ in range(200):
        t = getattr(eng, attr, None)
        if not (t.get("running") if isinstance(t, dict) else (t and t.is_alive())):
            return
        time.sleep(0.02)


def test_mark_bookorbit_books_as_owned():
    from heliograph.hardcover import Book
    from heliograph.providers.base import AuthError

    class Client:
        def books(self): return [book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="dune"),
                                 book(2, "Emma", ["Jane Austen"], ["m4b"], hardcoverId="42"),
                                 book(3, "Not on Hardcover", ["X"], ["epub"])]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        dune = Book(10, "Dune", status="read", slug="dune", book_id=7, edition_id=70)
        emma = Book(12, "Emma", status="want", book_id=42)
        eng, hc, gr, sg, plain = owned_setup(tmp, [dune, emma], owned=[42])        # Emma's already on the Owned list
        eng.bookorbit = lambda: Client()
        eng.state.save_link(10, "goodreads", remote_id="G10"); eng.state.save_link(10, "storygraph", remote_id="S10")
        eng.state.save_link(10, "fable", remote_id="F10")
        eng.state.save_link(12, "goodreads", remote_id="G12")      # Emma: not linked on StoryGraph yet
        sg.owned.add("S10")                                       # already owned there
        eng.start_bookorbit_owned(); wait(eng, "own_job")
        j = eng.own_job
        assert hc.adds == [7], hc.adds                            # Hardcover's Owned list first
        assert sorted(gr.calls) == ["G10", "G12"] and sg.calls == ["S10"] and plain.calls == [], (gr.calls, sg.calls)
        assert (j["marked"], j["already"], j["not_linked"], j["not_in_hardcover"], j["failed"]) == (3, 2, 1, 1, 0), j
        # a second run asks nobody again
        gr.calls.clear(); sg.calls.clear()
        eng.start_bookorbit_owned(); wait(eng, "own_job")
        assert gr.calls == [] and sg.calls == [] and eng.own_job["already"] == 5
        # a dead session stops that service, the others carry on
        eng.state.set("owned_marked", "[]"); gr.owned.clear(); gr.fail = AuthError("cookies expired")
        eng.start_bookorbit_owned(); wait(eng, "own_job")
        assert len(gr.calls) == 1 and eng.own_job["failed"] == 1 and sg.calls == ["S10"], (gr.calls, eng.own_job)
        # the setting: off -> nothing starts on its own
        eng.state.set("owned_checked_at", "0"); eng.bookorbit_owned_auto()
        assert not eng.own_job["running"]


def test_hardcover_owned_list_syncs_to_services():
    from heliograph.hardcover import Book
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        dune = Book(10, "Dune", status="read", book_id=7)
        emma = Book(12, "Emma", status="want", book_id=42)
        eng, hc, gr, sg, plain = owned_setup(tmp, [dune, emma], owned=[7])
        for ub, k, rid in ((10, "goodreads", "G10"), (10, "storygraph", "S10"), (10, "fable", "F10"),
                           (12, "goodreads", "G12"), (12, "storygraph", "S12")):
            eng.state.save_link(ub, k, remote_id=rid)
        gr.owned.add("G12")                                       # you marked Emma owned on Goodreads yourself
        eng.sync_owned(); wait(eng, "_owned_thread")
        assert gr.calls == ["G10"] and sg.calls == ["S10"] and plain.calls == []
        gr.calls.clear(); sg.calls.clear()
        eng.sync_owned(); wait(eng, "_owned_thread")
        assert gr.calls == [] and sg.calls == []                   # remembered: nothing asked again
        hc.owned = {42}                                           # Dune off the list, Emma on
        eng.sync_owned(); wait(eng, "_owned_thread")
        assert sorted(gr.calls) == ["-G10", "G12"] and sorted(sg.calls) == ["-S10", "S12"], (gr.calls, sg.calls)
        assert "G12" in gr.owned and "G10" not in gr.owned
        hc.owned = set(); gr.calls.clear(); sg.calls.clear()
        eng.sync_owned(); wait(eng, "_owned_thread")
        # Emma comes off too: unmarked where Heliograph marked it (StoryGraph); on Goodreads you'd marked it
        # yourself before it was ever on the list, so it's left as you set it
        assert sorted(sg.calls) == ["-S12"] and gr.calls == [] and "G12" in gr.owned, (gr.calls, sg.calls)
        eng.config["common"]["sync_owned"] = False
        hc.owned = {7}; gr.calls.clear()
        eng.sync_owned(); wait(eng, "_owned_thread")
        assert gr.calls == []                                     # the setting off: nothing


def test_ratings_and_reviews_from_hardcover():
    from heliograph.config import Config
    from heliograph.engine import Engine
    from heliograph.hardcover import Book
    from heliograph.state import State

    class Client:
        def __init__(self):
            self.lib = [book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="dune", rating=None,
                             readStatus={"status": "read"}),
                        book(2, "The Hobbit", ["J.R.R. Tolkien"], ["epub"], hardcoverId="the-hobbit", rating=3,
                             readStatus={"status": "reading"}),
                        book(3, "Emma", ["Jane Austen"], ["epub"], hardcoverId="42", rating=None, readStatus=None),
                        book(4, "Locked", ["X"], ["epub"], hardcoverId="locked", rating=2, lockedFields=["rating"])]
            self.notes, self.calls = {3: "my own private thoughts"}, []
        def books(self): return [dict(b) for b in self.lib]
        def book(self, i): return {"id": i, "personalNote": self.notes.get(i)}
        def update(self, i, f):
            self.calls.append(("rating", i, f["rating"])); next(b for b in self.lib if b["id"] == i).update(f)
        def set_note(self, i, t): self.calls.append(("note", i, t)); self.notes[i] = t

    class HC:
        def __init__(self, books): self.lib = books
        def fetch_library(self): return list(self.lib)

    dune = Book(10, "Dune", status="read", rating=4.0, slug="dune", review="Spice.")
    hobbit = Book(11, "The Hobbit", status="read", rating=5.0, slug="the-hobbit")
    emma = Book(12, "Emma", status="read", rating=3.5, book_id=42, review="Witty.")
    locked = Book(13, "Locked", status="read", rating=4.0, slug="locked")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = Config(os.path.join(tmp, "c.json"))
        cfg["bookorbit"].update(url="http://bo", api_key="k")
        eng = Engine(cfg, [], State(os.path.join(tmp, "s.db")), logging.getLogger("t"), hardcover=HC([dune, hobbit, emma, locked]))
        client = Client()
        eng.bookorbit = lambda: client
        eng.bookorbit_from_hardcover([])                      # the first run goes through the whole library
        assert client.calls == [("rating", 1, 4), ("note", 1, "Spice.")], client.calls
        # the Hobbit: BookOrbit says reading, Hardcover read -> not touched (BookOrbit would sync its status back)
        # Emma: 3.5 stars -> BookOrbit's (blank) rating stays blank; its own note isn't replaced
        # Locked: you locked the rating in BookOrbit
        assert client.notes[3] == "my own private thoughts"
        client.calls.clear()
        eng.bookorbit_from_hardcover([dune, hobbit, emma, locked])
        assert client.calls == []                             # nothing changed: nothing written
        client.lib[1]["readStatus"] = {"status": "read"}      # you fix the Hobbit's status in BookOrbit
        dune.rating, dune.review = 5.0, "Spice! Edited."
        eng.bookorbit_from_hardcover([dune, hobbit])
        assert client.calls == [("rating", 1, 5), ("note", 1, "Spice! Edited."), ("rating", 2, 5)], client.calls
        client.calls.clear()
        client.lib[0]["readStatus"] = {"status": "read", "finishedAt": "2024-04-18T00:00:00.000Z"}
        dune.rating, dune.finished_on = 3.0, "2024-04-17"     # BookOrbit's finish date differs: not touched
        eng.bookorbit_from_hardcover([dune])
        assert client.calls == []
        dune.finished_on = "2024-04-18"                       # dates agree: copied
        eng.bookorbit_from_hardcover([dune])
        assert client.calls == [("rating", 1, 3)]
        client.calls.clear()
        dune.rating = 4.5                                     # a half star: cleared, never rounded
        eng.bookorbit_from_hardcover([dune])
        assert client.calls == [("rating", 1, None)]
        client.calls.clear()
        client.calls.clear()
        dune.rating, dune.finished_on = 2.0, "2020-01-01"     # dates differ again: held back...
        eng.bookorbit_from_hardcover([dune])
        assert client.calls == []
        client.hardcover_settings = lambda: {"enabled": True, "autoSyncOnRatingChange": False}
        eng.bookorbit_from_hardcover([dune])                  # ...until BookOrbit no longer syncs on a rating change
        assert client.calls == [("rating", 1, 2)], client.calls
        client.calls.clear()
        cfg["bookorbit"]["from_hardcover"] = False
        dune.rating = 1.0
        assert eng.bookorbit_from_hardcover([dune]) == 0 and client.calls == []


def test_your_library_record_of_a_book_wins():
    look = Look()
    mine = {"id": 3, "slug": "dune-1965", "title": "Dune", "authors": ["Frank Herbert"]}
    look.books["dune-1965"] = mine
    lib = bo.Library({"dune-1965": 31, "3": 31}, owned=[mine])
    # linked to another record of Dune: your library's record wins, with its edition
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="dune", hardcoverEditionId="12"), look,
                     library=lib)
    assert p["changes"] == {"hardcoverId": "dune-1965", "hardcoverEditionId": "31"}, p
    assert "your Hardcover library has this book" in p["why"][0]
    # not linked yet: found in your library, no search needed
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"]), look, library=lib)
    assert p["changes"]["hardcoverId"] == "dune-1965" and "in your Hardcover library" in p["why"][0]
    # already on your library's record: nothing to do
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="dune-1965", hardcoverEditionId="31"),
                     look, library=lib)
    assert p["fix"] == "ok" and not p["changes"]
    # two records of it in your library: ambiguous, so the usual rules decide
    two = bo.Library({"dune-1965": 31}, owned=[mine, dict(mine, id=4, slug="dune-again")])
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="dune", hardcoverEditionId="12"), look,
                     library=two)
    assert p["fix"] == "ok"
    # your library has it only a subtitle apart: not linked either way, it's yours to confirm
    sub = {"id": 5, "slug": "dune-sub", "title": "Dune: The Novel", "authors": ["Frank Herbert"]}
    look.books["dune-sub"] = sub
    near = bo.Library({"dune-sub": 51}, owned=[sub])
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="dune", hardcoverEditionId="12"), look,
                     library=near)
    assert not p["changes"].get("hardcoverId") and p["suggestion"]["slug"] == "dune-sub", p
    assert "similar" in p["problem"] and "in your Hardcover library" in p["suggestion"]["reasons"]
    # ... and a book already on that record of yours stays on it
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="dune-sub", hardcoverEditionId="51"),
                     look, library=near)
    assert p["fix"] == "ok" and not p["changes"], p
    # a volume of a graphic adaptation isn't the book
    gn = bo.Library({}, owned=[{"id": 9, "slug": "dune-vol-1", "title": "Dune, Vol. 1", "authors": ["Frank Herbert"]}])
    assert bo.owned_match(gn, "Dune", ["Frank Herbert"], smartmatch.Matcher()) is None
    # nor is another book that shares words with it
    for other in ("The Way of Kings", "Ender's Game", "Earth Unaware"):
        lb = bo.Library({}, owned=[{"id": 8, "slug": "x", "title": other, "authors": ["A B"]}])
        title = {"The Way of Kings": "The Way of Kings Prime", "Ender's Game": "Ender's Shadow",
                 "Earth Unaware": "Earth Afire"}[other]
        assert bo.owned_match(lb, title, ["A B"], smartmatch.Matcher(), near=True) == (None, False), other


def test_books_you_have_read_keep_your_hardcover_edition():
    from heliograph.config import Config
    from heliograph.engine import Engine
    from heliograph.hardcover import Book
    from heliograph.state import State
    look = Look()
    # planner: Dune is in your Hardcover library with the print edition 11; you own the ebook in BookOrbit
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"]), look, library={"dune": 11, "1": 11})
    assert p["changes"] == {"hardcoverId": "dune", "hardcoverEditionId": "11"} and "you read" in p["why"][0]
    p = bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="dune", hardcoverEditionId="11"), look,
                     library={"dune": 11})
    assert p["fix"] == "ok" and not p["changes"]
    assert bo.plan_book(book(1, "Dune", ["Frank Herbert"], ["epub"]), look)["changes"]["hardcoverEditionId"] == "12"   # not read: ebook

    class Client:
        def __init__(self): self.lib, self.updates = [book(1, "Dune", ["Frank Herbert"], ["epub"], hardcoverId="dune",
                                                           hardcoverEditionId="12")], []
        def books(self): return [dict(b) for b in self.lib]
        def update(self, i, f): self.updates.append((i, dict(f))); self.lib[0].update(f)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        hc = NoLib()
        eng = Engine(Config(os.path.join(tmp, "c.json")), [], State(os.path.join(tmp, "s.db")), logging.getLogger("t"), hardcover=hc)
        client = Client()
        eng.bookorbit = lambda: client
        orig = bo.HardcoverLookup
        bo.HardcoverLookup = lambda h: Look()
        try:
            eng.start_bookorbit_scan()
            while eng.bo_job["running"]: time.sleep(0.05)
            assert eng.bo_job["items"][0]["fix"] == "right"                 # not in your library: the ebook is right
            hc.lib = [Book(9, "Dune", status="read", book_id=1, edition_id=11, slug="dune")]   # ...then you read it in print
            eng.start_bookorbit_scan()                                       # checked before, but the edition differs now
            while eng.bo_job["running"]: time.sleep(0.05)
            item = eng.bo_job["items"][0]
            assert item["fix"] == "edition" and item["changes"] == {"hardcoverEditionId": "11"}, item
        finally:
            bo.HardcoverLookup = orig


ALL = [test_hardcover_owned_list_syncs_to_services, test_mark_bookorbit_books_as_owned, test_your_library_record_of_a_book_wins, test_books_you_have_read_keep_your_hardcover_edition, test_ratings_and_reviews_from_hardcover, test_set_aside_a_book_hardcover_doesnt_have, test_kind_and_edition_rules, test_plan_book, test_engine_scan_apply_and_ebook_switch,
       test_scan_pauses_at_hardcovers_daily_limit, test_parse_ref, test_find_on_hardcover_and_paste,
       test_apply_uses_a_ticked_suggestion_and_hardcover_url]

if __name__ == "__main__":
    for t in ALL:
        try:
            t()
        except Exception:
            print("FAILED:", t.__name__); raise
    print("bookorbit tests passed")
