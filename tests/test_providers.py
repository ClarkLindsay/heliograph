import json, logging, sys, os, tempfile, time
from unittest import mock
from urllib.parse import urlparse
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from heliograph import matching
from heliograph.config import Config
from heliograph.hardcover import Book, book_from_ub
from heliograph.state import State
from heliograph.providers import goodreads as gr, storygraph as sg, pagebound as pb, fable as fb
from heliograph.providers.base import AuthError, mapped_page

LOG = logging.getLogger("t")


class Resp:
    def __init__(self, status=200, body="", headers=None, cookies=None, url="https://www.goodreads.com/"):
        self.status_code, self.text, self.headers, self.url = status, body if isinstance(body, str) else json.dumps(body), headers or {}, url
        self.content = self.text.encode(); self._c = cookies or {}; self.history = []
        self.cookies = mock.Mock(get_dict=lambda: self._c, get=lambda k: self._c.get(k))
    def json(self): return json.loads(self.text)
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")


def cfg(tmp):
    c = Config(os.path.join(tmp, "c.json"))
    c["goodreads"]["request_delay_seconds"] = 0          # no real pacing against mocked HTTP
    return c


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
    # co-written book, service lists only the main author
    assert matching.score("White Sand Omnibus", ["Brandon Sanderson", "Rik Hoskin", "Isaac Stewart"],
                          "White Sand Omnibus", ["Brandon Sanderson"]) is not None
    assert matching.score("White Sand Omnibus", ["Brandon Sanderson", "Rik Hoskin"], "White Sand Omnibus", ["Someone Else"]) is None
    # Hardcover's subtitle dropped by the service: matches, but ranks below a full-title match
    hob = [{"id": "slip", "title": "The Hobbit (Part 1 and 2) Slipcase", "authors": ["J.R.R. Tolkien"]},
           {"id": "plain", "title": "The Hobbit ", "authors": ["J.R.R. Tolkien"]},
           {"id": "gn", "title": "The Hobbit: or There and Back Again (Graphic Novel, Book 3)", "authors": ["Chuck Dixon"]}]
    assert matching.best_match("The Hobbit, or There and Back Again", ["J.R.R. Tolkien"], hob)["id"] == "plain"
    assert matching.best_match("The Hobbit, or There and Back Again", ["J.R.R. Tolkien"], hob[:1]) is None   # slipcase isn't the book
    assert matching.best_match("The Hobbit, or There and Back Again", [], hob[1:2]) is None                  # never without an author
    # their subtitle is never dropped: a bare title can't grab one volume of a series
    vols = [{"id": 1, "title": "Vampire: The Masquerade — Winter’s Teeth, Vol. 1", "authors": ["Tim Seeley"]}]
    assert matching.best_match("Vampire", ["Tim Seeley"], vols) is None
    assert matching.score("Winter’s Teeth, Vol. 2", ["Tim Seeley"], "Winter's Teeth, Vol. 2", ["Tim Seeley"]) is not None


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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="old=1", cookie_refresh_url="http://host.docker.internal:5080/", cookie_refresh_token="T")
        p = gr.Goodreads(c, LOG); seen = []
        def fake(method, url, **kw):
            seen.append((method, url, kw["headers"].get("Cookie")))
            if urlparse(url).hostname == "host.docker.internal":
                assert kw["headers"]["X-Auth-Token"] == "T"
                return Resp(200, "new=2")
            if kw["headers"]["Cookie"].startswith("old"):
                return Resp(202, "", {"x-amzn-waf-action": "challenge"})
            return Resp(200, '<meta name="csrf-token" content="C">', url=url)
        with mock.patch("requests.request", side_effect=fake):
            assert "OK" in p.check_auth()
        assert seen[1][:2] == ("POST", "http://host.docker.internal:5080/refresh") and c["goodreads"]["cookie"] == "new=2"
        assert seen[2][2] == "new=2"


