import logging, os, sys, tempfile, time
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from heliograph import dates
from heliograph.hardcover import HardcoverClient, HardcoverError, book_from_ub
from heliograph.providers.goodreads import parse_read_list


class Resp:
    def __init__(self, code, j): self.status_code, self._j = code, j
    def json(self): return self._j
    def raise_for_status(self): pass


def r(i, s=None, f=None): return {"id": i, "started": s, "finished": f}
def g(s=None, f=None): return {"started": s, "finished": f}


def test_plan_fills_missing_start_dates():
    ch = dates.plan([r(1, None, "2022-09-15")], [g("2022-08-01", "2022-09-15")])
    assert ch == [{"op": "start", "read_id": 1, "started": "2022-08-01", "finished": "2022-09-15", "was": (None, "2022-09-15"),
                   "pick": True, "why": "start date missing"}]
    assert dates.plan([r(1, "2022-08-01", "2022-09-15")], [g("2022-08-01", "2022-09-15")]) == []      # already right
    ch, = dates.plan([r(1, "2022-07-01", "2022-09-15")], [g("2022-08-01", "2022-09-15")])
    assert ch["op"] == "start" and not ch["pick"] and "replaces" in ch["why"]                         # listed, not picked
    ch, = dates.plan([r(1, None, "2022-09-15")], [g("2022-09-15", "2022-09-15")])
    assert not ch["pick"] and "same day" in ch["why"]                                                  # likely a placeholder
    ch, = dates.plan([r(1, None, None)], [g("2026-10-01", None)])                                      # still reading
    assert (ch["read_id"], ch["started"], ch["finished"], ch["pick"]) == (1, "2026-10-01", None, True)


def test_plan_rereads_mismatches_and_bad_dates():
    hc = [r(1, None, "2020-05-01"), r(2, None, "2023-03-03")]
    ch = dates.plan(hc, [g("2023-02-01", "2023-03-03"), g("2020-04-01", "2020-05-01")])    # each read pairs by finish date
    assert [(c["read_id"], c["started"]) for c in ch] == [(2, "2023-02-01"), (1, "2020-04-01")]
    ch, = dates.plan([r(1, None, "2020-05-01")], [g("2020-04-01", "2020-05-02")])           # one each, finishes differ
    assert (ch["op"], ch["read_id"], ch["started"], ch["finished"], ch["pick"]) == ("dates", 1, "2020-04-01", "2020-05-02", False)
    ch = dates.plan([], [g("2019-01-01", "2019-02-01")])                                     # Hardcover has no reads
    assert ch[0]["op"] == "add" and ch[0]["read_id"] is None and ch[0]["pick"]
    ch = dates.plan([r(1, None, "2022-09-15")], [g(None, "2022-09-15"), g("2026-11-21", "2026-11-21")])
    assert [(c["op"], c["pick"]) for c in ch] == [("add", False)]                            # an extra read: offered, not picked
    assert dates.plan([r(1, None, "2020-05-01")], [g("2020", "2020-05-01"), g("2020-06", None)]) == []   # partial dates left out
    ch = dates.plan([r(1, None, "2020-05-01")], [g("2020-06-01", "2020-05-01")])             # start after finish: ignored
    assert ch == []


def test_pair_books():
    hc = [{"id": 10, "title": "A Game of Thrones", "authors": ["George R. R. Martin"], "isbns": ["9780553588484"], "reads": []},
          {"id": 11, "title": "The Hobbit, or There and Back Again", "authors": ["J.R.R. Tolkien"], "isbns": [], "reads": []},
          {"id": 12, "title": "Dune", "authors": ["Frank Herbert"], "isbns": [], "reads": []},
          {"id": 13, "title": "Linked Thing", "authors": ["X"], "isbns": [], "reads": []}]
    gr = [{"id": "1", "title": "A Game of Thrones (A Song of Ice and Fire, #1)", "authors": ["Martin, George R.R."],
           "isbns": ["9780553588484"], "reads": [g(None, "2022-09-15")]},
          {"id": "2", "title": "A Game of Thrones", "authors": ["Martin, George R.R."], "isbns": ["0000000000"],
           "reads": [g("2022-08-01", "2022-09-15"), g(None, "2022-09-15")]},                  # a second edition: merged
          {"id": "3", "title": "The Hobbit, or There and Back Again", "authors": ["Tolkien, J.R.R. *"], "isbns": [], "reads": []},
          {"id": "4", "title": "Dune Messiah (Dune, #2)", "authors": ["Herbert, Frank"], "isbns": [], "reads": []},
          {"id": "5", "title": "Anything", "authors": ["Y"], "isbns": [], "reads": []}]
    pairs = {b["id"]: row for b, row in dates.pair_books(gr, hc, {"5": 13})}
    assert set(pairs) == {10, 11, 13}                                         # Dune Messiah isn't Dune
    assert pairs[10]["reads"] == [g(None, "2022-09-15"), g("2022-08-01", "2022-09-15")]
    assert dates.gr_author("Martin, George R.R. *") == "George R.R. Martin"
    assert dates.gr_title("The Wind Through the Keyhole (Dark Tower, #4.5)") == "The Wind Through the Keyhole"


