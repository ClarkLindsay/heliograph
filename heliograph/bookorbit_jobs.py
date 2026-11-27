"""The BookOrbit integration: getting a BookOrbit library's Hardcover links right (the BookOrbit tool), the
6-hourly ebook switch, copying Hardcover ratings and reviews into BookOrbit, and marking your BookOrbit books as
owned on the services that can (Goodreads, StoryGraph, Pagebound).

Optional: nothing here runs unless BookOrbit is set up in Settings. The engine inherits these methods; they use its
state, config, log, Hardcover client and matcher. The only thing they send to your services is "owned".
"""
import json, threading, time

from . import bookorbit, smartmatch
from .hardcover import HardcoverDailyLimit
from .providers.base import AuthError

OWNED_EVERY = 6 * 3600           # with "mark as owned" on, BookOrbit books added since are marked this often


def review_norm(text):
    from .engine import review_norm as norm         # shared with review syncing (imported late: no import cycle)
    return norm(text)


class BookOrbitJobs:
    def _bo_init(self):
        self.bo_job = {"running": False}
        self.own_job = {"running": False}
        self._bo_found = {}                              # BookOrbit book key -> the last Hardcover search you ran for it
        try:                                             # the last scan's list survives a restart
            saved = json.loads(self.state.get("bookorbit_job") or "null")
            if isinstance(saved, dict) and saved.get("items") is not None:
                self.bo_job = dict(saved, running=False)
        except (TypeError, ValueError, AttributeError):
            pass

    # ---- your BookOrbit books are copies you own: say so on every service that can
    def _owned_done(self):
        try:
            return set(json.loads(self.state.get("owned_marked") or "[]"))     # "service|remote id" already marked
        except ValueError:
            return set()

    def start_bookorbit_owned(self):
        if self.own_job.get("running"):
            raise ValueError("Already marking your BookOrbit books as owned")
        client = self.bookorbit()
        self.own_job = {"running": True, "started": time.time(), "finished": 0, "total": 0, "done": 0, "marked": 0,
                        "already": 0, "failed": 0, "not_linked": 0, "not_in_hardcover": 0, "errors": [],
                        "services": [], "message": "Reading your BookOrbit and Hardcover libraries..."}
        threading.Thread(target=self._bo_owned, args=(client,), daemon=True, name="bookorbit-owned").start()
        return self.own_job

    def bookorbit_owned_auto(self):
        """With the setting on: every OWNED_EVERY, mark the BookOrbit books added since (the rest are remembered)."""
        c = self.config["bookorbit"]
        if not (c.get("url") and c.get("api_key") and c.get("mark_owned")) or self.own_job.get("running"):
            return
        if time.time() - float(self.state.get("owned_checked_at") or 0) < OWNED_EVERY:
            return
        self.state.set("owned_checked_at", str(time.time()))
        self.start_bookorbit_owned()

    def _bo_owned(self, client):
        job, done = self.own_job, self._owned_done()
        try:
            by = {}
            for b in self.hc().fetch_library():
                for k in (b.slug, b.book_id):
                    if k:
                        by[str(k)] = b.ub_id
            ubs, missing = {}, 0
            for bo in client.books():
                ub = by.get(str(bo.get("hardcoverId") or ""))
                if ub:
                    ubs[ub] = bo.get("title") or ""
                else:
                    missing += 1                     # not in your Hardcover library, so not on your services either
            job["not_in_hardcover"] = missing
            provs = [p for p in self.providers if p.enabled and getattr(p, "supports_owned", False)]
            job["services"] = [p.label for p in provs]
            work = []
            for ub, title in ubs.items():
                for p in provs:
                    l = self.state.link(ub, p.key)
                    if l["skip"]:
                        continue
                    if not l["remote_id"]:
                        job["not_linked"] += 1
                    elif f"{p.key}|{l['remote_id']}" in done:
                        job["already"] += 1
                    else:
                        work.append((ub, title, p, l["remote_id"]))
            job["total"], dead = len(work), set()
            job["message"] = f"Marking {len(work)} book(s) as owned..." if work else "Nothing new to mark"
            for ub, title, p, rid in work:
                if p.key in dead:
                    job["done"] += 1
                    continue
                b = self.state.get_book(ub)
                try:
                    with self.busy(p.key, timeout=120, why="marking your BookOrbit books as owned"):
                        changed = p.set_owned(rid, b)
                    if changed:
                        job["marked"] += 1
                        self.log.info("%s: '%s' marked as owned (it's in your BookOrbit library)", p.label, title)
                    else:
                        job["already"] += 1
                    done.add(f"{p.key}|{rid}")
                    self.state.set("owned_marked", json.dumps(sorted(done)))
                except AuthError as e:
                    dead.add(p.key)
                    job["failed"] += 1
                    job["errors"].append({"title": title, "service": p.label, "error": str(e)[:200]})
                    self.log.error("%s: %s - stopped marking books as owned there", p.label, e)
                except Exception as e:
                    job["failed"] += 1
                    job["errors"].append({"title": title, "service": p.label, "error": str(e)[:200]})
                    self.log.warning("%s: couldn't mark '%s' as owned: %s", p.label, title, e)
                job["done"] += 1
            job["message"] = (f"Done: {job['marked']} marked as owned, {job['already']} already were"
                              + (f", {job['failed']} failed" if job["failed"] else ""))
            if job["marked"] or job["failed"]:
                self.log.info("Mark as owned: %s", job["message"])
        except Exception as e:
            job["message"] = f"Stopped: {e}"
            self.log.error("Marking BookOrbit books as owned failed: %s", e)
        finally:
            job.update(running=False, finished=time.time())

    # ---- BookOrbit: right Hardcover book + edition for every book, English, language normalised
    def bookorbit(self):
        c = self.config["bookorbit"]
        return bookorbit.BookOrbitClient(c.get("url"), c.get("api_key"))

    def _bo_seen(self):
        try:
            return json.loads(self.state.get("bookorbit_seen") or "{}")
        except (TypeError, ValueError):
            return {}

    def start_bookorbit_scan(self, full=False):
        if self.bo_job.get("running"):
            raise ValueError("A BookOrbit scan or update is already running")
        client = self.bookorbit()
        self.bo_job = {"running": True, "phase": "scan", "items": [], "done": 0, "total": 0, "skipped": 0,
                       "started": time.time(), "finished": 0, "message": "Reading your BookOrbit library…"}
        threading.Thread(target=self._bo_scan, args=(client, bool(full)), daemon=True, name="bookorbit").start()
        return self.bo_job

    def _bo_scan(self, client, full):
        job, seen = self.bo_job, None
        try:
            books = client.books()
            job["total"] = len(books)
            look, seen, items = bookorbit.HardcoverLookup(self.hc()), self._bo_seen(), []
            job["items"] = items                         # the page sees the list grow as the scan goes
            library = self._bo_library()
            aside = self._bo_aside()

            def right(b):                                # already right: listed under Done, from BookOrbit's own data
                hid, eid = b.get("hardcoverId"), b.get("hardcoverEditionId")
                items.append({"id": b["id"], "key": str(b["id"]), "title": b.get("title") or "", "authors": b.get("authors") or [],
                              "kind": bookorbit.kind(b), "changes": {}, "problem": None, "fix": "right", "pick": False,
                              "result": "✓ already right", "why": [f"Hardcover “{hid}”" + (f", edition {eid}" if eid else "")],
                              "book": {"id": hid, "slug": hid, "title": ""}, "edition": {"id": eid} if eid else None,
                              "isbn13": b.get("isbn13"), "isbn10": b.get("isbn10"),
                              "source": {k: b.get(k) for k in ("files", "hardcoverId", "hardcoverEditionId", "language")}})

            for b in books:
                sig = bookorbit.signature(b)
                if str(b["id"]) in aside:                # you set it aside (not on Hardcover): listed, never looked up
                    items.append({"id": b["id"], "key": str(b["id"]), "title": b.get("title") or "", "authors": b.get("authors") or [],
                                  "kind": bookorbit.kind(b), "changes": {}, "why": [], "problem": None, "fix": "aside",
                                  "result": "", "pick": False, "isbn13": b.get("isbn13"), "isbn10": b.get("isbn10"),
                                  "source": {k: b.get(k) for k in ("files", "hardcoverId", "hardcoverEditionId", "language")}})
                    job["items"] = items
                elif not full and seen.get(str(b["id"])) == sig and not self._bo_reads_differ(b, library):
                    job["skipped"] += 1
                    right(b)
                else:
                    job["message"] = f"Checking '{b.get('title')}' on Hardcover…"
                    try:
                        p = bookorbit.plan_book(b, look, self.matcher, library)
                    except HardcoverDailyLimit:
                        raise
                    except Exception as e:
                        p = {"id": b["id"], "title": b.get("title") or "", "authors": b.get("authors") or [], "changes": {},
                             "kind": bookorbit.kind(b), "isbn13": b.get("isbn13"), "isbn10": b.get("isbn10"),
                             "source": {k: b.get(k) for k in ("files", "hardcoverId", "hardcoverEditionId", "language")},
                             "why": [], "fix": "error", "problem": f"couldn't check: {str(e)[:150]}"}
                    if p["fix"] == "ok" and not p.get("problem"):
                        seen[str(b["id"])] = sig
                        right(b)
                    else:
                        p.update(key=str(b["id"]), result="", pick=bool(p["changes"]))
                        items.append(p)
                        job["items"] = items
                job["done"] += 1
            counts = {}
            for p in items:
                counts[p["fix"]] = counts.get(p["fix"], 0) + 1
            job["counts"] = counts
            work = [p for p in items if p["fix"] != "right"]
            fixable = sum(1 for p in work if p["changes"])
            aside_n = sum(1 for p in work if p["fix"] == "aside")
            look_n = len(work) - fixable - aside_n
            right_n = counts.get("right", 0)
            job["message"] = (f"{len(books)} books: {fixable} to fix" + (f", {look_n} to look at" if look_n else "")
                              + (f", {aside_n} set aside" if aside_n else "")
                              + (f", {right_n} already right" if right_n else "")) if work else \
                f"All {len(books)} books have the right Hardcover book, edition and language"
            self.log.info("BookOrbit: %s", job["message"])
        except HardcoverDailyLimit as e:
            # stop cleanly: what's checked is kept (and skipped next time), the rest waits for the reset
            job["message"] = (f"Paused after {job['done']} of {job['total']} books: {e}. Scan again then to carry on; "
                              "books already checked are skipped")
            self.log.warning("BookOrbit scan: %s", job["message"])
        except Exception as e:
            self.log.error("BookOrbit scan failed: %s", e)
            job["message"] = f"Failed: {e}"
        finally:
            if seen is not None:
                self.state.set("bookorbit_seen", json.dumps(seen))
            job.update(running=False, finished=time.time())
            self._bo_save()

    def apply_bookorbit(self, keys):
        job = self.bo_job
        if job.get("running"):
            raise ValueError("A BookOrbit scan or update is already running")
        keys = set(keys or ())
        todo = [p for p in job.get("items") or [] if p["key"] in keys and (p["changes"] or p.get("suggestion"))
                and not p["result"].startswith("✓")]
        if not todo:
            raise ValueError("Select at least one change")
        client = self.bookorbit()
        job.update(running=True, phase="apply", done=0, total=len(todo), message="Updating BookOrbit…")
        threading.Thread(target=self._bo_apply, args=(client, todo), daemon=True, name="bookorbit-apply").start()
        return job

    def _bo_apply(self, client, todo):
        job, ok = self.bo_job, 0
        seen = self._bo_seen()
        try:
            look = None
            for p in todo:
                try:
                    if p.get("suggestion") and "hardcoverId" not in p["changes"]:     # ticked with only a suggestion: use it
                        look = look or bookorbit.HardcoverLookup(self.hc())
                        s = p["suggestion"]
                        book = {"id": s["id"], "slug": s.get("slug"), "title": s["title"], "authors": s.get("authors") or []}
                        self._bo_link(p, book, self._bo_edition_for(p, look, book), "the suggestion", client)
                        seen = self._bo_seen()
                        ok += 1
                        job["done"] += 1
                        continue
                    client.update(p["id"], p["changes"])
                    p["result"] = "✓ updated"; ok += 1
                    self.log.info("BookOrbit: '%s' %s", p["title"], ", ".join(f"{k}={v}" for k, v in p["changes"].items()))
                    if not p.get("problem"):
                        seen[str(p["id"])] = bookorbit.signature(dict(p.get("source") or {}, **p["changes"]))
                except Exception as e:
                    p["result"] = f"error: {str(e)[:200]}"
                job["done"] += 1
            self.state.set("bookorbit_seen", json.dumps(seen))
            job["message"] = f"Done: {ok} book(s) updated in BookOrbit" + (f", {len(todo) - ok} failed" if ok < len(todo) else "")
        except Exception as e:
            job["message"] = f"Failed: {e}"
        finally:
            job.update(running=False, finished=time.time())
            self._bo_save()

    def _bo_save(self):
        """Keep the list (not the running flag) so a restart doesn't cost another scan."""
        try:
            self.state.set("bookorbit_job", json.dumps({k: v for k, v in self.bo_job.items() if k != "running"}, default=str))
        except Exception as e:
            self.log.debug("BookOrbit: couldn't save the list: %s", e)

    def _bo_library(self):
        """{Hardcover slug or book id: the edition in your Hardcover library}: a book you've read keeps that edition
        in BookOrbit. A few Hardcover requests (the library comes in pages of 100)."""
        lib = bookorbit.Library()
        for b in self.hc().fetch_library():
            if b.book_id:
                lib.owned.append({"id": b.book_id, "slug": b.slug, "title": b.title, "authors": list(b.authors)})
            if b.edition_id:
                if b.slug:
                    lib[b.slug] = b.edition_id
                if b.book_id:
                    lib[str(b.book_id)] = b.edition_id
        self._bo_lib = lib
        return lib

    def _bo_reads_differ(self, bo, library):
        """A book checked before whose BookOrbit edition isn't your Hardcover library's, or that your library has
        under another Hardcover record: check it again."""
        hid = str(bo.get("hardcoverId") or "")
        mine = library.get(hid) if hid else None
        if mine:
            return str(mine) != str(bo.get("hardcoverEditionId") or "") and \
                "hardcoverEditionId" not in (bo.get("lockedFields") or [])
        if not hid or "hardcoverId" in (bo.get("lockedFields") or []):
            return False
        found, _ = bookorbit.owned_match(library, bo.get("title") or "", list(bo.get("authors") or []), self.matcher,
                                         near=True)
        return bool(found) and hid not in (str(found["cand"]["id"]), str(found["cand"].get("slug") or ""))

    def _bo_aside(self):
        try:
            return json.loads(self.state.get("bookorbit_aside") or "{}")
        except (TypeError, ValueError):
            return {}

    def bookorbit_set_aside(self, key, aside=True):
        """A book that isn't on Hardcover (and won't be): scans list it under "Set aside" and never look it up.
        aside=False puts it back, to be checked on the next scan."""
        p = self._bo_item(key)
        saved = self._bo_aside()
        if aside:
            saved[str(p["id"])] = p["title"]
            p.update(fix_was=p.get("fix") if p.get("fix") != "aside" else p.get("fix_was"), fix="aside", changes={},
                     suggestion=None, problem=None, result="", why=[])
        else:
            saved.pop(str(p["id"]), None)
            seen = self._bo_seen()
            seen.pop(str(p["id"]), None)                 # make sure the next scan looks at it
            self.state.set("bookorbit_seen", json.dumps(seen))
            p.update(fix=p.get("fix_was") or "ok", problem="will be checked on the next scan", result="")
        self.state.set("bookorbit_aside", json.dumps(saved))
        self._bo_save()
        self.log.info("BookOrbit: '%s' %s", p["title"], "set aside (not on Hardcover)" if aside else "back in the checks")
        return p

    def _bo_item(self, key):
        p = next((x for x in self.bo_job.get("items") or [] if x["key"] == key), None)
        if not p:
            raise KeyError("that book isn't in the BookOrbit list (scan again)")
        if self.bo_job.get("running"):
            raise ValueError("Wait for the BookOrbit scan or update to finish")
        return p

    def _bo_link(self, p, book, ed, how, client=None):
        """Write the Hardcover book + edition you chose (and the language fix, if any) to BookOrbit."""
        changes = {"hardcoverId": book.get("slug") or str(book["id"]), "hardcoverEditionId": str(ed["id"])}
        if p["changes"].get("language"):
            changes["language"] = p["changes"]["language"]
        (client or self.bookorbit()).update(p["id"], changes)
        p.update(changes=changes, result="✓ updated", problem=None, suggestion=None, book=book, edition=ed,
                 why=[f"{how}: “{book['title']}”, {ed.get('edition_format') or ed.get('format') or 'edition'} {ed['id']}"])
        seen = self._bo_seen()
        seen[str(p["id"])] = bookorbit.signature(dict(p.get("source") or {}, **changes))
        self.state.set("bookorbit_seen", json.dumps(seen))
        self._bo_save()
        self.log.info("BookOrbit: '%s' linked to Hardcover '%s' (%s), edition %s", p["title"], book["title"], how, ed["id"])
        return p

    def _bo_edition_for(self, p, look, book):
        lib = getattr(self, "_bo_lib", None)
        if lib is None:
            try:
                lib = self._bo_library()
            except Exception:
                lib = {}
        mine = bookorbit.library_edition(lib, book)
        if mine:
            return {"id": mine, "edition_format": "your Hardcover library's edition", "format": None}
        ed = bookorbit.pick_edition(look.editions(book["id"]), p["kind"], [i for i in (p.get("isbn13"), p.get("isbn10")) if i])
        if not ed:
            raise ValueError(f"Hardcover has no English edition of “{book['title']}”: paste the edition's URL to use it anyway")
        return ed

    def bookorbit_use_suggestion(self, key):
        """You accepted a suggested Hardcover book: work out its edition and write both."""
        p = self._bo_item(key)
        if not p.get("suggestion"):
            raise KeyError("no suggestion for that book")
        look = bookorbit.HardcoverLookup(self.hc())
        s = p["suggestion"]
        book = {"id": s["id"], "slug": s.get("slug"), "title": s["title"], "authors": s.get("authors") or []}
        return self._bo_link(p, book, self._bo_edition_for(p, look, book), "the suggestion")

    def bookorbit_search(self, key, query=None):
        """Hardcover's search results for one book in the list, each with the matcher's verdict (one Hardcover request)."""
        p = self._bo_item(key)
        look = bookorbit.HardcoverLookup(self.hc())
        query = (query or "").strip()
        cands = look.search(query, []) if query else look.search(smartmatch.clean_title(p["title"]), p["authors"])
        isbns = [i for i in (p.get("isbn13"), p.get("isbn10")) if i]
        for c in cands:
            c["isbn"] = next((i for i in isbns if i in c.get("isbns", ())), None)
        self._bo_found[key] = cands
        out = []
        for r in self.matcher.evaluate(p["title"], p["authors"], cands, isbns):
            c = r["cand"]
            out.append({"id": c["id"], "slug": c.get("slug"), "title": c["title"], "authors": c["authors"][:3],
                        "users": c.get("users"), "pages": c.get("pages"), "cover": c.get("cover"),
                        "url": f"https://hardcover.app/books/{c.get('slug') or c['id']}",
                        "match": {"ok": r["ok"], "blocked": r["blocked"], "confidence": round(r["confidence"], 2),
                                  "reasons": r["reasons"]}})
        return {"results": out}

    def bookorbit_link(self, key, ref):
        """You picked a search result, or pasted a Hardcover book / edition: link it (the edition picked for your
        files unless you pasted one)."""
        p = self._bo_item(key)
        what, ident = bookorbit.parse_ref(str(ref))
        look = bookorbit.HardcoverLookup(self.hc())
        if what == "edition":
            ed, book = look.edition(ident)
            if not ed:
                raise ValueError(f"Hardcover has no edition {ident}")
            return self._bo_link(p, book, ed, "your edition")
        book = look.book(ident)
        if not book:
            raise ValueError(f"Hardcover has no book “{ident}”")
        found = self._bo_found.get(key) or []
        idx = next((n for n, c in enumerate(found) if str(c["id"]) == str(book["id"]) or c.get("slug") == ident), None)
        if idx is not None:                              # chosen from the search: the matcher learns from it
            isbns = [i for i in (p.get("isbn13"), p.get("isbn10")) if i]
            try:
                if self.matcher.learn(p["title"], p["authors"], found, idx, isbns):
                    self.state.set("match_weights", json.dumps(self.matcher.weights))
                    self.state.set("match_learned", str(int(self.state.get("match_learned") or 0) + 1))
            except Exception as e:
                self.log.debug("matcher didn't learn from the BookOrbit pick: %s", e)
        return self._bo_link(p, book, self._bo_edition_for(p, look, book), "your choice")

    def _bo_sent(self):
        try:
            return json.loads(self.state.get("bookorbit_sent") or "{}")
        except (TypeError, ValueError):
            return {}

    def bookorbit_from_hardcover(self, books=None):
        """Hardcover leads: your star rating and review there are copied to the BookOrbit book linked to it.
        - Ratings: BookOrbit takes whole stars only, so a half/quarter star clears BookOrbit's rating rather than
          rounding it (BookOrbit's own Hardcover sync would send the rounded value back over yours). A rating is
          also skipped while BookOrbit's reading status disagrees with Hardcover's: a rating change makes BookOrbit
          re-sync the book, status included.
        - Reviews: into the Personal Review. A different note you wrote in BookOrbit isn't replaced unless
          "overwrite reviews" is on; deleting a review on Hardcover doesn't delete the note.
        books: the books this cycle looked at; the first run (or books=None) goes through your whole library."""
        c = self.config["bookorbit"]
        if self.dry or not (c.get("url") and c.get("api_key") and c.get("from_hardcover")):
            return 0
        sent = self._bo_sent()
        first = books is None or self.state.get("bookorbit_from_hc_done") != "1"
        # ratings held back (BookOrbit out of step with Hardcover) are retried hourly: you may have fixed BookOrbit
        retry = any(v.get("rating_skip") for v in sent.values()) and             time.time() - float(self.state.get("bookorbit_retry_at") or 0) > 3600
        if first or retry:
            books = self.hc().fetch_library()
            self.state.set("bookorbit_retry_at", str(time.time()))
        books = [b for b in books if b.rating or b.review or str(b.ub_id) in sent]
        if not books and not first:
            return 0
        client = self.bookorbit()
        try:                                                 # does a rating change make BookOrbit sync the book?
            hs = client.hardcover_settings()
            guarded = bool(hs.get("effectiveEnabled", hs.get("enabled", True)) and hs.get("autoSyncOnRatingChange", True))
        except Exception:
            guarded = True
        held = 0
        by_hc = {}
        for bo in client.books():
            if bo.get("hardcoverId"):
                by_hc.setdefault(str(bo["hardcoverId"]), []).append(bo)
        n = 0
        for b in books:
            matches = by_hc.get(b.slug or "") or by_hc.get(str(b.book_id or "")) or []
            mine = sent.setdefault(str(b.ub_id), {})
            for bo in matches:
                try:
                    n += self._bo_copy(client, b, bo, mine, guarded)
                    held += bool(mine.get("rating_skip"))
                except Exception as e:
                    self.log.warning("BookOrbit: couldn't update '%s': %s", bo.get("title") or b.title, e)
            if not mine:
                sent.pop(str(b.ub_id), None)
        self.state.set("bookorbit_sent", json.dumps(sent))
        if held and self.state.get("bookorbit_held_hint") != str(held):
            self.state.set("bookorbit_held_hint", str(held))
            self.log.info("BookOrbit: %d rating(s) held back because BookOrbit's status, dates or edition differ from "
                          "Hardcover's. Turning off \"sync on rating change\" in BookOrbit's Hardcover settings lets "
                          "Heliograph copy them all safely", held)
        if first:
            self.state.set("bookorbit_from_hc_done", "1")
            self.log.info("BookOrbit: ratings and reviews copied from Hardcover (%d change(s))", n)
        return n

    def _bo_copy(self, client, b, bo, mine, guarded=True):
        n, title, locked = 0, bo.get("title") or b.title, set(bo.get("lockedFields") or [])
        # rating
        whole = b.rating is not None and float(b.rating) == int(b.rating) and 1 <= b.rating <= 5
        want = int(b.rating) if whole else None
        if b.rating and not whole and mine.get("rating_note") != b.rating:
            self.log.info("BookOrbit: '%s' is rated %s on Hardcover; BookOrbit only takes whole stars, so its rating "
                          "is left blank", title, b.rating)
            mine["rating_note"] = b.rating
        have = bo.get("rating")
        clear_ok = b.rating or mine.get("rating") is not None        # only clear a rating Heliograph put there,
        if want != have and (want is not None or (clear_ok and have is not None)) and "rating" not in locked:   # or one Hardcover no longer has whole
            why = bookorbit.out_of_step(bo, b) if guarded else None
            if why:
                if mine.get("rating_skip") != why:          # said once, not every cycle
                    self.log.info("BookOrbit: '%s' rating not copied: %s. A rating change makes BookOrbit sync the book to "
                                  "Hardcover (status, dates and edition included)", title, why)
                    mine["rating_skip"] = why
            else:
                mine.pop("rating_skip", None)
                client.update(bo["id"], {"rating": want})
                bo["rating"] = want
                self.log.info("BookOrbit: '%s' rating %s", title, want if want is not None else "cleared")
                n += 1
        else:
            mine.pop("rating_skip", None)                    # nothing to copy (any more)
        mine["rating"] = want if bo.get("rating") == want else mine.get("rating")
        # review
        sig = self.review_sig(b)
        if sig and mine.get("review") not in (sig, "kept:" + sig):
            note = (client.book(bo["id"]) or {}).get("personalNote") or ""
            if note and review_norm(note) == review_norm(b.review):
                mine["review"] = sig
            elif note and not (mine.get("review") and not str(mine["review"]).startswith("kept:")) \
                    and not self.common.get("overwrite_reviews"):
                self.log.info("BookOrbit: '%s' has a different Personal Review that Heliograph didn't write: left alone "
                              "(Settings → overwrite reviews to replace it)", title)
                mine["review"] = "kept:" + sig
            else:
                client.set_note(bo["id"], b.review)
                mine["review"] = sig
                self.log.info("BookOrbit: '%s' review copied from Hardcover (%d characters)", title, len(b.review))
                n += 1
        return n

    def bookorbit_ebooks(self, force=False):
        """An audiobook you've since added an ebook to: switch its Hardcover edition to the ebook (if you allow it)."""
        c = self.config["bookorbit"]
        if not (c.get("url") and c.get("api_key") and c.get("auto_ebook")):
            return 0
        last = float(self.state.get("bookorbit_ebooks_at") or 0)
        if not force and time.time() - last < 6 * 3600:
            return 0
        self.state.set("bookorbit_ebooks_at", str(time.time()))
        seen, n = self._bo_seen(), 0
        try:
            client = self.bookorbit()
            look, lib = None, None
            for b in client.books():
                was = (seen.get(str(b["id"])) or "").split("|")[0]
                if was != "audio" or bookorbit.kind(b) != "ebook" or not b.get("hardcoverId"):
                    continue
                look = look or bookorbit.HardcoverLookup(self.hc())
                if lib is None:
                    lib = self._bo_library()
                p = bookorbit.plan_book(b, look, self.matcher, lib)
                edit = {k: v for k, v in p["changes"].items() if k in ("hardcoverEditionId", "language")}
                if "hardcoverEditionId" in edit and "hardcoverId" not in p["changes"]:
                    client.update(b["id"], edit)
                    n += 1
                    self.log.info("BookOrbit: '%s' now has an ebook: Hardcover edition switched to %s", b.get("title"), edit["hardcoverEditionId"])
                seen[str(b["id"])] = bookorbit.signature(dict(b, **edit))
            self.state.set("bookorbit_seen", json.dumps(seen))
        except Exception as e:
            self.log.warning("BookOrbit ebook check failed: %s", e)
        return n