def test_goodreads_refresher_accounts():
    """Multi-account cookie refresher: /accounts/<id>/cookie|refresh|health, one shared token."""
    assert gr.account_url("http://r:5080/", "") == "http://r:5080" and gr.account_url("http://r:5080", "default") == "http://r:5080"
    assert gr.account_url("http://r:5080", "Sam") == "http://r:5080/accounts/sam"
    assert gr.account_url("http://r:5080/accounts/sam/", "other") == "http://r:5080/accounts/sam"    # URL already says which
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="", cookie_refresh_url="http://r:5080", cookie_refresh_token="T",
                                            cookie_refresh_account="sam")
        p = gr.Goodreads(c, LOG); seen = []
        def fake(method, url, **kw):
            seen.append((method, url, kw["headers"].get("X-Auth-Token")))
            return Resp(200, "sam=1") if url.endswith("/cookie") or url.endswith("/refresh") else Resp(200, "csrf-token", url=url)
        with mock.patch("requests.request", side_effect=fake):
            p.check_auth()
        assert seen[0] == ("GET", "http://r:5080/accounts/sam/cookie", "T") and c["goodreads"]["cookie"] == "sam=1"
        # the admin's refresher, shared: the account the admin assigned, the admin's token
        c["goodreads"].update(cookie="", cookie_refresh_url="", cookie_refresh_token="", cookie_refresh_account="")
        p = gr.Goodreads(c, LOG); seen.clear()
        p.shared_refresher = lambda: {"url": "http://admin-r:5080", "token": "ADMIN", "account": "bob"}
        with mock.patch("requests.request", side_effect=fake):
            p.check_auth()
        assert seen[0] == ("GET", "http://admin-r:5080/accounts/bob/cookie", "ADMIN")
        c["goodreads"]["cookie"] = ""; p = gr.Goodreads(c, LOG); seen.clear()
        p.shared_refresher = lambda: {"error": "ask the admin to assign you an account"}
        with mock.patch("requests.request", side_effect=fake):
            try: p.check_auth(); assert False
            except AuthError: pass
        assert seen == []                                                         # nothing asked for
        assert p.refresher_endpoint()[2] == "ask the admin to assign you an account"
        # health line for the Test button, and hints for the refresher's errors
        p.shared_refresher = lambda: {"url": "http://admin-r:5080", "token": "ADMIN", "account": "bob"}
        with mock.patch("requests.get", return_value=Resp(200, {"account": "bob", "logged_in": False})):
            assert "NOT logged in" in p.refresher_status() and "[ShelfSync: bob]" in p.refresher_status() and "shared" in p.refresher_status()
        with mock.patch("requests.get", return_value=Resp(200, {"account": "bob", "logged_in": True})):
            assert p.refresher_status() == "refresher account 'bob' logged in (shared by the admin)"
        assert "several accounts" in gr.refresher_hint(Resp(400, "specify an account")) and "unknown refresher account" in gr.refresher_hint(Resp(404, "unknown account 'x'"))