ROW = """<tr id="review_1" class="bookalike review"><td class="field title"><div class="value">
<a title="A Game of Thrones (A Song of Ice and Fire, #1)" href="/book/show/13496.A_Game_of_Thrones">A Game of Thrones</a></div></td>
<td class="field author"><div class="value"><a href="/author/show/346732.George_R_R_Martin">Martin, George R.R.</a> *</div></td>
<td class="field isbn" style="display: none"><div class="value">0553588486</div></td>
<td class="field isbn13" style="display: none"><div class="value">    9780553588484
</div></td>
<td class="field date_started" style="display: none"><div class="value">
<div class="date_row"><span class="date_started_value">Aug 01, 2022</span>
<a href="#" onclick="reviewEditor.summon(this, 13496, 'started_at', {value: &quot;2022-08-01&quot;, reading_session_id: &quot;s1&quot;}); return false;">[edit]</a></div>
<div class="date_row"><span class="greyText">not set</span>
<a href="#" onclick="reviewEditor.summon(this, 13496, 'started_at', {value: {}, reading_session_id: &quot;s2&quot;}); return false;">[edit]</a></div></div></td>
<td class="field date_read"><div class="value">
<div class="date_row"><a href="#" onclick="reviewEditor.summon(this, 13496, 'read_at', {value: &quot;2022-09-15&quot;, reading_session_id: &quot;s1&quot;}); return false;">[edit]</a></div>
<div class="date_row"><a href="#" onclick="reviewEditor.summon(this, 13496, 'read_at', {value: &quot;2015&quot;, reading_session_id: &quot;s2&quot;}); return false;">[edit]</a></div>
</div></td></tr>"""


def test_parse_read_list():
    rows = parse_read_list("<table>" + ROW + "</table>")
    assert rows == [{"id": "13496", "title": "A Game of Thrones (A Song of Ice and Fire, #1)", "authors": ["Martin, George R.R."],
                     "isbns": ["9780553588484", "0553588486"],
                     "reads": [{"started": "2022-08-01", "finished": "2022-09-15"}, {"started": None, "finished": "2015"}]}]


