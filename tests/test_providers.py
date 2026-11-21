import json, logging, sys, os, tempfile, time
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from heliograph import matching
from heliograph.config import Config
from heliograph.hardcover import Book
from heliograph.providers import goodreads as gr, storygraph as sg, pagebound as pb, fable as fb
from heliograph.providers.base import AuthError, mapped_page

LOG = logging.getLogger("t")


class Resp:
    def __init__(self, status=200, body="", headers=None, cookies=None, url="https://www.goodreads.com/"):
        self.status_code, self.text, self.headers, self.url = status, body if isinstance(body, str) else json.dumps(body), headers or {}, url
        self.content = self.text.encode(); self._c = cookies or {}; self.history = []
        self.cookies = mock.Mock(get_dict=lambda: self._c, get=lambda k: self._c.get(k))
    def json(self): return json.loads(self.text)


def cfg(tmp):
    return Config(os.path.join(tmp, "c.json"))


# ------------------------------------------------------------------ matching (ported scoring)
def test_matching():
    cands = [{"id": 1, "title": "The Hobbit, or There and Back Again", "authors": ["J.R.R. Tolkien"]},
             {"id": 2, "title": "The Hobbit", "authors": ["J. R. R. Tolkien"]},
             {"id": 3, "title": "The Hobbit", "authors": ["Someone Else"]}]
    assert matching.best_match("Hobbit", ["J.R.R. Tolkien"], cands)["id"] == 2          # leading "the" ignored
    assert matching.best_match("The Hobbit", ["Nobody Known"], cands) is None            # author must agree
    assert matching.best_match("The Hobbit", [], cands)["id"] in (2, 3)                  # no author -> near-exact title
    assert matching.best_match("Dune", ["Frank Herbert"], cands) is None
    assert matching.best_match("The Hobbit", ["T"], [{"title": "The Hobbitt", "authors": ["Unknown Author"]}]) is None
    assert matching.score("A & B", ["x"], "A and B", ["x"]) is not None


def test_page_mapping():
    b = Book(1, "T", pages=50, total_pages=200)
    assert mapped_page(b, 300) == 75 and mapped_page(b, 200) == 50 and mapped_page(b, None) == 50
    assert mapped_page(Book(1, "T", pages=250, total_pages=200), 300) == 300             # clamped


# ------------------------------------------------------------------ goodreads
def test_goodreads_helpers():
    assert gr.quarter_stars(4.5) == 4.5 and gr.quarter_stars(4.4) == 4.25 and gr.quarter_stars(5) == 5
    assert gr.merge_cookies("a=1; b=2", {"b": "3", "c": "4"}) == "a=1; b=3; c=4"
    html = '<a data-testid="book-item-title"><a href="/book/show/77-x">Dune &amp; Co</a></a>'
    html = ('<div data-testid="book-item-kca://book/amzn1.gr.book.v1.X"><img data-testid="responsive-image" srcSet="a 1x, b 2x" '
            'src="https://i.gr-assets.com/c/77.jpg"></div> ... data-testid="book-item-title"><a href="/book/show/77-x">Dune &amp; Co</a> '
            '... data-testid="name">Frank Herbert</span>')
    assert gr.parse_search_results(html) == [{"id": "77", "title": "Dune & Co", "authors": ["Frank Herbert"],
                                              "cover": "https://i.gr-assets.com/c/77.jpg"}]
    from heliograph.providers.base import clean_cover
    assert clean_cover("//x/y.jpg") == "https://x/y.jpg" and clean_cover("http://x/y.jpg") is None and clean_cover("javascript:1") is None
    assert clean_cover("/img/a.jpg", "https://s.example") == "https://s.example/img/a.jpg"
    page = '<script>{"ROOT_QUERY":{"getBookByLegacyId({\\"legacyId\\":\\"77\\"})":{"__ref":"Book:1"}},"Book:1":{"details":{"numPages":412}}}</script>'
    assert gr.book_page_pages(page, "77") == 412


def test_goodreads_refresher_on_waf():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="old=1", cookie_refresh_url="http://host.docker.internal:5080/", cookie_refresh_token="T")
        p = gr.Goodreads(c, LOG); seen = []
        def fake(method, url, **kw):
            seen.append((method, url, kw["headers"].get("Cookie")))
            if url.startswith("http://host.docker"):
                assert kw["headers"]["X-Auth-Token"] == "T"
                return Resp(200, "new=2")
            if kw["headers"]["Cookie"].startswith("old"):
                return Resp(202, "", {"x-amzn-waf-action": "challenge"})
            return Resp(200, '<meta name="csrf-token" content="C">', url=url)
        with mock.patch("requests.request", side_effect=fake):
            assert "OK" in p.check_auth()
        assert seen[1][:2] == ("POST", "http://host.docker.internal:5080/refresh") and c["goodreads"]["cookie"] == "new=2"
        assert seen[2][2] == "new=2"