def test_profile_links():
    """Dashboard icons open your profile: worked out from each service's session, only ever into its own site."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp)
        c["goodreads"].update(enabled=True, cookie="a=1"); c["storygraph"].update(enabled=True, session_cookie="S")
        c["fable"].update(id_token="I", refresh_token="R", token_expires_at=time.time() + 3000, user_id="U")
        c["pagebound"].update(id_token="I", refresh_token="R", api_token="A", token_expires_at=time.time() + 3000, user_id="234")
        g = gr.Goodreads(c, LOG)
        with mock.patch("requests.request", side_effect=[Resp(302, "", {"location": "/review/list/157306489-sam"}, url="x"),
                                                         Resp(200, "csrf-token", url="x")]):
            assert g.profile_url() == "https://www.goodreads.com/user/show/157306489"
        html = '<a href="/profile/edit/sam">Edit</a><a href="/profile/sam">Profile</a>'
        with mock.patch("requests.request", return_value=Resp(200, html, url=sg.BASE + "/")):
            assert sg.StoryGraph(c, LOG).profile_url() == "https://app.thestorygraph.com/profile/sam"
        with mock.patch("requests.request", return_value=Resp(200, {"id": "U", "url": "https://fable.co/fabler/sam-123"})):
            assert fb.Fable(c, LOG).profile_url() == "https://fable.co/fabler/sam-123"
        with mock.patch("requests.request", return_value=Resp(200, {"id": 234, "username": "sam b"})):
            assert pb.Pagebound(c, LOG).profile_url() == "https://pagebound.co/users/sam%20b"
        from heliograph.providers import librarything as lt
        c["librarything"].update(username="", session_cookie="cookie_userid=samlt; cookie_usernum=1")
        assert lt.LibraryThing(c, LOG).profile_url() == "https://www.librarything.com/profile/samlt"
        # only links into the service's own site; your own link always wins
        assert g.is_own_url("https://www.goodreads.com/user/show/1") and not g.is_own_url("https://evil.example/goodreads.com")
        assert not g.is_own_url("http://www.goodreads.com/x") and not g.is_own_url("https://notgoodreads.com/x")
        st = {}
        g.state = mock.Mock(get=lambda k, d=None: st.get(k, d))
        assert g.home_url() == "https://www.goodreads.com/"
        st["profile_url:goodreads"] = "https://www.goodreads.com/user/show/1"; assert g.home_url().endswith("/user/show/1")
        st["profile_url:goodreads"] = "https://evil.example/"; assert g.home_url() == "https://www.goodreads.com/"
        c["goodreads"]["home_url"] = "https://www.goodreads.com/review/list/1"; assert g.home_url().endswith("/review/list/1")


def test_goodreads_cools_down_after_uncleared_waf():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="old=1", cookie_refresh_url="http://r:5080")
        p = gr.Goodreads(c, LOG); seen = []
        def fake(method, url, **kw):
            seen.append(url)
            return Resp(503, "") if urlparse(url).hostname == "r" else Resp(202, "", {"x-amzn-waf-action": "challenge"})
        with mock.patch("requests.request", side_effect=fake), mock.patch("time.sleep"):
            try: p.check_auth(); assert False
            except AuthError: pass
            n = len(seen)
            try: p.check_auth(); assert False
            except AuthError as e: assert "pausing after a WAF challenge" in str(e)
        assert len(seen) == n                                 # nothing sent to Goodreads while cooling down


def test_goodreads_pacing_and_reuse():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="a=1", request_delay_seconds=3)
        p = gr.Goodreads(c, LOG); urls, slept = [], []
        def fake(method, url, **kw):
            urls.append((method, url))
            return Resp(200, '<meta name="csrf-token" content="C">', url=url)
        b = Book(1, "T", pages=40, total_pages=100)
        with mock.patch("requests.request", side_effect=fake), mock.patch("time.sleep", side_effect=slept.append):
            p._pages("77"); p._book_page("77")                # second fetch of the same book page is reused
            p.set_status("77", "reading", b); p.set_progress("77", b, True, 200)
        assert [u for m, u in urls if u.endswith("/book/show/77")] == [gr.BASE + "/book/show/77"]
        assert sum(1 for m, u in urls if u == gr.BASE + "/") == 1      # one CSRF fetch for both writes
        assert len(slept) == len(urls) - 1 and all(2.9 < s <= 3 for s in slept)   # steady gap between requests


def test_goodreads_bootstraps_empty_cookie_from_refresher():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="", cookie_refresh_url="http://r:5080")
        p = gr.Goodreads(c, LOG); seen = []
        def fake(method, url, **kw):
            seen.append((method, url))
            return Resp(200, "boot=1") if urlparse(url).hostname == "r" else Resp(200, 'csrf-token', url=url)
        with mock.patch("requests.request", side_effect=fake):
            p.check_auth()
        assert seen[0] == ("GET", "http://r:5080/cookie") and c["goodreads"]["cookie"] == "boot=1"


def test_goodreads_auth_failure_without_refresher():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="a=1")
        p = gr.Goodreads(c, LOG)
        with mock.patch("requests.request", return_value=Resp(302, "", {"location": "https://www.goodreads.com/user/sign_in"})):
            try: p.check_auth(); assert False
            except AuthError as e: assert "no Cookie Auto-Refresh URL" in str(e)


def test_goodreads_writes_and_headers():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="a=1")
        p = gr.Goodreads(c, LOG); calls = []
        def fake(method, url, **kw):
            calls.append((method, url, kw.get("data"), kw.get("params"), kw["headers"]))
            return Resp(200, '<meta name="csrf-token" content="C">', url=url)
        b = Book(1, "T", pages=40, total_pages=100)
        with mock.patch("requests.request", side_effect=fake), mock.patch.object(p, "set_date_finished") as dated:
            p.set_status("77", "reading", b); p.set_progress("77", b, True, 200); p.set_progress("77", b, False, 200)
            p.set_rating("77", 4.4, b); p.set_status("77", "read", b)
            assert dated.call_count == 0                     # no finish date in Hardcover: none invented
            b.finished_on = "2026-02-03"; p.set_status("77", "read", b)
            dated.assert_called_once_with("77", "2026-02-03", None)
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
    start = {"__typename": "NullableDate", "year": 2026, "month": 2, "day": 1}
    out, already, _ = gr.prepare_finished_sessions([], "kca://b", fin, 1, start)        # a new read: Hardcover's start
    assert out[0]["startedDate"] == start and out[0]["endedDate"] == fin and not already
    assert gr.prepare_finished_sessions([], "kca://b", fin, 1)[0][0]["startedDate"] is None   # unknown: none, not the finish
    for old_start in (None, fin):                                                       # no start, or the same-day placeholder
        out, already, _ = gr.prepare_finished_sessions([{"startedDate": old_start, "endedDate": fin}], "kca://b", fin, 1, start)
        assert out[0]["startedDate"] == start and not already
    shelved = {"id": "s1", "bookId": "kca://b", "state": "COMPLETED", "startedDate": None, "endedDate": None}
    out, _, _ = gr.prepare_finished_sessions([shelved], "kca://b", fin, 1, start)        # Goodreads' own undated read
    assert len(out) == 1 and out[0]["id"] == "s1" and out[0]["endedDate"] == fin and out[0]["startedDate"] == start
    dated = {"startedDate": None, "endedDate": {"year": 2019, "month": 5, "day": 1}}
    out, _, _ = gr.prepare_finished_sessions([shelved, dated], "kca://b", fin, 1)       # several reads: a new one is added
    assert len(out) == 3 and out[0] == shelved
    real = {"year": 2026, "month": 1, "day": 9}                                         # a start you set yourself stays
    assert gr.prepare_finished_sessions([{"startedDate": real, "endedDate": fin}], "kca://b", fin, 1, start)[1] is True


# ------------------------------------------------------------------ storygraph
SEARCH_HTML = '''<div class="pane"><div class="book-title-author-and-series"><h3><a href="/books/aaa-1">The Hobbit</a></h3>
<p><a href="/authors/t">J.R.R. Tolkien</a></p></div><p class="text-xs">310 pages</p>
<div class="edition-info" data-book-id="aaa-1"><p>Format: Audio</p></div></div>
<div class="pane"><img src="/covers/bbb-2.jpg"><div class="book-title-author-and-series"><h3><a href="/books/bbb-2">The Hobbit</a></h3>
<p><a href="/authors/t">J.R.R. Tolkien</a></p></div><p class="text-xs">310 pages</p>
<div class="edition-info" data-book-id="bbb-2"><p>Format: Paperback</p></div></div>'''


def _page(button=None, pages=310, other=None, review=None, fmt="Paperback", rid="x", lang="English", other_text="Currently reading another edition"):
    h = '<meta name="csrf-token" content="CS">'
    if button: h += f'<button class="read-status-label bg">{button}</button>'
    h += f'<input name="read_status[book_num_of_pages]" value="{pages}">'
    h += f'<div class="edition-info" data-book-id="{rid}"><p>Format: {fmt}</p><p>Language: {lang}</p></div>'
    if other: h += f'<a href="/books/{other}">{other_text}</a>'
    if review: h += f'<a href="{review[0]}">{review[1]}</a>'
    return h


def test_storygraph_search_and_status_parsing():
    res = sg.parse_search(SEARCH_HTML)
    assert [(r["id"], r["format"]) for r in res] == [("aaa-1", "Audio"), ("bbb-2", "Paperback")]   # audio kept, marked
    assert res[1]["authors"] == ["J.R.R. Tolkien"] and res[1]["pages"] == 310
    assert res[1]["cover"] == "https://app.thestorygraph.com/covers/bbb-2.jpg"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["storygraph"].update(enabled=True, session_cookie="S")
        with mock.patch("requests.request", return_value=Resp(200, SEARCH_HTML)):
            assert [r["id"] for r in sg.StoryGraph(c, LOG).search("hobbit")] == ["bbb-2", "aaa-1"]   # wanted editions first
    info = sg.parse_book_page(_page("Currently Reading", review=("/reviews/9", "See review")), "x")
    assert info["status"] == "reading" and info["pages"] == 310 and info["review_href"] == "/reviews/9" and info["csrf"] == "CS"
    assert sg.parse_book_page(_page("Read"), "x")["status"] == "read" and sg.parse_book_page(_page("To-Read"), "x")["status"] == "want"


def test_storygraph_edition_redirect_and_audio_refusal():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["storygraph"].update(enabled=True, session_cookie="S", remember_user_token="R")
        p = sg.StoryGraph(c, LOG)
        pages = {"/books/one": _page(None, other="two", rid="one"), "/books/two": _page("Currently Reading", 500, rid="two"),
                 "/books/aud": _page("Currently Reading", 0, fmt="Audio", rid="aud")}
        def fake(method, url, **kw):
            return Resp(200, pages[url.replace(sg.BASE, "")], url=url)
        with mock.patch("requests.request", side_effect=fake):
            m = p.finalize({"id": "one", "title": "T", "pages": 310})
            assert (m.id, m.pages, m.switch_from) == ("two", 500, None)   # follows to the edition you have a status on
            pages["/books/aud/editions"] = _editions([("aud", "English", "Audio", 0, True)])
            assert p.finalize({"id": "aud", "title": "T"}) is None  # audio, and its work has no other edition


def _editions(rows):
    """An editions page: rows of (id, language, format, pages, is_yours[, isbn]). Other panes point at yours."""
    mine = next((r[0] for r in rows if r[4]), None)
    out = '<meta name="csrf-token" content="CS">'
    for r in rows:
        bid, lang, fmt, pages, yours = r[:5]
        isbn = r[5] if len(r) > 5 else ""
        out += (f'<div class="book-pane"><a href="/books/{bid}">t</a><p class="text-xs">{pages} pages</p>'
                f'<div class="edition-info"><p>ISBN/UID: {isbn}</p><p>Format: {fmt}</p><p>Language: {lang}</p></div>')
        out += '<button class="read-status-label">read</button>' if yours else (
            f'<a href="/books/{mine}">You\'ve read another edition</a>' if mine else "")
        out += "</div>"
    return out


def test_storygraph_picks_the_right_edition():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["storygraph"].update(enabled=True, session_cookie="S", remember_user_token="R")
        p = sg.StoryGraph(c, LOG); posts = []
        pages = {
            # audiobook found by search, no status: go to its work's editions, take the English print one
            "/books/aud": _page(None, 0, fmt="Audio", rid="aud"),
            "/books/aud/editions": _editions([("aud", "English", "Audio", 0, False), ("sk", "Slovak", "Paperback", 624, False),
                                              ("en-dig", "English", "Digital", 600, False), ("en-pb", "English", "Paperback", 583, False)]),
            # your entry is on the German edition: link the English one and move your entry there
            "/books/de": _page("read", 700, fmt="Paperback", rid="de", lang="German"),
            "/books/de/editions": _editions([("de", "German", "Paperback", 700, True), ("en-hc", "English", "Hardcover", 896, False, "978X"),
                                             ("en-pb2", "English", "Paperback", 604, False, "978Y")]),
            # the English candidate says "You've read another edition" -> that's the German one
            "/books/en-hc": _page(None, 896, other="de", other_text="You've read another edition", rid="en-hc"),
            "/books/en-hc/editions": _editions([("de", "German", "Paperback", 700, True), ("en-hc", "English", "Hardcover", 896, False)]),
            "/books/en-pb2/editions": _editions([("de", "German", "Paperback", 700, True), ("en-pb2", "English", "Paperback", 604, False)]),
        }
        def fake(method, url, **kw):
            path = url.replace(sg.BASE, "")
            if method == "POST":
                posts.append((path, dict(kw.get("data") or {})))
                pages["/books/en-pb2"] = _page("read", 604, rid="en-pb2")          # after the switch your status is there
                return Resp(302, "")
            return Resp(200, pages[path], url=url)
        with mock.patch("requests.request", side_effect=fake):
            m = p.finalize({"id": "aud", "title": "Cibola Burn"})
            assert (m.id, m.pages, m.switch_from) == ("en-pb", 583, None)          # print before digital, never Slovak
            m = p.finalize({"id": "de", "title": "Dune", "want_isbns": ["978Y"]})
            assert (m.id, m.switch_from) == ("en-pb2", "de") and "German" in m.note  # Hardcover's ISBN wins among English
            m = p.finalize({"id": "en-hc", "title": "Dune"})
            assert (m.id, m.switch_from) == ("en-hc", "de")                         # English candidate, entry moved to it
            p.switch_edition("de", "en-pb2")
        assert posts == [("/switch-editions", {"authenticity_token": "CS", "from_book_id": "de", "to_book_id": "en-pb2"})]
        c["common"]["preferred_language"] = ""                                        # blank = any language
        with mock.patch("requests.request", side_effect=fake):
            assert p.finalize({"id": "de", "title": "Dune"}).id == "de"
        c["common"].update(preferred_language="English", avoid_audiobooks=False)
        with mock.patch("requests.request", side_effect=fake):
            assert p.finalize({"id": "aud", "title": "Cibola Burn"}).id == "aud"     # audiobooks allowed if you say so


def test_audiobook_books_get_audiobook_editions():
    """Your Hardcover edition is an audiobook ("Listened"): prefer an audiobook edition, fall back to print."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["storygraph"].update(enabled=True, session_cookie="S", remember_user_token="R")
        p = sg.StoryGraph(c, LOG)
        pages = {
            "/books/pb": _page(None, 300, rid="pb"),
            "/books/pb/editions": _editions([("pb", "English", "Paperback", 300, False), ("de-aud", "German", "Audio", 0, False),
                                             ("en-aud", "English", "Audio", 0, False)]),
            "/books/only-print": _page(None, 250, rid="only-print"),
            "/books/only-print/editions": _editions([("only-print", "English", "Paperback", 250, False)]),
            "/books/mine-print": _page("read", 280, rid="mine-print"),
            "/books/mine-print/editions": _editions([("mine-print", "English", "Hardcover", 280, True),
                                                     ("aud-x", "English", "Audio", 0, False, "AUDX")]),
            "/books/mine-only": _page("read", 280, rid="mine-only"),
            "/books/mine-only/editions": _editions([("mine-only", "English", "Hardcover", 280, True)]),
        }
        with mock.patch("requests.request", side_effect=lambda m, url, **kw: Resp(200, pages[url.replace(sg.BASE, "")], url=url)):
            assert p.finalize({"id": "pb", "title": "T", "want_audio": True}).id == "en-aud"       # English audiobook, not German
            assert p.finalize({"id": "pb", "title": "T"}).id == "pb"                              # read in print: print
            assert p.finalize({"id": "only-print", "title": "T", "want_audio": True}).id == "only-print"   # no audiobook: print
            m = p.finalize({"id": "mine-print", "title": "T", "want_audio": True})
            assert (m.id, m.switch_from) == ("aud-x", "mine-print") and "listened" in m.note      # your entry moves to audio
            m = p.finalize({"id": "mine-print", "title": "T"})
            assert (m.id, m.switch_from) == ("mine-print", None)                                  # read in print: it stays
            m = p.finalize({"id": "mine-only", "title": "T", "want_audio": True})
            assert (m.id, m.switch_from) == ("mine-only", None)                                   # no audiobook edition: stays
        assert p.edition_problem("English", "Audio", want_audio=True) is None
        assert p.edition_problem("German", "Audio", want_audio=True)                             # language still applies
        c["goodreads"].update(enabled=True, cookie="a=1"); g = gr.Goodreads(c, LOG)
        cache = {"ROOT_QUERY": {'getBookByLegacyId({"legacyId":"9"})': {"__ref": "Book:1"}},
                 "Book:1": {"details": {"numPages": None, "format": "Audible Audio", "language": {"name": "English"}}}}
        with mock.patch("requests.request", return_value=Resp(200, "<script>%s</script>" % json.dumps(cache), url="u")):
            assert g.finalize({"id": "9", "want_audio": True}).id == "9"
    ub = {"id": 1, "book": {"title": "T"}, "edition": {"reading_format_id": 2}}
    assert book_from_ub(ub).audio is True and book_from_ub(dict(ub, edition={"reading_format_id": 1})).audio is False
    assert book_from_ub(dict(ub, edition=None)).audio is False