def test_hardcover_reading_dates_and_writes():
    hc = HardcoverClient("T"); hc._uid = 42
    sent = []
    ub = {"id": 5, "user_id": 42, "edition_id": 111,
          "user_book_reads": [{"id": 70, "edition_id": 111, "started_at": None, "finished_at": "2022-09-15",
                               "progress_pages": 300, "progress_seconds": None}]}
    def post(url, json=None, **kw):
        q, v = json["query"], json["variables"]; sent.append((q, v))
        if "user_books(where: {user_id" in q:
            data = {"user_books": [{"id": 5, "status_id": 3, "book": {"title": "T", "contributions": [{"author": {"name": "A"}}]},
                                    "edition": {"isbn_13": "978", "isbn_10": None},
                                    "user_book_reads": [{"id": 70, "started_at": None, "finished_at": "2022-09-15"}]}]}
        elif "user_books(where: {id" in q:
            data = {"user_books": [ub]}
        elif "update_user_book_read" in q:
            data = {"update_user_book_read": {"id": v["id"], "error": None}}
        elif "insert_user_book_read" in q:
            data = {"insert_user_book_read": {"id": 99, "error": None}}
        return Resp(200, {"data": data})
    with mock.patch("requests.post", side_effect=post):
        assert hc.reading_dates() == [{"id": 5, "title": "T", "authors": ["A"], "status": "read", "isbns": ["978"],
                                       "reads": [{"id": 70, "started": None, "finished": "2022-09-15"}]}]
        sent.clear()
        hc.set_read_dates(5, 70, "2022-08-01", "2022-09-15", was=[None, "2022-09-15"])
        assert sent[-1][1] == {"id": 70, "o": {"edition_id": 111, "progress_pages": 300, "progress_seconds": None,
                                               "started_at": "2022-08-01", "finished_at": "2022-09-15"}}   # rest kept
        sent.clear()
        try: hc.set_read_dates(5, 70, "2022-08-01", "2022-09-15", was=["2021-01-01", "2022-09-15"]); assert False
        except HardcoverError as e: assert "changed since" in str(e)
        assert not any("mutation" in q for q, _ in sent)                       # nothing written
        assert hc.set_read_dates(5, None, "2019-01-01", "2019-02-01") == 99
        assert sent[-1][1] == {"id": 5, "o": {"started_at": "2019-01-01", "finished_at": "2019-02-01", "edition_id": 111}}
        try: hc.set_read_dates(5, 71, "2019-01-01", "2019-02-01"); assert False
        except HardcoverError as e: assert "no longer" in str(e)
        ub["user_id"] = 7
        try: hc.set_read_dates(5, 70, "2022-08-01", "2022-09-15"); assert False
        except HardcoverError as e: assert "isn't in your" in str(e)


def test_finish_date_is_the_latest_finished_read():
    ub = {"id": 1, "status_id": 3, "book": {"title": "T"},
          "user_book_reads": [{"id": 9, "started_at": "2022-09-17", "finished_at": None}],     # a re-read never finished
          "last_finished": [{"finished_at": "2022-09-15"}]}
    assert book_from_ub(ub).finished_on == "2022-09-15"


def test_engine_scan_and_apply():
    from heliograph.config import Config
    from heliograph.engine import Engine
    from heliograph.providers.base import Provider
    from heliograph.state import State

    class GR(Provider):
        key, label = "goodreads", "Goodreads"
        def book_url(self, rid): return f"https://gr/{rid}"
        def reading_dates(self, progress=None):
            progress and progress(1)
            return [{"id": "1", "title": "T", "authors": ["A"], "isbns": ["978"], "reads": [g("2022-08-01", "2022-09-15")]}]

    class HC:
        def __init__(self): self.writes = []
        def reading_dates(self):
            return [{"id": 5, "title": "T", "authors": ["A"], "isbns": ["978"], "status": "read", "reads": [r(70, None, "2022-09-15")]}]
        def set_read_dates(self, *a): self.writes.append(a)
        def fetch_one(self, ub): return None

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg, log = Config(os.path.join(tmp, "c.json")), logging.getLogger("t")
        hc = HC()
        eng = Engine(cfg, [GR(cfg, log)], State(os.path.join(tmp, "s.db")), log, hardcover=hc)
        eng.start_dates_scan()
        for _ in range(100):
            if not eng.dates_job["running"]: break
            time.sleep(0.05)
        j = eng.dates_job
        assert j["matched"] == 1 and len(j["items"]) == 1 and j["items"][0]["pick"], j["message"]
        try: eng.apply_dates([]); assert False
        except ValueError: pass
        eng.apply_dates([j["items"][0]["key"]])
        for _ in range(100):
            if not eng.dates_job["running"]: break
            time.sleep(0.05)
        assert hc.writes == [(5, 70, "2022-08-01", "2022-09-15", (None, "2022-09-15"))]
        assert j["items"][0]["result"].startswith("✓") and j["message"].startswith("Done: 1")
        try: eng.apply_dates([j["items"][0]["key"]]); assert False            # already done: nothing left to apply
        except ValueError: pass


ALL = [test_plan_fills_missing_start_dates, test_plan_rereads_mismatches_and_bad_dates, test_pair_books, test_parse_read_list,
       test_hardcover_reading_dates_and_writes, test_finish_date_is_the_latest_finished_read, test_engine_scan_and_apply]

if __name__ == "__main__":
    for t in ALL:
        try:
            t()
        except Exception:
            print("FAILED:", t.__name__); raise
    print("dates tests passed")
