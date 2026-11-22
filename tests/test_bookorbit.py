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


ALL = [test_kind_and_edition_rules, test_plan_book, test_engine_scan_apply_and_ebook_switch]

if __name__ == "__main__":
    for t in ALL:
        try:
            t()
        except Exception:
            print("FAILED:", t.__name__); raise
    print("bookorbit tests passed")
