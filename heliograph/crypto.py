"""Encryption at rest for stored secrets (Fernet = AES-128-CBC + HMAC-SHA256), plus PBKDF2 hashing for the web password.

Hashing can't be used for cookies/passwords/tokens: the app has to send the real value to each service. Encrypting
them with a key that lives OUTSIDE the data volume and the compose file (a Docker secret / key file) is the useful
protection: a leaked compose file, config.json or volume backup is then unreadable without the key.
"""
import base64, hashlib, hmac, os
from pathlib import Path

PREFIX = "enc:v1:"
KEY_FILE_DEFAULT = "/run/secrets/heliograph_key"


class CryptoError(Exception):
    pass


def read_secret_file(path) -> str:
    """Text of a secret/key file, tolerating the BOM / UTF-16 that Windows PowerShell likes to write."""
    data = Path(path).read_bytes()
    text = data.decode("utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("utf-8-sig")
    return text.strip()


def _is_fernet_key(raw: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(raw.encode())) == 32
    except Exception:
        return False


def _fernet_key(raw: str) -> bytes:
    raw = raw.strip()
    if _is_fernet_key(raw):
        return raw.encode()                                  # a proper Fernet key from --gen-key
    derived = hashlib.pbkdf2_hmac("sha256", raw.encode(), b"hcsync-secrets-v1", 200_000)   # any passphrase works too; salt keeps the pre-rename name: changing it breaks existing passphrase keys
    return base64.urlsafe_b64encode(derived)


def _pick_key(text: str) -> str:
    """If the file picked up extra lines (e.g. compose progress output), use the line that is a valid key."""
    for line in reversed([l.strip() for l in text.splitlines() if l.strip()]):
        if _is_fernet_key(line):
            return line
    return text.strip()


def load_key():
    """-> (key, where it came from) or (None, None) when no key is configured. Raises if a key file is unusable."""
    raw = os.environ.get("HELIOGRAPH_KEY")
    if raw and raw.strip():
        return raw.strip(), "HELIOGRAPH_KEY"
    path = os.environ.get("HELIOGRAPH_KEY_FILE") or (KEY_FILE_DEFAULT if Path(KEY_FILE_DEFAULT).exists() else None)
    if path:
        try:
            text = read_secret_file(path)
        except (OSError, UnicodeDecodeError) as e:
            raise CryptoError(f"Can't read the key file {path}: {e}")
        if not text:
            raise CryptoError(f"The key file {path} is empty. Create it again with --gen-key (README > Protecting secrets).")
        return _pick_key(text), path
    return None, None


class Crypto:
    def __init__(self, key=None):
        self.source = "argument" if key is not None else None
        if key is None:
            key, self.source = load_key()
        self.enabled = bool(key)
        self._f = None
        if key:
            from cryptography.fernet import Fernet
            self._f = Fernet(_fernet_key(key))

    def describe(self):
        if self.enabled:
            return f"encryption on (key from {self.source})"
        return ("encryption off: no key found (looked for HELIOGRAPH_KEY, HELIOGRAPH_KEY_FILE and the heliograph_key Docker secret at "
                f"{KEY_FILE_DEFAULT}; is the service-level `secrets: - heliograph_key` uncommented?)")

    @staticmethod
    def is_encrypted(value):
        return isinstance(value, str) and value.startswith(PREFIX)

    def encrypt(self, value: str) -> str:
        if not self._f:
            raise CryptoError("No encryption key configured (HELIOGRAPH_KEY or a key file)")
        return PREFIX + self._f.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        if not self.is_encrypted(value):
            return value
        if not self._f:
            raise CryptoError("A stored secret is encrypted but no key is available. Provide the same key "
                              "(HELIOGRAPH_KEY, HELIOGRAPH_KEY_FILE or the heliograph_key Docker secret).")
        from cryptography.fernet import InvalidToken
        try:
            return self._f.decrypt(value[len(PREFIX):].encode()).decode()
        except InvalidToken:
            raise CryptoError("A stored secret couldn't be decrypted: wrong key?")


def generate_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


# ---- web-password hashing (the one secret that is only ever compared, so it can be a real hash)
def hash_password(password: str, iterations=240_000) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2:{iterations}:{salt.hex()}:{dk.hex()}"


def verify_password(given: str, stored: str) -> bool:
    if stored.startswith("pbkdf2:"):
        try:
            _, it, salt, want = stored.split(":")
            dk = hashlib.pbkdf2_hmac("sha256", given.encode(), bytes.fromhex(salt), int(it))
            return hmac.compare_digest(dk.hex(), want)
        except (ValueError, TypeError):
            return False
    return hmac.compare_digest(given.encode(), stored.encode())