def test_hardcover_edition_cleanup_and_set_edition():
    from heliograph.hardcover import HardcoverClient, HardcoverError
    hc = HardcoverClient("T"); hc._uid = 42
    sent = []
    ub = {"id": 5, "user_id": 42, "book_id": 900, "edition_id": 111,
          "user_book_reads": [{"id": 70, "edition_id": 111, "started_at": "2024-01-02", "finished_at": "2024-02-03",
                               "progress_pages": 300, "progress_seconds": None},
                              {"id": 71, "edition_id": 999, "started_at": None, "finished_at": None, "progress_pages": 5, "progress_seconds": None}]}
    editions = {222: {"id": 222, "book_id": 900, "language": {"language": "English"}},
                333: {"id": 333, "book_id": 901, "language": {"language": "English"}},
                444: {"id": 444, "book_id": 900, "language": {"language": "German"}}}
    def post(url, json=None, **kw):
        q, v = json["query"], json["variables"]; sent.append((q.split("(")[0].split()[0], v))
        if "user_books(where: {user_id" in q:
            data = {"user_books": [{"id": 5, "book_id": 900, "book": {"title": "Glas"},
                                    "edition": {"id": 111, "title": "Glas", "reading_format_id": 1, "edition_format": "Paperback",
                                                "language": {"language": "German"}}},
                                   {"id": 6, "book_id": 901, "book": {"title": "Fine"},
                                    "edition": {"id": 5, "language": {"language": "English"}}}]}
        elif "editions(where: {book_id" in q:
            data = {"editions": [{"id": 223, "reading_format_id": 4, "users_count": 900},
                                 {"id": 222, "reading_format_id": 1, "users_count": 500}]}
        elif q.startswith("query($id: Int!, $e: Int!)"):
            data = {"user_books": [ub], "editions": [editions.get(v["e"])] if v["e"] in editions else []}
        elif "update_user_book_read" in q:
            data = {"update_user_book_read": {"id": v["id"], "error": None}}
        elif "update_user_book" in q:
            data = {"update_user_book": {"id": v["id"], "error": "nope" if v["e"] == 666 else None}}
        else:
            data = {"user_books": []}
        return Resp(200, {"data": data})
    with mock.patch("requests.post", side_effect=post):
        books = hc.edition_cleanup("English")
        assert [b["id"] for b in books] == [5] and books[0]["suggested"] == 222      # same format beats more-used ebook
        assert books[0]["current"]["language"] == "German" and len(books[0]["candidates"]) == 2
        for bad, msg in ((333, "different book"), (444, "German")):
            sent.clear()
            try: hc.set_edition(5, bad, "English"); assert False
            except HardcoverError as e: assert msg in str(e)
            assert not any("update" in s[0] or "mutation" in s[0] for s in sent)   # nothing written
        sent.clear()
        assert hc.set_edition(5, 222, "English") == {"old": 111, "new": 222, "reads_moved": 1}
        muts = [v for q, v in sent if q == "mutation"]
        assert muts[0] == {"id": 5, "e": 222}
        assert muts[1] == {"id": 70, "o": {"started_at": "2024-01-02", "finished_at": "2024-02-03", "progress_pages": 300,
                                           "progress_seconds": None, "edition_id": 222}}      # dates kept, other read untouched
        assert len(muts) == 2
        editions[666] = {"id": 666, "book_id": 900, "language": {"language": "English"}}
        try: hc.set_edition(5, 666, "English"); assert False
        except HardcoverError as e: assert "refused" in str(e)
        ub["user_id"] = 7
        try: hc.set_edition(5, 222); assert False
        except HardcoverError as e: assert "isn't in your" in str(e)


