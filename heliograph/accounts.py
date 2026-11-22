"""Web UI accounts: users, one-time invite codes and login sessions, in <data>/users.json.

Passwords are PBKDF2 hashes (crypto.hash_password). Session tokens and invite codes are only stored as SHA-256
hashes, so a copy of users.json can't be used to log in or to sign up."""
import hashlib, json, os, re, secrets, threading, time
from pathlib import Path
from .crypto import hash_password, verify_password

USERNAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")
MIN_PASSWORD = 8
SESSION_DAYS = 30
INVITE_DAYS = 7
_DUMMY = hash_password("not a real password")   # same cost as a real hash: an unknown username takes as long as a wrong password


class AccountError(ValueError):
    pass


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def clean_name(name):
    n = (name or "").strip().lower()
    if not USERNAME.match(n):
        raise AccountError("Usernames are 2-32 characters: letters, numbers, dots, dashes or underscores")
    return n


class Accounts:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = {"users": {}, "invites": {}, "sessions": {}}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    # ---- users
    def has_users(self):
        return bool(self.data["users"])

    def users(self):
        return [{"name": n, "admin": bool(u.get("admin")), "created": u.get("created", 0),
                 "refresher_account": u.get("refresher_account", "")} for n, u in sorted(self.data["users"].items())]

    def refresher_account(self, name):
        return self.data["users"].get(name, {}).get("refresher_account", "")

    def set_refresher_account(self, name, account, admin_account=""):
        """The admin's Goodreads cookie refresher account this user may use ("" = none). Unique per user, and never
        the admin's own: the refresher hands any caller with its token any account, so users can't choose."""
        acct = (account or "").strip().lower()
        with self.lock:
            if name not in self.data["users"]:
                raise AccountError("No such user")
            if acct:
                if not re.match(r"^[a-z0-9][a-z0-9_-]{0,31}$", acct):
                    raise AccountError("Refresher account names are lowercase letters, digits, - and _ (as in its ACCOUNTS)")
                if acct == (admin_account or "default") or acct == "default":
                    raise AccountError("That's the admin's own refresher account")
                taken = [n for n, u in self.data["users"].items() if n != name and u.get("refresher_account") == acct]
                if taken:
                    raise AccountError(f"'{acct}' is already assigned to {taken[0]}")
            self.data["users"][name]["refresher_account"] = acct
            self._save()

    def exists(self, name):
        return name in self.data["users"]

    def is_admin(self, name):
        return bool(self.data["users"].get(name, {}).get("admin"))

    @staticmethod
    def check_password(password):
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"Use a password of at least {MIN_PASSWORD} characters")

    def create_user(self, name, password, admin=False):
        name = clean_name(name)
        self.check_password(password)
        with self.lock:
            if name in self.data["users"]:
                raise AccountError("That username is taken")
            self.data["users"][name] = {"password": hash_password(password), "admin": bool(admin), "created": time.time()}
            self._save()
        return name

    def verify(self, name, password):
        u = self.data["users"].get((name or "").strip().lower())
        ok = verify_password(password or "", u["password"] if u else _DUMMY)
        return bool(u) and ok

    def set_password(self, name, password):
        self.check_password(password)
        with self.lock:
            self.data["users"][name]["password"] = hash_password(password)
            self.data["sessions"] = {k: s for k, s in self.data["sessions"].items() if s["user"] != name}
            self._save()

    def delete_user(self, name):
        with self.lock:
            self.data["users"].pop(name, None)
            self.data["sessions"] = {k: s for k, s in self.data["sessions"].items() if s["user"] != name}
            self._save()

    # ---- invites (one use each)
    def create_invite(self, by):
        code = secrets.token_urlsafe(9)
        with self.lock:
            self._prune()
            self.data["invites"][_h(code)] = {"by": by, "created": time.time(), "expires": time.time() + INVITE_DAYS * 86400}
            self._save()
        return code

    def invites(self):
        self._prune()
        return [{"id": k[:12], "by": v["by"], "created": v["created"], "expires": v["expires"]}
                for k, v in sorted(self.data["invites"].items(), key=lambda kv: kv[1]["created"])]

    def delete_invite(self, invite_id):
        with self.lock:
            for k in [k for k in self.data["invites"] if k.startswith(invite_id) and len(invite_id) >= 8]:
                del self.data["invites"][k]
            self._save()

    def take_invite(self, code):
        """Use up an invite code. -> True if it was valid."""
        with self.lock:
            self._prune()
            if self.data["invites"].pop(_h((code or "").strip()), None) is None:
                return False
            self._save()
            return True

    # ---- sessions
    def new_session(self, name):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self._prune()
            self.data["sessions"][_h(token)] = {"user": name, "expires": time.time() + SESSION_DAYS * 86400}
            self._save()
        return token

    def session_user(self, token):
        s = self.data["sessions"].get(_h(token)) if token else None
        if not s or s["expires"] < time.time() or s["user"] not in self.data["users"]:
            return None
        return s["user"]

    def end_session(self, token):
        with self.lock:
            if self.data["sessions"].pop(_h(token or ""), None) is not None:
                self._save()

    def _prune(self):
        now = time.time()
        for key in ("invites", "sessions"):
            self.data[key] = {k: v for k, v in self.data[key].items() if v["expires"] > now}


class LoginThrottle:
    """After 5 failed logins for a username, wait 30 s, doubling per further failure up to 15 minutes."""
    def __init__(self):
        self.fails, self.lock = {}, threading.Lock()

    def wait_for(self, name):
        n, until = self.fails.get(name, (0, 0))
        return max(0, until - time.time())

    def failed(self, name):
        with self.lock:
            n, _ = self.fails.get(name, (0, 0))
            n += 1
            self.fails[name] = (n, time.time() + (min(900, 30 * 2 ** (n - 5)) if n >= 5 else 0))

    def ok(self, name):
        self.fails.pop(name, None)