def test_goodreads_bootstraps_empty_cookie_from_refresher():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="", cookie_refresh_url="http://r:5080")
        p = gr.Goodreads(c, LOG); seen = []
        def fake(method, url, **kw):
            seen.append((method, url))
            return Resp(200, "boot=1") if url.startswith("http://r") else Resp(200, 'csrf-token', url=url)
        with mock.patch("requests.request", side_effect=fake):
            p.check_auth()
        assert seen[0] == ("GET", "http://r:5080/cookie") and c["goodreads"]["cookie"] == "boot=1"


def test_goodreads_auth_failure_without_refresher():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="a=1")
        p = gr.Goodreads(c, LOG)
        with mock.patch("requests.request", return_value=Resp(302, "", {"location": "https://www.goodreads.com/user/sign_in"})):
            try: p.check_auth(); assert False
            except AuthError as e: assert "no Cookie Auto-Refresh URL" in str(e)


def test_goodreads_writes_and_headers():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="a=1")
        p = gr.Goodreads(c, LOG); calls = []
        def fake(method, url, **kw):
            calls.append((method, url, kw.get("data"), kw.get("params"), kw["headers"]))
            return Resp(200, '<meta name="csrf-token" content="C">', url=url)
        b = Book(1, "T", pages=40, total_pages=100)
        with mock.patch("requests.request", side_effect=fake), mock.patch.object(p, "set_date_finished") as dated:
            p.set_status("77", "reading", b); p.set_progress("77", b, True, 200); p.set_progress("77", b, False, 200)
            p.set_rating("77", 4.4, b); p.set_status("77", "read", b)
        posts = [x for x in calls if x[0] == "POST"]
        assert posts[0][2] == {"book_id": "77", "name": "currently-reading", "a": ""}
        assert posts[0][4]["X-Prototype-Version"] == "1.7" and posts[0][4]["X-CSRF-Token"] == "C" and posts[0][4]["Cookie"] == "a=1"
        assert posts[1][1].endswith("/user_status.json") and posts[1][2]["user_status[page]"] == 80   # mapped to the 200-page edition
        assert posts[2][2]["user_status[percent]"] == 40
        assert posts[3][1].endswith("/review/rate/77") and posts[3][3]["rating"] == "4.25" and posts[3][2] == ""
        assert posts[4][2]["name"] == "read" and dated.call_count == 1


def _flight_page(sessions, chunk=True):
    flight = '0:{"readingSessions":%s,"bookId":"kca://book/abc","userFormattedText":"my review"}' % json.dumps(sessions)
    html = "<script>self.__next_f.push(%s)</script>" % json.dumps([1, flight])
    if chunk:
        html += '<script src="/_next/static/chunks/app/review/edit/%5Bid%5D/page-1.js"></script><script src="/_next/static/chunks/webpack-1.js"></script>'
    return html


def test_goodreads_date_finished_flow():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="a=1")
        p = gr.Goodreads(c, LOG); state = {"saved": None, "gets": 0}
        action = "ab" * 20
        js = 'x=createServerReference("%s",callServer,void 0,findSourceMapURL,"submitReviewFormAction")' % action
        def fake(method, url, **kw):
            if method == "GET" and url.endswith("/review/edit/77"):
                state["gets"] += 1
                sess = [] if not state["saved"] else state["saved"]["readingSessions"]
                return Resp(200, _flight_page(sess), url=url)
            if method == "GET" and "/_next/static/chunks/" in url:
                assert "webpack" not in url
                return Resp(200, js, url=url)
            if method == "POST":
                assert kw["headers"]["Next-Action"] == action and kw["headers"]["Content-Type"].startswith("text/plain")
                state["saved"] = json.loads(kw["data"])[0]
                return Resp(200, '1:{"legacyId":"1"}', url=url)
            return Resp(200, '<meta name="csrf-token" content="C">', url=url)
        with mock.patch("requests.request", side_effect=fake):
            assert p.set_date_finished("77", "2026-03-04") is True
        pl = state["saved"]
        assert pl["reviewText"] == "my review" and pl["initialReadingSessions"] == []
        s = pl["readingSessions"][0]
        assert s["state"] == "COMPLETED" and s["endedDate"] == {"__typename": "NullableDate", "year": 2026, "month": 3, "day": 4}


