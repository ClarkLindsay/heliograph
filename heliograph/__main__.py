import argparse, json, logging, signal, sys, threading, time
from logging.handlers import RotatingFileHandler
from .config import Config, ConfigError, CONFIG_PATH, DB_PATH, LOG_PATH, APP_DIR, HEARTBEAT_PATH
from .engine import Engine
from .providers import build
from .state import State


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
    cfg = Config()
    limit = max(15, 3 * int(cfg["common"]["poll_interval_minutes"])) * 60
    try:
        age = time.time() - float(HEARTBEAT_PATH.read_text())
    except (OSError, ValueError):
        return 1
    return 0 if age < limit else 1


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


def _bootstrap(eng, log):
    try:
        n = eng.refresh_library()
        log.info("Loaded %d books from Hardcover for the web UI", n)
    except Exception as e:
        log.warning("Couldn't preload the library for the web UI: %s", e)


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
    if not CONFIG_PATH.exists():
        Config().save()
        print(f"Wrote default config to {CONFIG_PATH}")
    if a.init:
        return
    try:
        cfg = Config()
    except ConfigError as e:
        sys.exit(f"Config error: {e}")
    log = setup_logging(cfg)
    log.info("Secrets: %s", cfg.crypto.describe())
    if cfg.encryption_enabled and cfg._plaintext_on_disk:
        cfg.save()
        log.info("Encrypted the secrets stored in config.json")
    elif not cfg.encryption_enabled and cfg._plaintext_on_disk:
        log.warning("Secrets in config.json are stored unencrypted; see README > Protecting secrets")
    if a.check:
        sys.exit(check(cfg, log))
    if a.inspect:
        return inspect(cfg, log)
    eng = Engine(cfg, build(cfg, log), State(DB_PATH), log, dry_run=a.dry_run,
                 heartbeat=None if a.dry_run else HEARTBEAT_PATH)
    if a.once or a.dry_run:
        try:
            eng.run_once()
        except Exception as e:
            log.error("Sync failed: %s", e)
            sys.exit(1)
        return
    stop = threading.Event()
    web = None
    if cfg["web"]["enabled"]:
        from .web import WebServer
        try:
            web = WebServer(eng, cfg, log)
            web.start()
            log.info("Web UI on port %s (publish it in docker-compose; keep it bound to 127.0.0.1)", web.port)
        except OSError as e:
            log.error("Web UI couldn't start: %s", e)
        if web and eng.state.book_count() == 0:        # existing installs: fill the Books list once
            threading.Thread(target=lambda: _bootstrap(eng, log), daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):          # docker stop sends SIGTERM
        signal.signal(sig, lambda *_: stop.set())
    log.info("Heliograph running; polling every %s min; services: %s", cfg["common"]["poll_interval_minutes"],
             ", ".join(p.label for p in eng.providers if p.enabled) or "none enabled")
    eng.loop(stop)
    if web:
        web.stop()


main()