def test_state_keeps_audio_and_upgrades_old_databases():
    import sqlite3
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, "s.db")
        db = sqlite3.connect(path)                                   # a 0.2.0 database: no audio column
        db.execute("""create table books(ub_id integer primary key, title text, authors text, status text, pages integer,
            total_pages integer, rating real, isbn13 text, isbn10 text, book_id integer, edition_id integer, finished_on text, seen_at real)""")
        db.execute("insert into books values(7,'Old','[]','read',null,null,null,null,null,null,null,null,0)"); db.commit(); db.close()
        s = State(path)
        assert s.get_book(7).audio is False and s.get_book(7).title == "Old"
        s.upsert_book(Book(8, "Heard", audio=True), 1)
        assert s.get_book(8).audio is True and [b.ub_id for b in s.books()] == [7, 8]


def test_edition_problem_rules():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        p = sg.StoryGraph(cfg(tmp), LOG)
        assert p.edition_problem("English", "Paperback") is None
        assert p.edition_problem("Not specified", "Hardcover") is None and p.edition_problem("", "") is None   # unknown passes
        assert p.edition_problem("english", "Kindle Edition") is None
        assert "German" in p.edition_problem("German", "Paperback")
        assert "audiobook" in p.edition_problem("English", "Audible Audio") and "audiobook" in p.edition_problem("English", "Audio CD")