def test_goodreads_finished_sessions_logic():
    fin = {"__typename": "NullableDate", "year": 2026, "month": 3, "day": 4}
    reading = {"bookId": "kca://b", "state": "READING", "startedDate": {"year": 2026, "month": 1, "day": 1}}
    out, already, err = gr.prepare_finished_sessions([reading], "kca://b", fin, 1)
    assert out[0]["endedDate"] == fin and not already and not err                      # closes the active session
    assert gr.prepare_finished_sessions([{"endedDate": fin}], "kca://b", fin, 1)[1] is True
    assert gr.prepare_finished_sessions([{}] * 25, "kca://b", fin, 1)[2]


# ------------------------------------------------------------------ storygraph
SEARCH_HTML = '''<div class="pane"><div class="book-title-author-and-series"><h3><a href="/books/aaa-1">The Hobbit</a></h3>
<p><a href="/authors/t">J.R.R. Tolkien</a></p></div><p class="text-xs">310 pages</p>
<div class="edition-info" data-book-id="aaa-1"><p>Format: Audio</p></div></div>
<div class="pane"><img src="/covers/bbb-2.jpg"><div class="book-title-author-and-series"><h3><a href="/books/bbb-2">The Hobbit</a></h3>
<p><a href="/authors/t">J.R.R. Tolkien</a></p></div><p class="text-xs">310 pages</p>
<div class="edition-info" data-book-id="bbb-2"><p>Format: Paperback</p></div></div>'''


def _page(button=None, pages=310, other=None, review=None, fmt="Paperback", rid="x"):
    h = '<meta name="csrf-token" content="CS">'
    if button: h += f'<button class="read-status-label bg">{button}</button>'
    h += f'<input name="read_status[book_num_of_pages]" value="{pages}">'
    h += f'<div class="edition-info" data-book-id="{rid}"><p>Format: {fmt}</p></div>'
    if other: h += f'<a href="/books/{other}">Currently reading another edition</a>'
    if review: h += f'<a href="{review[0]}">{review[1]}</a>'
    return h


def test_storygraph_search_and_status_parsing():
    res = sg.parse_search(SEARCH_HTML)
    assert [r["id"] for r in res] == ["bbb-2"] and res[0]["authors"] == ["J.R.R. Tolkien"] and res[0]["pages"] == 310   # audio filtered
    assert res[0]["cover"] == "https://app.thestorygraph.com/covers/bbb-2.jpg"
    info = sg.parse_book_page(_page("Currently Reading", review=("/reviews/9", "See review")), "x")
    assert info["status"] == "reading" and info["pages"] == 310 and info["review_href"] == "/reviews/9" and info["csrf"] == "CS"
    assert sg.parse_book_page(_page("Read"), "x")["status"] == "read" and sg.parse_book_page(_page("To-Read"), "x")["status"] == "want"


def test_storygraph_edition_redirect_and_audio_refusal():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["storygraph"].update(enabled=True, session_cookie="S", remember_user_token="R")
        p = sg.StoryGraph(c, LOG)
        pages = {"/books/one": _page(None, other="two", rid="one"), "/books/two": _page("Currently Reading", 500, rid="two"),
                 "/books/aud": _page("Currently Reading", 0, fmt="Audio", rid="aud")}
        def fake(method, url, **kw):
            return Resp(200, pages[url.replace(sg.BASE, "")], url=url)
        with mock.patch("requests.request", side_effect=fake):
            m = p.finalize({"id": "one", "title": "T", "pages": 310})
            assert (m.id, m.pages) == ("two", 500)                 # follows to the edition you have a status on
            assert p.finalize({"id": "aud", "title": "T"}) is None  # refuses audio editions


