"""Config: /data/config.json (HELIOGRAPH_DIR) plus HELIOGRAPH__<section>__<key> environment overrides."""
import copy, json, os, threading
from pathlib import Path
from .crypto import Crypto, CryptoError, read_secret_file

_SAVE_LOCK = threading.RLock()

APP_DIR = Path(os.environ.get("HELIOGRAPH_DIR") or "/data")
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "state.db"
LOG_PATH = APP_DIR / "heliograph.log"
HEARTBEAT_PATH = APP_DIR / "heartbeat"

DEFAULTS = {
    "hardcover": {"token": "", "home_url": ""},       # source of truth (api token from hardcover.app/account/api)
    "goodreads": {
        "enabled": False,
        "cookie": "",                 # full Cookie header; blank = pulled from the refresher
        "cookie_refresh_url": "",     # base URL of goodreads-cookie-refresher
        "cookie_refresh_token": "",   # REFRESHER_AUTH_TOKEN, if set
        "user_agent": "",             # blank = same Firefox UA ShelfSync uses
        "request_delay_seconds": 3,   # minimum gap between Goodreads requests; bursts are what trigger WAF challenges
        "waf_cooldown_minutes": 15,   # after a challenge the refresher can't clear, leave Goodreads alone this long
        "home_url": "",               # where the dashboard icon links; blank = the service's homepage
    },
    "storygraph": {"enabled": False, "session_cookie": "", "remember_user_token": "", "home_url": ""},
    "fable": {"enabled": False, "email": "", "password": "", "id_token": "",
              "refresh_token": "", "token_expires_at": 0, "user_id": "", "home_url": ""},
    "pagebound": {"enabled": False, "email": "", "password": "", "id_token": "",
                  "refresh_token": "", "token_expires_at": 0, "api_token": "", "user_id": "", "home_url": ""},
    "librarything": {
        "enabled": False,
        "username": "", "password": "",       # signs in through LibraryThing's app endpoint (like the plugin)
        "session_cookie": "",                 # or paste cookie_userid/cookie_usernum/cookie_userchecksum
        "read_collection_id": 0,              # 0 = "Your library"; set e.g. 5 for "Read but unowned"
        "user_agent": "", "home_url": "",
    },
    "web": {
        "enabled": True,
        "port": 8080,
        "password": "",              # optional; enables HTTP Basic auth (any username)
        "allowed_hosts": "",         # extra Host names accepted when no password is set (comma separated)
    },
    "common": {
        "poll_interval_minutes": 5,
        "min_update_interval_seconds": 60,   # plugin: track frequency
        "progress_mode": "pages",            # pages | percent  (plugin: sync by remote pages)
        "progress_step_percent": 1,          # plugin: progress step
        "progress_step_pages": 1,
        "link_by_isbn": True,                # plugin: Automatically link by ISBN
        "link_by_title": True,               # plugin: Automatically link by title and author
        "preferred_language": "English",     # only link editions in this language (blank = any) where the service says
        "avoid_audiobooks": True,            # never link an audiobook edition where the service says which is which
        "remove_from_services": "off",       # off | want | all: books you remove from Hardcover come off your services
        "sync_want_to_read": True,
        "sync_paused_dnf": False,
        "sync_ratings": True,
        "initial_sync": "baseline",          # baseline = record library, push only later changes | import_all
        "verbose_logging": False,
    },
}

ENV_PREFIX = "HELIOGRAPH__"
# Values that are encrypted at rest in config.json when a key is configured (and accepted as "enc:v1:..." anywhere).
SECRET_FIELDS = {
    ("hardcover", "token"), ("goodreads", "cookie"), ("goodreads", "cookie_refresh_token"),
    ("storygraph", "session_cookie"), ("storygraph", "remember_user_token"),
    ("fable", "password"), ("fable", "id_token"), ("fable", "refresh_token"),
    ("pagebound", "password"), ("pagebound", "id_token"), ("pagebound", "refresh_token"), ("pagebound", "api_token"),
    ("librarything", "password"), ("librarything", "session_cookie"), ("web", "password"),
}


class ConfigError(Exception):
    pass


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _coerce(default, raw):
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    return raw


class Config:
    def __init__(self, path=CONFIG_PATH, crypto=None):
        self.path = Path(path)
        try:
            self.crypto = crypto if crypto is not None else Crypto()
        except CryptoError as e:
            raise ConfigError(str(e))
        self.data = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            _merge(self.data, json.loads(self.path.read_text(encoding="utf-8")))
        self._env = set()          # settings supplied via environment; never written back to the file
        for name, raw in os.environ.items():
            up = name.upper()
            if not up.startswith(ENV_PREFIX):
                continue
            body = name[len(ENV_PREFIX):]
            from_file = body.upper().endswith("_FILE")           # HELIOGRAPH__fable__password_FILE=/run/secrets/x
            parts = (body[:-5] if from_file else body).lower().split("__")
            if len(parts) == 2 and parts[0] in self.data and parts[1] in self.data[parts[0]]:
                sec, key = parts
                if from_file:
                    try:
                        raw = read_secret_file(raw)
                    except OSError as e:
                        raise ConfigError(f"{name}: can't read {raw}: {e}")
                self.data[sec][key] = _coerce(self.data[sec][key], raw)
                self._env.add((sec, key))
        self._plaintext_on_disk = 0
        try:
            for sec, key in SECRET_FIELDS:
                v = self.data[sec][key]
                if Crypto.is_encrypted(v):
                    self.data[sec][key] = self.crypto.decrypt(v)
                elif v and (sec, key) not in self._env:
                    self._plaintext_on_disk += 1
        except CryptoError as e:
            raise ConfigError(str(e))

    @property
    def encryption_enabled(self):
        return self.crypto.enabled

    def __getitem__(self, k):
        return self.data[k]

    def save(self):
        with _SAVE_LOCK:                             # services save their own tokens/cookies from different threads
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out = copy.deepcopy(self.data)
        if self.crypto.enabled:                      # secrets are stored encrypted whenever a key is configured
            for sec, key in SECRET_FIELDS:
                if out[sec][key] and not Crypto.is_encrypted(out[sec][key]):
                    out[sec][key] = self.crypto.encrypt(out[sec][key])
        on_disk = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        for sec, key in self._env:
            if key in on_disk.get(sec, {}):
                out[sec][key] = on_disk[sec][key]
            else:
                out[sec].pop(key, None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        self._plaintext_on_disk = 0 if self.crypto.enabled else sum(
            1 for sec, key in SECRET_FIELDS if out[sec][key] and (sec, key) not in self._env)
