import base64, json, logging, os, sys, tempfile
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from heliograph.config import Config
from heliograph.hardcover import Book
from heliograph.providers import librarything as lt
from heliograph.providers.base import AuthError
from heliograph.state import State

LOG = logging.getLogger("t")
COOKIE = "cookie_userid=bob; cookie_usernum=42; cookie_userchecksum=abc"


class Resp:
    def __init__(self, status=200, body="", headers=None):
        self.status_code, self.text, self.headers = status, body if isinstance(body, str) else json.dumps(body), headers or {}
    def json(self): return json.loads(self.text)


def provider(tmp, **cfg):
    c = Config(os.path.join(tmp, "c.json"))
    c["librarything"].update(enabled=True, session_cookie=COOKIE, **cfg)
    p = lt.LibraryThing(c, LOG); p.state = State(os.path.join(tmp, "s.db"))
    return c, p


def test_helpers():
    assert lt.session_cookies("a=1; cookie_userid=bob; cookie_usernum=42; cookie_userchecksum=x; cookie_usernum2=9") == \
        {"cookie_userid": "bob", "cookie_usernum": "42", "cookie_userchecksum": "x"}
    assert lt.session_cookies("cookie_userid=deleted; cookie_usernum=1") == {"cookie_usernum": "1"}
    row = ('<tr><td><img src="https://covers.example/c.jpg"></td><td><p class="item"><a data-workid="700" href="/work/700">The Hobbit &amp; Co</a>'
           ' by <a href="/a/Tolkien">J.R.R. Tolkien</a></p> 1,234 members</td></tr>')
    assert lt.parse_search_results("<table>" + row + row + "</table>") == [
        {"id": "700", "title": "The Hobbit & Co", "authors": ["J.R.R. Tolkien"], "cover": "https://covers.example/c.jpg"}]
    popup = '<input type="checkbox" data-cid="7" checked="checked"> <span>Currently reading</span><input data-cid="1"><span>Your library</span>'
    cols = lt.parse_collections_popup(popup)
    assert [(c["id"], c["key"], c["checked"]) for c in cols] == [(7, "reading", True), (1, "library", False)]
    assert lt.status_for([{"id": 7, "key": "reading"}], [], None) == "reading"
    assert lt.status_for([{"id": 1, "key": "library"}], [{"finished": "2026-01-01"}], None) == "read"
    assert lt.status_for([{"id": 1, "key": "library"}], [], None) is None          # owned isn't "read"
    assert lt.status_for([{"id": 5, "key": None}], [], 5) == "read"
    assert lt.b64_decode(base64.b64encode("héllo".encode()).decode()) == "héllo"


def test_search_isbn_and_title():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c, p = provider(tmp)
        row = ('<tr><p class="item"><a data-workid="700">The Hobbit</a> by <a href="/a/T">J.R.R. Tolkien</a></p></tr>')
        body = json.dumps({"text": base64.b64encode(f"<table>{row}</table>".encode()).decode()})
        calls = []
        def fake(method, url, **kw):
            calls.append((method, url)); return Resp(200, body)
        with mock.patch("requests.request", side_effect=fake):
            m = p.find(Book(1, "The Hobbit", authors=["Tolkien, J.R.R."], isbn13="978"), True, True)
            assert m.id == "700" and "search=978" in calls[0][1]
            assert p._work("700")["title"] == "The Hobbit"
            assert p.find(Book(1, "Unrelated Thing", authors=["Someone"]), False, True) is None