def test_storygraph_writes():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["storygraph"].update(enabled=True, session_cookie="S", remember_user_token="R")
        p = sg.StoryGraph(c, LOG); calls = []
        review_form = ('<form action="/reviews/9" method="post"><input type="hidden" name="_method" value="patch">'
                       '<input type="hidden" name="authenticity_token" value="FT"><input name="stars_integer" value="2">'
                       '<textarea name="review[explanation]">nice</textarea></form>')
        def fake(method, url, **kw):
            calls.append((method, url.replace(sg.BASE, ""), kw.get("data"), kw.get("params"), kw["headers"]))
            if "/reviews/9/edit" in url: return Resp(200, review_form, url=url)
            if method == "GET": return Resp(200, _page("Currently Reading", 400, review=("/reviews/9", "See review")), url=url)
            return Resp(302 if "update-status" in url else 200, "", {"location": "/books/x"})
        b = Book(1, "T", pages=100, total_pages=200, rating=4.25)
        with mock.patch("requests.request", side_effect=fake):
            p.set_status("x", "read", b); p.set_progress("x", b, True, None); p.set_progress("x", b, False, None); p.set_rating("x", 4.25, b)
        posts = [x for x in calls if x[0] == "POST"]
        assert posts[0][1] == "/update-status.js" and posts[0][3] == {"book_id": "x", "status": "read"}
        assert posts[1][2]["read_status[progress_number]"] == 200 and posts[1][2]["read_status[progress_type]"] == "pages"   # 100/200 of the 400-page edition
        assert posts[2][2]["read_status[progress_number]"] == 50 and posts[2][2]["read_status[progress_type]"] == "percentage"
        fields = dict(posts[3][2])
        assert posts[3][1] == "/reviews/9" and fields["_method"] == "patch" and fields["stars_integer"] == "4" and fields["stars_decimal"] == "25"
        assert fields["review[explanation]"] == "nice" and fields["authenticity_token"] == "FT"


def test_storygraph_form_roundtrip():
    html = '''<form action="/reviews/55" method="post"><input type="hidden" name="_method" value="patch">
      <textarea name="review[explanation]">nice</textarea><select name="review[pace]"><option value="">-</option>
      <option value="fast" selected>fast</option></select><input type="checkbox" name="x" value="1">
      <input type="checkbox" name="y" value="1" checked><input type="hidden" name="review[mood_ids][]" value="2">
      <input type="hidden" name="review[mood_ids][]" value="5"><input type="submit" name="button"></form>'''
    f = sg.parse_forms(html)[0]
    assert ["review[pace]", "fast"] in f["fields"] and ["y", "1"] in f["fields"] and ["x", "1"] not in f["fields"]
    assert [v for n, v in f["fields"] if n == "review[mood_ids][]"] == ["2", "5"]
    assert sg.star_fields(4.5) == ("4", "50") and sg.star_fields(3) == ("3", "")


# ------------------------------------------------------------------ fable / pagebound
def test_fable_flow():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["fable"].update(enabled=True, id_token="J", refresh_token="R", token_expires_at=time.time() + 3000, user_id="U1")
        p = fb.Fable(c, LOG); calls = []
        def fake(method, url, **kw):
            path = url.replace("https://api.fable.co", ""); calls.append((method, path, kw.get("json")))
            assert kw["headers"]["Authorization"] == "JWT J"
            if path == "/api/books/B1": return Resp(200, {"response": {"status": "want_to_read"}})
            if path == "/api/books/B1/editions/":
                return Resp(200, {"results": [{"display_isbn": "x", "page_count": 0}, {"is_current_book": True, "page_count": 320}]})
            if path.startswith("/api/books/search/"):
                return Resp(200, {"response": {"books": [{"id": 5, "title": "T", "isbn": "111", "authors": [{"name": "A"}]}]}})
            if path.startswith("/api/v2/users/U1/book_lists?"):
                return Resp(200, {"results": [{"type": "system", "system_type": t, "id": "L-" + t} for t in fb.SYSTEM_TYPE.values()]})
            if path.endswith("/reviews/B1"): return Resp(404, "")
            return Resp(201, {})
        b = Book(1, "T", pages=40, total_pages=100, rating=4.5)
        with mock.patch("requests.request", side_effect=fake):
            m = p.lookup_isbn("111"); assert m.id == "5"
            assert p._edition_pages("B1", None) == 320
            p.set_status("B1", "reading", b); p.set_progress("B1", b, True, 320); p.set_progress("B1", b, False, 320); p.set_rating("B1", 4.5, b)
        post = [x for x in calls if x[0] == "POST"]
        assert post[0][2]["book_list_ids"] == ["L-current_reading"] and "L-current_reading" not in post[0][2]["exclude_from"]
        assert post[1][2]["selected_mode"] == "page_count" and post[1][2]["current_page"] == 128 and post[1][2]["page_count"] == 320
        assert post[2][2]["selected_mode"] == "percentage" and post[2][2]["current_percentage"] == 40
        assert post[3][1] == "/api/books/B1/reviews" and post[3][2]["rating"] == 4.5