def test_goodreads_and_fable_refuse_wrong_editions():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="a=1")
        g = gr.Goodreads(c, LOG)
        def apollo(lang, fmt):
            cache = {"ROOT_QUERY": {'getBookByLegacyId({"legacyId":"77"})': {"__ref": "Book:1"}},
                     "Book:1": {"details": {"numPages": 412, "format": fmt, "language": {"name": lang}}}}
            return Resp(200, "<script>%s</script>" % json.dumps(cache), url="https://www.goodreads.com/book/show/77")
        with mock.patch("requests.request", return_value=apollo("English", "Paperback")):
            assert g.finalize({"id": "77"}).pages == 412
        g._page_cache.clear()
        with mock.patch("requests.request", return_value=apollo("English", "Audible Audio")):
            assert g.finalize({"id": "77"}) is None
        g._page_cache.clear()
        with mock.patch("requests.request", return_value=apollo("Spanish", "Paperback")):
            assert g.finalize({"id": "77"}) is None
            assert g.check_edition("77") == ("Spanish", "Paperback")
        c["fable"].update(enabled=True, id_token="I", refresh_token="R", token_expires_at=time.time() + 3000, user_id="U")
        f = fb.Fable(c, LOG)
        def eds(lang, fmt):
            return Resp(200, {"results": [{"is_current_book": True, "page_count": 300, "display_isbn": "1",
                                            "language": {"name": lang}, "format": {"name": fmt}}]})
        with mock.patch("requests.request", return_value=eds("English", "Hardcover")):
            assert f.finalize({"id": "B"}).pages == 300
        with mock.patch("requests.request", return_value=eds("Ukrainian", "Paperback")):
            assert f.finalize({"id": "B"}) is None and f.check_edition("B") == ("Ukrainian", "Paperback")


