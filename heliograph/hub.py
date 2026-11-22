"""Several users on one Heliograph: each user has their own folder under <data>/users/<name>/ with their own
config.json (settings and secrets), state.db, import logs and log file, and their own sync loop.

Before 0.4 there was one user, with config.json and state.db straight in <data>. On first start those are moved to
users/.owner/ (a copy is kept in backup-before-0.4/) and keep syncing there until the first account is created; that
account becomes the admin and takes the data over."""
import logging, shutil, sys, threading, time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .accounts import Accounts, AccountError, clean_name
from .config import APP_DIR, Config, HEARTBEAT_PATH
from .crypto import verify_password
from .engine import Engine
from .providers import build
from .state import State

OWNER = ".owner"            # the pre-0.4 data, waiting for the first account
LEGACY_FILES = ("config.json", "state.db", "imports")
FMT = "%(asctime)s %(levelname)s %(message)s"


class Runtime:
    """One user's settings, database, log and engine (and the thread running its sync loop)."""
    def __init__(self, name, folder, config, state, engine, log, log_path):
        self.name, self.dir, self.config, self.state, self.engine, self.log = name, folder, config, state, engine, log
        self.log_path, self.stop, self.thread = log_path, threading.Event(), None


def user_logger(name, folder):
    log = logging.getLogger(f"heliograph.user.{name}")
    log.propagate = False
    log.handlers[:] = []
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)s [{name}] %(message)s"))
    path = Path(folder) / "heliograph.log"
    f = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    f.setFormatter(logging.Formatter(FMT))
    log.addHandler(out); log.addHandler(f)
    return log, path


def default_factory(name, folder, admin, dry_run=False):
    folder.mkdir(parents=True, exist_ok=True)
    log, path = user_logger(name, folder)
    cfg = Config(folder / "config.json", env=admin)
    if not (folder / "config.json").exists():
        cfg.save()
    elif cfg.encryption_enabled and cfg._plaintext_on_disk:
        cfg.save(); log.info("Encrypted the secrets stored in config.json")
    log.setLevel(logging.DEBUG if cfg["common"]["verbose_logging"] else logging.INFO)
    eng = Engine(cfg, build(cfg, log), State(folder / "state.db"), log, dry_run=dry_run, data_dir=folder)
    return Runtime(name, folder, cfg, eng.state, eng, log, path)


