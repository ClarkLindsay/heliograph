import argparse, json, logging, signal, sys, threading, time
from logging.handlers import RotatingFileHandler
from .config import Config, ConfigError, LOG_PATH, APP_DIR, HEARTBEAT_PATH
from .hub import Hub, OWNER, default_factory
from .providers import build


def setup_logging(cfg):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [logging.StreamHandler(sys.stdout), RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
    for h in handlers:
        h.setFormatter(fmt)
    log = logging.getLogger("heliograph")
    log.handlers[:] = handlers
    log.setLevel(logging.DEBUG if cfg["common"]["verbose_logging"] else logging.INFO)
    return log


def healthcheck():
    """Healthy = the hub touched the heartbeat recently (it does every minute while every user's sync loop runs)."""
    try:
        age = time.time() - float(HEARTBEAT_PATH.read_text())
    except (OSError, ValueError):
        return 1
    return 0 if age < 10 * 60 else 1


def check(cfg, log):
    from .hardcover import HardcoverClient
    ok = True
    log.info("Secrets: %s", cfg.crypto.describe())
    try:
        HardcoverClient(cfg["hardcover"]["token"]).user_id
        log.info("Hardcover token OK")
    except Exception as e:
        log.error("Hardcover: %s", e); ok = False
    for p in build(cfg, log):
        if p.enabled:
            try:
                log.info(p.check_auth())
            except Exception as e:
                log.error("%s: %s", p.label, e); ok = False
    return 0 if ok else 1


def inspect(cfg, log):
    """Print what Hardcover actually holds for your Currently Reading books (to debug 'nothing gets pushed')."""
    from .hardcover import HardcoverClient
    hc = HardcoverClient(cfg["hardcover"]["token"])
    base = """query($uid: Int!) { user_books(where: {user_id: {_eq: $uid}, status_id: {_eq: 2}}) {
      id edition_id updated_at book { title pages } user_book_reads(order_by: {id: desc}, limit: 3) { %s } } }"""
    try:
        rows = hc._q(base % "id progress_pages progress_seconds started_at finished_at edition_id", {"uid": hc.user_id})
    except Exception:       # progress_seconds may not exist on this schema
        rows = hc._q(base % "id progress_pages started_at finished_at edition_id", {"uid": hc.user_id})
    for ub in rows["user_books"]:
        print(f"\n'{ub['book']['title']}'  book pages={ub['book']['pages']}  user_book {ub['id']}  updated {ub['updated_at']}")
        for r in ub["user_book_reads"]:
            print("   read:", json.dumps(r))
    print(f"\n{len(rows['user_books'])} currently-reading book(s). Only progress_pages is synced (null there = no progress to send).")


def pick_user(hub, wanted):
    """The user a one-shot command (--check, --once, --inspect) works on: --user, else the admin."""
    names = [u["name"] for u in hub.accounts.users()]
    if wanted:
        if wanted not in names:
            sys.exit(f"No user '{wanted}'. Users: {', '.join(names) or 'none yet'}")
        return wanted
    admin = next((u["name"] for u in hub.accounts.users() if u["admin"]), None)
    if admin:
        return admin
    if hub.has_legacy_data():
        return OWNER
    sys.exit("No users yet: open the web UI and create the first account")


def main():
    ap = argparse.ArgumentParser(prog="heliograph", description="Hardcover -> Goodreads/StoryGraph/Fable/Pagebound sync")
    ap.add_argument("--once", action="store_true", help="run a single sync cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="with --once: log what would be pushed, change nothing")
    ap.add_argument("--check", action="store_true", help="test the Hardcover token and every enabled service, then exit")
    ap.add_argument("--inspect", action="store_true", help="show raw Hardcover progress data for your Currently Reading books")
    ap.add_argument("--gen-key", action="store_true", help="print a new encryption key (save it as the heliograph_key Docker secret)")
    ap.add_argument("--encrypt", action="store_true", help="read a value (prompted, hidden) and print an enc:v1:... token for compose/env use")
    ap.add_argument("--hash-password", action="store_true", help="read a web UI password (prompted, hidden) and print its hash")
    ap.add_argument("--init", action="store_true", help="write a default config.json and exit")
    ap.add_argument("--healthcheck", action="store_true", help="exit 0 if the loop has completed a cycle recently")
    ap.add_argument("--user", help="with --check / --once / --inspect: which user (default: the admin)")
    ap.add_argument("--reset-password", metavar="NAME", help="set a new web UI password for an account (prompted, hidden)")
    a = ap.parse_args()
    if a.healthcheck:
        sys.exit(healthcheck())
    if a.gen_key:
        from .crypto import generate_key
        print(generate_key()); return
    if a.encrypt or a.hash_password:
        import getpass
        from .crypto import Crypto, CryptoError, hash_password
        value = getpass.getpass("Value (input hidden): ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\r\n")
        if a.hash_password:
            print(hash_password(value)); return
        try:
            print(Crypto().encrypt(value))
        except CryptoError as e:
            sys.exit(f"{e}\nCreate a key with --gen-key and provide it first.")
        return
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if a.init:
        return
    if a.reset_password:
        import getpass
        from .accounts import Accounts, AccountError
        acc, name = Accounts(APP_DIR / "users.json"), a.reset_password.strip().lower()
        if not acc.exists(name):
            sys.exit(f"No account '{name}'. Accounts: {', '.join(u['name'] for u in acc.users()) or 'none yet'}")
        pw = getpass.getpass("New password (input hidden): ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\r\n")
        try:
            acc.set_password(name, pw)
        except AccountError as e:
            sys.exit(str(e))
        print(f"Password changed for '{name}'; their other sessions were signed out. Restart the container to apply.")
        return
    try:
        server = Config(APP_DIR / "server.json")        # server-wide settings (web port, allowed hosts) from env/defaults
    except ConfigError as e:
        sys.exit(f"Config error: {e}")
    log = setup_logging(server)
    log.info("Secrets: %s", server.crypto.describe())
    hub = Hub(server, log)
    one_shot = a.check or a.inspect or a.once or a.dry_run
    if one_shot:
        hub.start(run_loops=False)
        name = pick_user(hub, a.user)
        rt = hub.runtime(name) if not a.dry_run else default_factory(name, hub.users_dir / name, name == OWNER or
                                                                      hub.accounts.is_admin(name), dry_run=True)
        if rt is None:
            sys.exit(f"Couldn't load {name}'s settings (see the log above)")
        if a.check:
            sys.exit(check(rt.config, rt.log))
        if a.inspect:
            return inspect(rt.config, rt.log)
        try:
            rt.engine.run_once()
        except Exception as e:
            rt.log.error("Sync failed: %s", e)
            sys.exit(1)
        return
    hub.start()
    stop = threading.Event()
    web = None
    if server["web"]["enabled"]:
        from .web import WebServer
        try:
            web = WebServer(hub, server, log)
            web.start()
            log.info("Web UI on port %s (publish it in docker-compose; keep it bound to 127.0.0.1)", web.port)
        except OSError as e:
            log.error("Web UI couldn't start: %s", e)
    for sig in (signal.SIGINT, signal.SIGTERM):          # docker stop sends SIGTERM
        signal.signal(sig, lambda *_: stop.set())
    users = [u["name"] for u in hub.accounts.users()]
    log.info("Heliograph running for %s", ", ".join(users) if users else
             "no accounts yet: open the web UI to create the first one" + (" (your existing data is waiting for it)"
                                                                          if hub.has_legacy_data() else ""))
    stop.wait()
    hub.stop_all()
    if web:
        web.stop()


main()