def test_storygraph_writes():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
        calls.clear()                                              # already reviewed: the review is updated in place
        def reviewed(method, url, **kw):
            if url.endswith("/reviews/B2") and method == "GET":
                return Resp(200, {"id": "R9", "rating": "4.00", "review": "Good", "labels": ["x"]})
            return fake(method, url, **kw)
        with mock.patch("requests.request", side_effect=reviewed):
            p.set_rating("B2", 5, b)
        assert [(m, u) for m, u, _ in calls if m != "GET"] == [("PATCH", "/api/books/B2/reviews/R9")]
        assert calls[-1][2]["rating"] == 5.0 and calls[-1][2]["review"] == "Good" and calls[-1][2]["labels"] == ["x"]
        uid = "78D23960-2135-4233-b11c-a7811bb77ade"                  # linking by hand: an id, or a book link
        assert p.parse_ref(uid) == uid.lower() and p.parse_ref("https://fable.co/x/" + uid) == uid.lower()
        assert p.parse_ref("12345") is None and p.parse_ref("https://fable.co/book/white-sand") is None
        def by_isbn(method, url, **kw):
            if "/api/books/search/" in url and "9781524110062" in url:
                return Resp(200, {"response": {"books": [{"id": "B9", "isbn": "9781524110062"}]}})
            return fake(method, url, **kw)
        with mock.patch("requests.request", side_effect=by_isbn):
            assert p.parse_ref("https://fable.co/book/white-sand-volume-3-by-brandon-sanderson-9781524110062") == "B9"
        with mock.patch("requests.request", side_effect=reviewed):
            assert p.read_state("B2")["rating"] == 4.0                 # Fable's rating is a string
            assert p.read_state("B1")["rating"] is None                # not reviewed


def test_pagebound_flow():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
        undated = pb.status_payload("read", None)                          # no dates in Hardcover: none sent
        assert "finished_reading_at" not in undated and "started_reading_at" not in undated
        assert "started_reading_at" in pb.status_payload("reading", None)   # starting a book is dated today
        calls.clear()                              # already rated: the review is changed in place, never posted again
        def rated(method, url, **kw):
            path = url.replace(pb.API, "")
            if path.startswith("/api/v1/user_books?page=1&q="):
                calls.append((method, path, None))
                return Resp(200, {"user_books": [{"book_id": 5, "review": {"id": 77, "overall_rating": "3.0"}}], "total_pages": 1})
            return fake(method, url, **kw)
        with mock.patch("requests.request", side_effect=rated):
            p.set_rating("5:UU", 4.5, b)
            p.set_rating("5:UU", 3, b)                                              # same as Pagebound has: nothing sent
        writes = [(m, u, j) for m, u, j in calls if m != "GET"]
        assert writes == [("PUT", "/api/v1/reviews/77", {"review": {"overall_rating": 4.5}})]


