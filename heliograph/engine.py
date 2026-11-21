import csv, io, logging, threading, time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from .config import APP_DIR
from .hardcover import HardcoverClient
from .providers.base import AuthError

RELINK_AFTER = 6 * 3600          # retry an unmatched book every 6h
IMPORT_RETRY_DELAYS = (10, 30)   # a failed book/service is retried after these many seconds...
IMPORT_AUTH_DELAYS = (20, 60)    # ...longer for login/WAF/refresher problems
IMPORT_BREAKER = 5               # this many login failures in a row pause a service until the retry rounds
IMPORT_ROUND_WAIT = 60           # pause before each end-of-import retry round
IMPORT_ROUNDS = 2
MAX_CURSOR_HOLD = 7 * 86400      # after this long a failing book no longer pins the incremental window


class Busy(Exception):
    pass


class Engine:
    def __init__(self, config, providers, state, log=None, hardcover=None, dry_run=False, heartbeat=None):
        self.config, self.providers, self.state = config, providers, state
        self.log = log or logging.getLogger("heliograph")
        self._hc, self.dry, self.heartbeat = hardcover, dry_run, heartbeat
        self._lock = threading.Lock()
        self._problems = 0
        self.wake = threading.Event()
        self.status = {"running": False, "last_start": 0, "last_end": 0, "examined": 0, "pushed": 0,
                       "result": "not run yet", "next_run": 0}
        self.job = {"running": False}
        self.job_entries, self.job_results = [], []
        self.import_dir = APP_DIR / "imports"
        self._covers = {}
        for p in self.providers:
            p.state = self.state

    @property
    def common(self):
        return self.config["common"]

    def hc(self):
        if self._hc is None:
            self._hc = HardcoverClient(self.config["hardcover"]["token"])
        return self._hc

    def reset_hardcover(self):
        self._hc = None

    def provider(self, key):
        return next((p for p in self.providers if p.key == key), None)

    @contextmanager
    def busy(self, timeout=60):
        """Exclusive use of the providers (a sync cycle or a UI action), so their sessions aren't used concurrently."""
        if not self._lock.acquire(timeout=timeout):
            raise Busy("A sync cycle is running; try again in a moment")
        try:
            yield
        finally:
            self._lock.release()

    # ---- one poll cycle
    def run_once(self):
        if not self._lock.acquire(blocking=False):
            return 0
        self.status.update(running=True, last_start=time.time(), examined=0, pushed=0)
        try:
            n = self._cycle()
            self.status["result"] = "ok" if not self._problems else f"{self._problems} update(s) failed, will retry"
            return n
        except Exception as e:
            self.status["result"] = f"error: {e}"
            raise
        finally:
            self.status.update(running=False, last_end=time.time())
            self._lock.release()

    def _cycle(self):
        active = [p for p in self.providers if p.enabled]
        if not active:
            self.log.info("No target services enabled"); self.status["result"] = "no services enabled"; return 0
        hc, started = self.hc(), datetime.now(timezone.utc)
        # a service enabled for the first time gets its own baseline/import, whenever that happens
        firsts = {p.key for p in active if self.state.get(f"baselined:{p.key}") is None}
        last_poll = self.state.get("last_poll")
        if firsts:
            books = hc.fetch_library()
        else:
            seen = {b.ub_id: b for b in hc.fetch_updated_since(last_poll)}
            for b in hc.fetch_reading():          # progress edits may not bump user_books.updated_at
                seen[b.ub_id] = b
            books = list(seen.values())
        base = self.common["initial_sync"] == "baseline"
        self.log.info("Hardcover: %d book(s) to examine%s", len(books),
                      (f" (first run for {', '.join(sorted(firsts))}: "
                       + ("recording only, nothing pushed)" if base else "importing)")) if firsts else "")
        failed, n, self._problems = set(), 0, 0
        for b in books:
            if not self.dry:
                self.state.upsert_book(b, time.time())
            for p in active:
                if p.key not in failed:
                    n += self._sync(b, p, base and p.key in firsts, failed)
        self.status.update(examined=len(books), pushed=n)
        self.log.info("Cycle done: %d book(s) examined, %d update(s) pushed%s", len(books), n,
                      "" if n or firsts else " (nothing changed since the last sync; see Books in the web UI, or turn on "
                      "verbose logging to see each book's status and page)")
        clean = not failed and not self._problems
        if not self.dry:
            for p in active:
                if p.key in firsts and p.key not in failed:
                    self.state.set(f"baselined:{p.key}", "1")
            held_too_long = bool(last_poll) and (started - datetime.fromisoformat(last_poll)).total_seconds() > MAX_CURSOR_HOLD
            if clean or held_too_long or not last_poll:
                self.state.set("last_poll", (started - timedelta(minutes=10)).isoformat())
            else:
                self.log.warning("Some updates failed; keeping the Hardcover window open so they are retried")
            if self.heartbeat:
                self.heartbeat.write_text(str(time.time()))
        return n

    def _allowed(self, p, status):
        c = self.common
        if status == "want":
            return c["sync_want_to_read"]
        if status in ("paused", "dnf"):
            return c["sync_paused_dnf"] and p.supports(status)
        return status in ("reading", "read")

    def _progress_due(self, b, st, status_changed, now):
        if b.status != "reading" or b.pages is None or b.pages == st["pages"]:
            return False
        if b.pages == 0 and not st["pages"]:
            return False                         # nothing read yet
        if status_changed:
            return True
        c = self.common
        if now - st["pushed_at"] < c["min_update_interval_seconds"]:
            return False
        if st["pages"] is None:
            return True
        if c["progress_mode"] == "percent" and b.total_pages:
            return abs(b.pages - st["pages"]) * 100 / b.total_pages >= c["progress_step_percent"]
        return abs(b.pages - st["pages"]) >= c["progress_step_pages"]

    def _note(self, b, p, error, now, dry=None):
        if not (self.dry if dry is None else dry):
            self.state.save_link(b.ub_id, p.key, error=(str(error)[:300] if error else None), error_at=now if error else 0)

    def _sync(self, b, p, baseline, failed, dry=None, st=None, outcome=None, flag=False):
        dry = self.dry if dry is None else dry
        now, c = time.time(), self.common
        st = st if st is not None else self.state.link(b.ub_id, p.key)
        if baseline:
            if not dry:
                self.state.save_link(b.ub_id, p.key, status=b.status, pages=b.pages, rating=b.rating, pushed_at=now)
            return 0
        if st["skip"] or not b.status or not self._allowed(p, b.status):
            return 0
        status_changed = st["status"] != b.status
        progress_changed = p.tracks_progress and self._progress_due(b, st, status_changed, now)
        rating_changed = bool(c["sync_ratings"] and b.status == "read" and b.rating and b.rating != st["rating"])
        if not (status_changed or progress_changed or rating_changed):
            self.log.debug("%s: '%s' unchanged (Hardcover: %s p.%s/%s, last synced: %s p.%s)", p.label, b.title,
                           b.status, b.pages, b.total_pages or "?", st["status"], st["pages"])
            return 0
        try:
            hydrate = getattr(self.hc(), "hydrate", None)
            if hydrate:
                hydrate(b)                       # ISBNs + the user's edition page count
            rid, rpages = st["remote_id"], st["remote_pages"]
            if not rid:
                m = self._link(p, b, st, now, dry, outcome, flag)
                if not m:
                    return 0
                rid, rpages = m.id, m.pages
            tag = "[dry-run] would: " if dry else ""
            upd, done = {}, 0
            if status_changed:
                self.log.info("%s%s: '%s' -> %s", tag, p.label, b.title, b.status)
                if not dry:
                    p.set_status(rid, b.status, b)
                done += 1; upd["status"] = b.status
            if progress_changed:
                by_pages = c["progress_mode"] == "pages" or b.percent is None
                self.log.info("%s%s: '%s' progress %s", tag, p.label, b.title,
                              f"p.{b.pages}/{b.total_pages or '?'}" if by_pages else f"{b.percent}%")
                if not dry:
                    p.set_progress(rid, b, by_pages, rpages)
                done += 1; upd["pages"] = b.pages
            if rating_changed:
                self.log.info("%s%s: '%s' rated %s", tag, p.label, b.title, b.rating)
                if not dry:
                    p.set_rating(rid, b.rating, b)
                done += 1; upd["rating"] = b.rating
            if b.status == "read":
                upd["pages"] = b.pages
            if done:
                upd["pushed_at"] = now
            upd.update(error=None, error_at=0)
            if not dry:
                self.state.save_link(b.ub_id, p.key, **upd)
            return done
        except AuthError as e:
            self.log.error("%s: %s - skipping this service until next cycle", p.label, e)
            self._note(b, p, e, now, dry)
            failed.add(p.key)
            if outcome is not None:
                outcome.update(error=str(e)[:300], auth=True)
        except Exception as e:
            self._problems += 1
            if outcome is not None:
                outcome.update(error=str(e)[:300])
            self.log.error("%s: failed to sync '%s': %s", p.label, b.title, e, exc_info=self.common["verbose_logging"])
            self._note(b, p, e, now, dry)
        return 0

    def _link(self, p, b, st, now, dry=None, outcome=None, flag=False):
        """flag=True (imports/previews) records an unmatched book even in dry-run, so it shows up flagged in the Books tab."""
        dry = self.dry if dry is None else dry
        c = self.common
        if not (c["link_by_isbn"] or c["link_by_title"]) or now - st["link_failed_at"] < RELINK_AFTER:
            return None
        m = p.find(b, c["link_by_isbn"], c["link_by_title"])
        if m:
            self.log.info("%s: linked '%s' -> %s", p.label, b.title, m.id)
            if not dry:
                self.state.save_link(b.ub_id, p.key, remote_id=m.id, remote_pages=m.pages)
        else:
            if flag or not dry:
                self.state.save_link(b.ub_id, p.key, link_failed_at=now, error="no confident match found")
            if outcome is not None:
                outcome["nomatch"] = True
            self.log.warning("%s: no confident match for '%s' - will retry in 6h", p.label, b.title)
        return m

    # ---- actions used by the web UI (each holds the provider lock)
    def refresh_library(self):
        with self.busy():
            books = self.hc().fetch_library()
            now = time.time()
            for b in books:
                self.state.upsert_book(b, now)
            return len(books)

    def search_candidates(self, ub_id, key, query=None):
        b, p = self.state.get_book(ub_id), self.provider(key)
        if not b or not p:
            raise KeyError("unknown book or service")
        q = (query or " ".join([b.title] + b.authors)).strip()
        with self.busy():
            out = []
            for c in p.search(q)[:15]:
                out.append({"id": c["id"], "title": c.get("title") or "", "authors": c.get("authors") or [],
                            "pages": c.get("pages"), "cover": c.get("cover"), "url": p.book_url(c["id"]), "raw": c})
            return out

    def link_candidate(self, ub_id, key, cand=None, ref=None):
        b, p = self.state.get_book(ub_id), self.provider(key)
        if not b or not p:
            raise KeyError("unknown book or service")
        with self.busy():
            if ref is not None:
                rid = p.parse_ref(ref)
                if not rid:
                    raise ValueError(f"That doesn't look like a {p.label} book link or id")
                cand = {"id": rid, "title": b.title, "authors": b.authors, "pages": None}
            m = p.finalize(cand)
            if not m:
                raise ValueError(f"{p.label} can't use that edition (e.g. an audiobook edition)")
            self.state.save_link(ub_id, key, remote_id=m.id, remote_pages=m.pages, manual=1, skip=0,
                                 link_failed_at=0, error=None, error_at=0)
            self.log.info("%s: '%s' manually linked -> %s", p.label, b.title, m.id)
            return m

    def unlink(self, ub_id, key):
        self.state.save_link(ub_id, key, remote_id=None, remote_pages=None, manual=0, link_failed_at=0,
                             error=None, error_at=0)

    def set_skip(self, ub_id, key, skip):
        self.state.save_link(ub_id, key, skip=1 if skip else 0)

    def push_now(self, ub_id, key):
        """Send this book's current Hardcover status/progress/rating to one service, ignoring what was recorded."""
        p = self.provider(key)
        if not p:
            raise KeyError("unknown service")
        with self.busy():
            if self.state.link(ub_id, key)["skip"]:
                raise ValueError("This service is set to skip this book")
            b = self.hc().fetch_one(ub_id)
            if not b:
                raise KeyError("book not found on Hardcover")
            self.state.upsert_book(b, time.time())
            if not b.status or not self._allowed(p, b.status):
                raise ValueError(f"Nothing to send: '{b.status}' books aren't synced to {p.label} (see Settings)")
            self.state.save_link(ub_id, key, status=None, pages=None, rating=None, pushed_at=0, link_failed_at=0)
            self._problems = 0
            n = self._sync(b, p, False, set())
            st = self.state.link(ub_id, key)
            return {"pushed": n, "error": st["error"], "linked": bool(st["remote_id"])}

    def book_cover(self, ub_id):
        if ub_id not in self._covers:
            b = self.state.get_book(ub_id)
            self._covers[ub_id] = self.hc().cover_url(b) if b else None
        return self._covers[ub_id]

    def push_all(self, ub_id):
        """Push one book's current status/progress/rating to every enabled service."""
        results = []
        for p in [p for p in self.providers if p.enabled]:
            r = {"service": p.key, "label": p.label}
            try:
                out = self.push_now(ub_id, p.key)
                r.update(ok=not out["error"], pushed=out["pushed"], linked=out["linked"], error=out["error"])
            except Busy as e:
                r.update(ok=False, error=str(e))
            except (ValueError, KeyError) as e:
                r.update(ok=False, skipped=True, error=str(e.args[0]) if e.args else str(e))
            except Exception as e:
                r.update(ok=False, error=str(e)[:300])
            results.append(r)
        return results

    # ---- on-demand import of the whole Hardcover library
    def start_import(self, services=None, statuses=None, dry=False, skip_existing=False, only=None):
        """only: optional [(ub_id, service_key), ...] - used by 'retry failed' to redo just those pairs."""
        if self.job.get("running"):
            raise ValueError("An import is already running")
        only = {(int(u), k) for u, k in only} if only else None
        keys = {k for _, k in only} if only else services
        provs = [p for p in self.providers if p.enabled and (not keys or p.key in keys)]
        if not provs:
            raise ValueError("Select at least one enabled service")
        statuses = set(statuses or ("reading", "read", "want", "paused", "dnf"))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.job_entries, self.job_results = [], []
        self.job = {"running": True, "cancel": False, "dry": bool(dry), "done": 0, "total": 0, "pushed": 0,
                    "errors": 0, "nomatch": 0, "skipped": 0, "skip_existing": bool(skip_existing), "started": time.time(),
                    "finished": 0, "message": "Loading library…", "services": [p.label for p in provs],
                    "retry_only": bool(only), "failed_items": [], "unmatched": [],
                    "log_name": f"import-{stamp}{'-preview' if dry else ''}"}
        t = threading.Thread(target=self._import_worker, args=(provs, statuses, bool(dry), bool(skip_existing), only),
                             daemon=True, name="import")
        t.start()
        return self.job

    def retry_failed(self):
        if self.job.get("running"):
            raise ValueError("An import is already running")
        items = [(i["id"], i["service"]) for i in self.job.get("failed_items") or []]
        if not items:
            raise ValueError("Nothing to retry")
        return self.start_import(dry=self.job.get("dry", False), skip_existing=self.job.get("skip_existing", False), only=items)

    # --- the import's own log (also written to <data>/imports/)
    def _sleep(self, seconds):
        """Sleep, but wake early (returning False) if the import is cancelled."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.job.get("cancel"):
                return False
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))
        return not self.job.get("cancel")

    def _attach_log(self, job):
        """Capture everything the import thread logs, for the downloadable log."""
        engine = self
        try:
            self.import_dir.mkdir(parents=True, exist_ok=True)
            fh = open(self.import_dir / (job["log_name"] + ".log"), "a", encoding="utf-8")
        except OSError:
            fh = None
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

        class Capture(logging.Handler):
            def emit(self, rec):
                if rec.threadName != "import":
                    return
                line = fmt.format(rec).split("\n")[0]
                engine.job_entries.append(line)
                if len(engine.job_entries) > 50000:
                    del engine.job_entries[:5000]
                if fh:
                    try:
                        fh.write(line + "\n"); fh.flush()
                    except OSError:
                        pass
        h = Capture(level=logging.INFO)
        self.log.addHandler(h)
        return h, fh

    def _detach_log(self, h, fh):
        self.log.removeHandler(h)
        if fh:
            try: fh.close()
            except OSError: pass

    def _latest_log_file(self):
        try:
            files = sorted(self.import_dir.glob("import-*.log"), key=lambda f: f.stat().st_mtime)
        except OSError:
            return None
        return files[-1] if files else None

    def log_available(self):
        return bool(self.job_entries) or self._latest_log_file() is not None

    def export_log(self, fmt="txt"):
        """-> (bytes, filename, content-type)."""
        name = self.job.get("log_name") or "import"
        if fmt == "csv":
            if not self.job_results:
                raise KeyError("No import results to export yet")
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["time", "service", "book", "authors", "hardcover_id", "outcome", "detail"])
            for r in self.job_results:
                w.writerow([r["time"], r["service"], r["title"], r["authors"], r["id"], r["outcome"], r["detail"]])
            return out.getvalue().encode("utf-8-sig"), name + ".csv", "text/csv; charset=utf-8"
        if self.job_entries:
            text = "\n".join(self.job_entries) + "\n"
        else:
            f = self._latest_log_file()
            if not f:
                raise KeyError("No import log yet")
            text, name = f.read_text(encoding="utf-8", errors="replace"), f.stem
        return text.encode("utf-8"), name + ".txt", "text/plain; charset=utf-8"

    # --- one book on one service
    def _attempt(self, b, p, dry, skip_existing):
        """One try at syncing book b to provider p. Returns {pushed, skipped, error, auth, nomatch}."""
        out = {"pushed": 0, "skipped": False, "error": None, "auth": False, "nomatch": False}
        with self.busy(timeout=1800):
            st = dict(self.state.link(b.ub_id, p.key), status=None, pages=None, rating=None, pushed_at=0, link_failed_at=0)
            if skip_existing:
                try:
                    st = self._compare_with_remote(b, p, st, dry, out)
                except AuthError as e:
                    out.update(error=str(e), auth=True)
                    return out
                except Exception as e:
                    out["error"] = f"couldn't read it from the service: {e}"[:300]
                    return out
                if st is None:                                      # no match on that service
                    out["nomatch"] = True
                    return out
            before = dict(out)
            n = self._sync(b, p, False, set(), dry=dry, st=st, outcome=out, flag=True)
            out["pushed"] = n
            if out["nomatch"]:
                out["error"] = None
            if skip_existing and n == 0 and not out["error"] and not out["nomatch"]:
                out["skipped"] = True
        return out

    def _import_item(self, b, p, dry, skip_existing):
        """_attempt with backoff: transient failures (timeouts, a Goodreads WAF page, a refresher that is busy) get retried."""
        tries = 0
        while True:
            out = self._attempt(b, p, dry, skip_existing)
            if not out["error"] or self.job.get("cancel"):
                return out
            delays = IMPORT_AUTH_DELAYS if out["auth"] else IMPORT_RETRY_DELAYS
            if tries >= len(delays):
                return out
            wait = delays[tries]; tries += 1
            self.log.warning("%s: '%s' failed (%s) - retrying in %ds (%d/%d)", p.label, b.title, out["error"], wait, tries, len(delays))
            self.job["message"] = f"{p.label}: retrying '{b.title}' in {wait}s…"
            if not self._sleep(wait):
                return out

    def _compare_with_remote(self, b, p, st, dry, outcome=None):
        """Read what the service already holds for this book and shape the 'last pushed' snapshot to match it, so the
        normal sync logic only writes what differs. Returns None if the book can't be matched on that service."""
        if not st["remote_id"]:
            self.hydrate_book(b)
            m = self._link(p, b, st, time.time(), dry, outcome=outcome, flag=True)
            if not m:
                return None
            st = dict(st, remote_id=m.id, remote_pages=m.pages)
        remote = p.read_state(st["remote_id"])
        if remote is None:                                       # service can't be read back: write as usual
            return st
        out = dict(st, status=remote["status"])
        if remote["status"] != b.status:
            return out                                           # missing or on another shelf: write everything
        known = remote.get("known") or set()
        if b.status == "reading":
            out["pages"] = b.pages                               # on the right shelf: progress follows later updates
        elif b.status == "read":
            if b.rating:
                same = ("rating" not in known or remote.get("rating") is not None and
                        abs(remote["rating"] - p.normalize_rating(b.rating)) < 1e-6)
                out["rating"] = b.rating if same else None       # unreadable ratings are assumed right
            if b.finished_on and "finished_on" in known and b.finished_on[:10] not in remote.get("finished_dates", ()):
                out["status"] = None                             # date differs: re-send the status (it carries the date)
        return out

    def hydrate_book(self, b):
        hydrate = getattr(self.hc(), "hydrate", None)
        if hydrate:
            hydrate(b)

    def cancel_import(self):
        if self.job.get("running"):
            self.job["cancel"] = True
            self.job["message"] = "Cancelling after the current book…"

    def _import_worker(self, provs, statuses, dry, skip_existing=False, only=None):
        job = self.job
        h, fh = self._attach_log(job)
        failed, unmatched, tripped, streak, by_id = {}, {}, set(), {}, {}

        def publish():
            job["failed_items"] = list(failed.values())
            job["unmatched"] = list(unmatched.values())
            job["errors"], job["nomatch"] = len(failed), len(unmatched)

        def record(b, p, out):
            k = (b.ub_id, p.key)
            base = {"id": b.ub_id, "title": b.title, "authors": ", ".join(b.authors), "service": p.key, "label": p.label}
            job["pushed"] += out["pushed"]
            if out["error"]:
                kind, detail = "error", out["error"]
                failed[k] = dict(base, error=out["error"], auth=out["auth"])
            else:
                failed.pop(k, None)
                if out["nomatch"]:
                    kind, detail = "no match", "no confident match found - link it manually in the Books tab"
                    unmatched[k] = base
                else:
                    unmatched.pop(k, None)
                    if out["skipped"]:
                        kind, detail = "skipped", "already up to date"; job["skipped"] += 1
                    elif out["pushed"]:
                        kind, detail = ("would update" if dry else "updated"), f"{out['pushed']} change(s)"
                    else:
                        kind, detail = "unchanged", ""
            self.job_results.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "service": p.label, "title": b.title,
                                     "authors": base["authors"], "id": b.ub_id, "outcome": kind, "detail": detail})
            publish()

        try:
            with self.busy(timeout=600):
                books = self.hc().fetch_library()
            books = [b for b in books if b.status in statuses]
            if only:
                ids = {u for u, _ in only}
                books = [b for b in books if b.ub_id in ids]
            by_id = {b.ub_id: b for b in books}
            job["total"] = len(books)
            job["message"] = "Previewing…" if dry else "Importing…"
            self.log.info("%s started for %s: %d book(s)%s", "Retry" if only else "Import", ", ".join(job["services"]),
                          len(books), " (preview only)" if dry else "")
            for b in books:
                if job["cancel"]:
                    break
                with self.busy(timeout=1800):
                    self.state.upsert_book(b, time.time())      # so flagged books always show up in the Books tab
                for p in provs:
                    if only and (b.ub_id, p.key) not in only:
                        continue
                    if p.key in tripped:
                        failed[(b.ub_id, p.key)] = {"id": b.ub_id, "title": b.title, "authors": ", ".join(b.authors),
                                                    "service": p.key, "label": p.label, "auth": True,
                                                    "error": "not attempted: login kept failing"}
                        publish()
                        continue
                    st0 = self.state.link(b.ub_id, p.key)
                    if st0["skip"] or not self._allowed(p, b.status):
                        continue
                    job["message"] = f"{'Previewing' if dry else 'Importing'} '{b.title}' → {p.label}"
                    out = self._import_item(b, p, dry, skip_existing)
                    record(b, p, out)
                    if out["auth"] and out["error"]:
                        streak[p.key] = streak.get(p.key, 0) + 1
                        if streak[p.key] >= IMPORT_BREAKER and p.key not in tripped:
                            tripped.add(p.key)
                            self.log.error("%s: %d logins in a row failed - pausing it for now, will retry at the end", p.label, streak[p.key])
                    else:
                        streak[p.key] = 0
                job["done"] += 1
                time.sleep(0.2)
            # ---- retry rounds for whatever failed (after a pause, and only once the service answers a login check)
            rounds = 0
            while failed and not job["cancel"] and rounds < IMPORT_ROUNDS:
                rounds += 1
                self.log.info("%d item(s) failed - retry round %d/%d in %ds", len(failed), rounds, IMPORT_ROUNDS, IMPORT_ROUND_WAIT)
                job["message"] = f"Retry round {rounds}/{IMPORT_ROUNDS}: waiting {IMPORT_ROUND_WAIT}s…"
                if not self._sleep(IMPORT_ROUND_WAIT):
                    break
                for p in provs:
                    todo = [k for k in list(failed) if k[1] == p.key]
                    if not todo or job["cancel"]:
                        continue
                    try:
                        with self.busy(timeout=600):
                            p.check_auth()
                    except Exception as e:
                        self.log.warning("%s: still not ready (%s) - leaving %d item(s) for the next round", p.label, e, len(todo))
                        continue
                    tripped.discard(p.key)
                    for k in todo:
                        if job["cancel"]:
                            break
                        b = by_id.get(k[0])
                        if not b:
                            continue
                        job["message"] = f"Retry round {rounds}: '{b.title}' → {p.label}"
                        record(b, p, self._import_item(b, p, dry, skip_existing))
            if not dry and not job["cancel"]:
                for p in provs:
                    if not any(k[1] == p.key and v.get("auth") for k, v in failed.items()):
                        self.state.set(f"baselined:{p.key}", "1")
            left = len(failed)
            job["message"] = ("Cancelled" if job["cancel"] else "Preview finished" if dry else "Import finished") + \
                (f"; {left} item(s) still failing - see the list below" if left else "")
        except Exception as e:
            self.log.error("Import failed: %s", e)
            job["message"] = f"Failed: {e}"
        finally:
            job.update(running=False, finished=time.time())
            publish()
            self.log.info("Import ended: %s (%d/%d books, %d update(s), %d error(s), %d unmatched)", job["message"],
                          job["done"], job["total"], job["pushed"], job["errors"], job["nomatch"])
            self._detach_log(h, fh)

    # ---- loop
    def request_sync(self):
        self.wake.set()

    def loop(self, stop: threading.Event):
        while not stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                self.log.error("Sync cycle failed: %s", e)
            wait = max(1, int(self.common["poll_interval_minutes"])) * 60
            self.status["next_run"] = time.time() + wait
            end = time.monotonic() + wait
            while not stop.is_set() and not self.wake.is_set() and time.monotonic() < end:
                stop.wait(1)
            self.wake.clear()