def test_pagebound_flow():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["pagebound"].update(enabled=True, id_token="I", refresh_token="R", api_token="A", token_expires_at=time.time() + 3000)
        p = pb.Pagebound(c, LOG); calls = []; state = {"has": False}
        def fake(method, url, **kw):
            path = url.replace(pb.API, ""); calls.append((method, path, kw.get("json")))
            if path == "/api/v1/books/UU":
                ub = {"id": 9, "uuid": "ubu", "status": "current", "book_id": 5, "page_count": 200, "current_reading_instance":
                      {"id": 33, "user_book_id": 9, "current": True}} if state["has"] else None
                return Resp(200, {"book": {"id": 5, "uuid": "UU", "page_count": 200, "user_book": ub}})
            if method == "POST" and path == "/api/v1/user_books": state["has"] = True
            return Resp(201, {})
        b = Book(1, "T", pages=50, total_pages=200, finished_on="2026-02-03")
        with mock.patch("requests.request", side_effect=fake):
            p.set_status("5:UU", "reading", b); p.set_progress("5:UU", b, True, 200); p.set_rating("5:UU", 4.75, b)
            p.set_status("5:UU", "read", b)
        post = {x[1]: x[2] for x in calls if x[0] == "POST"}
        assert post["/api/v1/user_books"]["user_book"]["status"] == "current" and post["/api/v1/user_books"]["user_book"]["book_id"] == 5
        ru = post["/api/v1/reading_updates"]
        assert ru["reading_update"]["total_progress"] == 25 and ru["reading_update"]["total_pages_read"] == "50" and ru["progress_method"] == "pages"
        assert post["/api/v1/reviews"]["review"]["overall_rating"] == 4.5          # half stars, floored
        put = [x for x in calls if x[0] == "PUT"][-1]
        assert put[2]["status"] == "finished" and put[2]["finished_reading_at"] == "2/3/2026"
        assert pb.status_payload("reading", None)["total_minutes"] is None


def test_pagebound_search_candidates():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp); c["pagebound"].update(enabled=True)
        p = pb.Pagebound(c, LOG)
        hit = {"hits": [{"document": {"id": 5, "uuid": "UU", "title": "The Hobbit", "author_name": "J.R.R. Tolkien", "page_count": 310}}]}
        with mock.patch("requests.get", return_value=Resp(200, hit)):
            m = p.find(Book(1, "The Hobbit", authors=["J.R.R. Tolkien"], isbn13="978"), True, True)
        assert (m.id, m.pages) == ("5:UU", 310)           # ISBN is skipped (no ISBN lookup), title matched by score
        with mock.patch("requests.get", return_value=Resp(200, hit)):
            assert p.find(Book(1, "Totally Different", authors=["Someone"]), True, True) is None


# ------------------------------------------------------------------ config
def test_env_overrides():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "c.json")
        c = Config(path); c["hardcover"]["token"] = "filetoken"; c.save()
        os.environ.update({"HELIOGRAPH__hardcover__token": "envtoken", "HELIOGRAPH__goodreads__enabled": "true",
                           "HELIOGRAPH__common__poll_interval_minutes": "9", "HELIOGRAPH__fable__password": "pw"})
        try:
            c = Config(path)
            assert c["hardcover"]["token"] == "envtoken" and c["goodreads"]["enabled"] is True
            assert c["common"]["poll_interval_minutes"] == 9 and c["fable"]["password"] == "pw"
            c["goodreads"]["cookie"] = "refreshed=1"; c.save()
            saved = json.load(open(path))
            assert saved["hardcover"]["token"] == "filetoken" and saved["goodreads"]["enabled"] is False
            assert saved["goodreads"]["cookie"] == "refreshed=1" and saved["fable"]["password"] == ""
        finally:
            for k in [k for k in os.environ if k.startswith("HELIOGRAPH__")]:
                del os.environ[k]


ALL = [test_matching, test_page_mapping, test_goodreads_helpers, test_goodreads_refresher_on_waf,
       test_goodreads_bootstraps_empty_cookie_from_refresher, test_goodreads_auth_failure_without_refresher,
       test_goodreads_writes_and_headers, test_goodreads_date_finished_flow, test_goodreads_finished_sessions_logic,
       test_storygraph_search_and_status_parsing, test_storygraph_edition_redirect_and_audio_refusal,
       test_storygraph_writes, test_storygraph_form_roundtrip, test_fable_flow, test_pagebound_flow,
       test_pagebound_search_candidates, test_env_overrides]

if __name__ == "__main__":
    for t in ALL:
        try:
            t()
        except Exception:
            print("FAILED:", t.__name__); raise
    print("provider tests passed")