def test_pagebound_search_candidates():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["pagebound"].update(enabled=True)
        p = pb.Pagebound(c, LOG)
        hit = {"hits": [{"document": {"id": 5, "uuid": "UU", "title": "The Hobbit", "author_name": "J.R.R. Tolkien", "page_count": 310}}]}
        with mock.patch("requests.get", return_value=Resp(200, hit)):
            m = p.find(Book(1, "The Hobbit", authors=["J.R.R. Tolkien"], isbn13="978"), True, True)
        assert (m.id, m.pages) == ("5:UU", 310)           # ISBN is skipped (no ISBN lookup), title matched by score
        with mock.patch("requests.get", return_value=Resp(200, hit)):
            assert p.find(Book(1, "Totally Different", authors=["Someone"]), True, True) is None
        sent = []
        def rec(url, **kw):
            sent.append(kw["params"]); return Resp(200, hit)
        with mock.patch("requests.get", side_effect=rec):
            p.search_book("The Hobbit", ["J.R.R. Tolkien", "A `Quote"])
        assert sent[0]["q"] == "The Hobbit" and sent[0]["query_by"] == "title"       # author kept out of the query...
        assert sent[0]["filter_by"] == "author_name:=[`J.R.R. Tolkien`,`A Quote`]"   # ...and used as a filter instead
        assert "filter_by" not in sent[1] and sent[1]["q"] == "The Hobbit"            # then the title alone


def test_pagebound_link_from_url():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["pagebound"].update(enabled=True, id_token="I", refresh_token="R", api_token="A", token_expires_at=time.time() + 3000)
        p = pb.Pagebound(c, LOG)
        u = "8804d280-1d5a-4f53-8f31-d46b4d4d81b2"
        assert p.parse_ref(f"https://pagebound.co/books/{u}") == ":" + u
        assert p.parse_ref(f"https://pagebound.co/books/{u.upper()}?ref=x") == ":" + u
        assert p.parse_ref(u) == ":" + u and p.parse_ref("5:" + u) == "5:" + u
        assert p.parse_ref("https://pagebound.co/") is None and p.parse_ref("hello") is None
        seen = []
        def fake(method, url, **kw):
            seen.append(url.replace(pb.API, ""))
            return Resp(200, {"book": {"id": 4242, "uuid": u, "page_count": 310, "user_book": None}})
        with mock.patch("requests.request", side_effect=fake):
            m = p.finalize({"id": ":" + u})
        assert (m.id, m.pages) == (f"4242:{u}", 310) and seen == [f"/api/v1/books/{u}"]
        assert p.finalize({"id": "5:UU", "pages": 9}).id == "5:UU"          # picker candidates need no lookup
        assert p.book_url(f"4242:{u}") == f"https://pagebound.co/books/{u}"


# ------------------------------------------------------------------ config
def test_env_overrides():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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


def test_goodreads_favorites_shelf_that_does_not_exist_yet():
    """Goodreads shows your whole library for a shelf that doesn't exist: that isn't "already on it"."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c = cfg(tmp); c["goodreads"].update(enabled=True, cookie="a=1")
        g = gr.Goodreads(c, LOG)
        g.profile_url = lambda: "https://www.goodreads.com/user/show/1"
        row = ('<tr class="bookalike review"><td class="field title"><a href="/book/show/42">Dune</a></td></tr>')
        missing = Resp(200, "<html>" + row + "</html>", url="x")
        real = Resp(200, '<a title="x" class="selectedShelf" href="/review/list/1-x?tag=favorites&amp;view=table">'
                    + row + "</a>", url="x")
        g._req = lambda *a, **k: missing
        assert g._on_shelf("favorites", "42") is False
        g._req = lambda *a, **k: real
        assert g._on_shelf("favorites", "42") is True and g._on_shelf("favorites", "7") is False


ALL = [test_goodreads_favorites_shelf_that_does_not_exist_yet, test_matching, test_page_mapping, test_goodreads_helpers, test_goodreads_refresher_on_waf,
       test_goodreads_refresher_accounts, test_profile_links, test_goodreads_cools_down_after_uncleared_waf, test_goodreads_pacing_and_reuse,
       test_goodreads_bootstraps_empty_cookie_from_refresher, test_goodreads_auth_failure_without_refresher,
       test_goodreads_writes_and_headers, test_goodreads_date_finished_flow, test_goodreads_finished_sessions_logic,
       test_storygraph_search_and_status_parsing, test_storygraph_edition_redirect_and_audio_refusal,
       test_storygraph_picks_the_right_edition, test_edition_problem_rules, test_goodreads_and_fable_refuse_wrong_editions,
       test_audiobook_books_get_audiobook_editions, test_state_keeps_audio_and_upgrades_old_databases,
       test_hardcover_edition_cleanup_and_set_edition,
       test_storygraph_writes, test_storygraph_form_roundtrip, test_fable_flow, test_pagebound_flow,
       test_pagebound_search_candidates, test_pagebound_link_from_url, test_env_overrides]

if __name__ == "__main__":
    for t in ALL:
        try:
            t()
        except Exception:
            print("FAILED:", t.__name__); raise
    print("provider tests passed")