class Hub:
    def __init__(self, server_config, log, data_dir=APP_DIR, factory=default_factory, heartbeat=HEARTBEAT_PATH):
        self.server, self.log, self.data_dir = server_config, log, Path(data_dir)
        self.users_dir = self.data_dir / "users"
        self.accounts = Accounts(self.data_dir / "users.json")
        self.factory, self.heartbeat = factory, heartbeat
        self.runtimes, self.lock = {}, threading.RLock()
        self._stop, self._loops = threading.Event(), True

    # ---- start-up
    def migrate_legacy(self):
        """Move pre-0.4 single-user data into users/.owner/ (keeping a backup copy)."""
        found = [n for n in LEGACY_FILES if (self.data_dir / n).exists()]
        if not found or self.accounts.has_users() or (self.users_dir / OWNER).exists():
            return False
        backup, owner = self.data_dir / "backup-before-0.4", self.users_dir / OWNER
        owner.mkdir(parents=True, exist_ok=True)
        backup.mkdir(parents=True, exist_ok=True)
        for n in found:
            src = self.data_dir / n
            (shutil.copytree if src.is_dir() else shutil.copy2)(src, backup / n)
            shutil.move(str(src), str(owner / n))
        self.log.info("Moved your existing settings and library into the multi-user layout (backup in %s). "
                      "The first account you create gets them.", backup)
        return True

    def start(self, run_loops=True):
        self._loops = run_loops
        self.users_dir.mkdir(parents=True, exist_ok=True)
        self.migrate_legacy()
        names = [u["name"] for u in self.accounts.users()]
        if (self.users_dir / OWNER).exists() and not self.accounts.has_users():
            names.append(OWNER)
        for n in names:
            self._open(n, run_loops)
        if run_loops:
            threading.Thread(target=self._watch, daemon=True, name="hub").start()

    def _open(self, name, run_loop=True):
        admin = name == OWNER or self.accounts.is_admin(name)
        try:
            rt = self.factory(name, self.users_dir / name, admin)
        except Exception as e:
            self.log.error("Couldn't load %s's settings: %s", name, e)
            return None
        if not admin:                                            # may use the admin's cookie refresher, see shared_refresher
            for p in rt.engine.providers:
                if p.key == "goodreads":
                    p.shared_refresher = lambda n=name: self.shared_refresher(n)
        with self.lock:
            self.runtimes[name] = rt
        if run_loop:
            rt.thread = threading.Thread(target=self._loop, args=(rt,), daemon=True, name=f"sync-{name}")
            rt.thread.start()
        return rt

    def _loop(self, rt):
        if rt.state.book_count() == 0 and rt.config["hardcover"]["token"]:
            try:
                rt.log.info("Loaded %d books from Hardcover for the web UI", rt.engine.refresh_library())
            except Exception as e:
                rt.log.warning("Couldn't preload the library for the web UI: %s", e)
        rt.engine.loop(rt.stop)

    def _watch(self):
        """Health: touch the heartbeat while every user's sync loop is alive (Docker HEALTHCHECK reads it)."""
        while not self._stop.wait(60):
            with self.lock:
                alive = all(rt.thread is None or rt.thread.is_alive() for rt in self.runtimes.values())
            if alive and self.heartbeat:
                try:
                    Path(self.heartbeat).write_text(str(time.time()))
                except OSError:
                    pass

    def stop_all(self):
        self._stop.set()
        with self.lock:
            rts = list(self.runtimes.values())
        for rt in rts:
            rt.stop.set()

    # ---- lookups
    def runtime(self, name):
        with self.lock:
            return self.runtimes.get(name)

    def first_run(self):
        return not self.accounts.has_users()

    def has_legacy_data(self):
        return (self.users_dir / OWNER).exists()

    def setup_code_hash(self):
        """The pre-0.4 web password (HELIOGRAPH__web__password), if set: it proves you own this install."""
        return (self.server["web"].get("password") or "").strip()

    def admin_name(self):
        return next((u["name"] for u in self.accounts.users() if u["admin"]), None)

    def admin_refresher(self):
        """(url, token, the admin's own account) of the admin's Goodreads cookie refresher, or (None, None, None)."""
        rt = self.runtime(self.admin_name() or "")
        gr = rt.config["goodreads"] if rt else {}
        url = (gr.get("cookie_refresh_url") or "").strip()
        if not url:
            return None, None, None
        return url, gr.get("cookie_refresh_token"), (gr.get("cookie_refresh_account") or "default").strip().lower()

    def shared_refresher(self, name):
        """For a non-admin user without their own refresher: the admin's, with the account the admin assigned them."""
        url, token, admin_acct = self.admin_refresher()
        if not url:
            return None
        acct = self.accounts.refresher_account(name)
        if not acct:
            return {"error": "no Cookie Auto-Refresh URL configured; ask the admin to assign you an account on their "
                             "cookie refresher (Users tab), or set up your own"}
        if acct == admin_acct:                                   # never hand out the admin's own Goodreads session
            return {"error": "the refresher account assigned to you is the admin's own; ask the admin to change it"}
        return {"url": url, "token": token, "account": acct}

    def set_refresher_account(self, name, account):
        _, _, admin_acct = self.admin_refresher()
        if self.accounts.is_admin(name):
            raise AccountError("The admin sets their own refresher in Settings → Goodreads")
        self.accounts.set_refresher_account(name, account, admin_acct or "default")

    # ---- accounts
    def register(self, name, password, code="", local=False):
        """Create an account. The first one becomes the admin (and gets the pre-0.4 data); it needs the request to
        come from this computer (localhost / allowed hosts) or the old web password as setup code. Later ones need
        an invite code from the admin."""
        name = clean_name(name)
        if name == OWNER.strip("."):
            raise AccountError("That username is reserved")
        with self.lock:
            first = self.first_run()
            if first:
                pw = self.setup_code_hash()
                if not local and not (pw and code and verify_password(code.strip(), pw)):
                    raise AccountError("To create the first account, open Heliograph at http://localhost:<port> on the "
                                       "machine running it" + (", or enter your current web password as the setup code"
                                                               if pw else ", or add this host name to HELIOGRAPH__web__allowed_hosts"))
                self.accounts.check_password(password)
            else:
                if self.accounts.exists(name):
                    raise AccountError("That username is taken")
                self.accounts.check_password(password)
                if not self.accounts.take_invite(code):
                    raise AccountError("That invite code isn't valid (they work once and expire after 7 days)")
            self.accounts.create_user(name, password, admin=first)
            owner = self.runtimes.pop(OWNER, None)
            if first and owner:                                  # hand the existing data to the new admin
                owner.stop.set()
                if owner.thread:
                    owner.thread.join(timeout=60)
                owner.state.db.close()
                for h in list(owner.log.handlers):
                    h.close(); owner.log.removeHandler(h)
                (self.users_dir / OWNER).rename(self.users_dir / name)
                self.log.info("Your existing settings and library now belong to '%s'", name)
            self._open(name, run_loop=self._loops)
        return name

    def delete_user(self, name, by):
        if name == by:
            raise AccountError("You can't delete your own account")
        if not self.accounts.exists(name):
            raise AccountError("No such user")
        with self.lock:
            rt = self.runtimes.pop(name, None)
            if rt:
                rt.stop.set()
                if rt.thread:
                    rt.thread.join(timeout=60)
                rt.state.db.close()
                for h in list(rt.log.handlers):
                    h.close(); rt.log.removeHandler(h)
            self.accounts.delete_user(name)
            folder = self.users_dir / name
            if folder.exists():                                  # kept aside, not destroyed
                folder.rename(self.users_dir / f".deleted-{name}-{int(time.time())}")
        self.log.info("User '%s' deleted by '%s' (their folder was kept as users/.deleted-%s-…)", name, by, name)