def test_set_status_read_and_rating():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c, p = provider(tmp)
        p._remember("700", title="The Hobbit", author="Tolkien, J.R.R.")
        posts, mem = [], {"cols": {7}, "rows": {1: ("2026-01-01", "")}}
        def popup():
            names = {1: "Your library", 7: "Currently reading", 3: "To read"}
            return "".join(f'<input data-cid="{i}"{" checked=\"checked\"" if i in mem["cols"] else ""}> <span>{n}</span>' for i, n in names.items())
        def rows_html():
            return '<input name="edit_type">' + "".join(
                f'<input name="newdatestartedA[{i}]" value="{s}"><input name="newdateendedA[{i}]" value="{e}">' for i, (s, e) in mem["rows"].items())
        def fake(method, url, **kw):
            path = url.replace(lt.BASE, ""); form = dict(kw["data"]) if kw.get("data") else {}
            if path.startswith("/catalog_bottom.php"):
                assert "deepsearch=The+Hobbit" in path
                return Resp(200, '<a href="/work/700/book/9001">x</a><a href="/work/5/book/1">y</a>')
            if path.startswith("/ajax_lb_newwork_edit_bookfields.php") and "reading_dates" in path:
                return Resp(200, rows_html())
            if path.startswith("/ajax_collections_popup.php"):
                return Resp(200, popup())
            if path.startswith("/ajax_collectionsToggleBook.php"):
                cid, add = int(form["c_id"]), form["addRemove"] == 1
                (mem["cols"].add if add else mem["cols"].discard)(cid)
                return Resp(200, "".join(f'<a href="/x?collection={i}">n</a>' for i in mem["cols"]))
            if path.startswith("/ajax_newwork_edit_bookfields_submit.php"):
                posts.append(form); return Resp(200, "edit_field_reading_dates")
            if path.startswith("/ajax_setBookRating.php"):
                posts.append(form); return Resp(200, "")
            raise AssertionError(path)
        b = Book(1, "The Hobbit", finished_on="2026-03-04")
        with mock.patch("requests.request", side_effect=fake):
            p.set_status("700", "read", b)
            assert mem["cols"] == {1}                      # moved from "Currently reading" into "Your library"
            sub = [x for x in posts if "edit_type" in x][0]
            assert sub["newdatestartedA[1]"] == "2026-01-01" and sub["newdateendedA[1]"] == "2026-03-04" and sub["edit_book"] == "9001"
            p.set_rating("700", 4.75, b)
        assert posts[-1]["rating"] == 9 and posts[-1]["book"] == "9001"          # half stars, floored
        assert p._work("700")["copy"] == "9001"


def test_start_reading_adds_start_date_and_to_read():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c, p = provider(tmp, read_collection_id=5)
        assert p._read_collection_id() == 5 and p._add_collection_ids("read") == [5]
        assert p._add_collection_ids("reading") == [1, 7] and p._add_collection_ids("want") == [1, 3]


def test_relogin_on_expired_session():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c, p = provider(tmp, username="bob", password="pw")
        state = {"n": 0}
        def fake(method, url, **kw):
            state["n"] += 1
            if state["n"] == 1:
                return Resp(302, "", {"location": "https://www.librarything.com/signin.php"})
            return Resp(200, '<input type="checkbox" name="books_collections[]" value="3"> <span>To read</span>')
        login = Resp(200, {"data": {"cookie_userid": "bob", "cookie_usernum": "42", "cookie_userchecksum": "NEW"}})
        with mock.patch("requests.request", side_effect=fake), mock.patch("requests.get", return_value=login) as g:
            assert "OK" in p.check_auth()
        assert "cookie_userchecksum=NEW" in c["librarything"]["session_cookie"] and g.call_count == 1


def test_expired_without_password_and_cloudflare():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c, p = provider(tmp)
        with mock.patch("requests.request", return_value=Resp(401)):
            try: p.check_auth(); assert False
            except AuthError: pass
        with mock.patch("requests.request", return_value=Resp(403, "Just a moment...", {"cf-mitigated": "challenge"})):
            try: p.check_auth(); assert False
            except lt.ProviderError as e: assert "Cloudflare" in str(e)


if __name__ == "__main__":
    for t in (test_helpers, test_search_isbn_and_title, test_set_status_read_and_rating,
              test_start_reading_adds_start_date_and_to_read, test_relogin_on_expired_session, test_expired_without_password_and_cloudflare):
        try: t()
        except Exception: print("FAILED", t.__name__); raise
    print("librarything tests passed")
