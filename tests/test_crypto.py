import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from heliograph.config import Config, ConfigError
from heliograph.crypto import Crypto, CryptoError, generate_key, hash_password, verify_password
k = generate_key(); c = Crypto(k)
t = c.encrypt("hunter2"); assert t.startswith("enc:v1:") and "hunter2" not in t and c.decrypt(t) == "hunter2"
assert Crypto("my passphrase").decrypt(Crypto("my passphrase").encrypt("x")) == "x"
try: Crypto(generate_key()).decrypt(t); assert False
except CryptoError: pass
h = hash_password("pw"); assert verify_password("pw", h) and not verify_password("no", h) and verify_password("plain", "plain")
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    path = os.path.join(tmp, "c.json")
    plain = Config(path, Crypto(None) if False else Crypto("")); plain.data["fable"]["password"] = "pw"; plain.save()
    assert json.load(open(path))["fable"]["password"] == "pw" and plain._plaintext_on_disk == 1
    enc = Config(path, Crypto(k)); assert enc["fable"]["password"] == "pw" and enc._plaintext_on_disk == 1
    enc.save(); raw = json.load(open(path)); assert raw["fable"]["password"].startswith("enc:v1:") and "pw" != raw["fable"]["password"] and enc._plaintext_on_disk == 0
    assert Config(path, Crypto(k))["fable"]["password"] == "pw"
    try: Config(path, Crypto("")); assert False
    except ConfigError as e: assert "no key" in str(e).lower() or "encrypted" in str(e)
    os.environ["HELIOGRAPH__hardcover__token"] = c.encrypt("TOK")                 # an encrypted value straight from compose
    f = os.path.join(tmp, "secret.txt"); open(f, "w").write("fromfile\n"); os.environ["HELIOGRAPH__goodreads__cookie_refresh_token_FILE"] = f
    try:
        e2 = Config(path, Crypto(k)); assert e2["hardcover"]["token"] == "TOK" and e2["goodreads"]["cookie_refresh_token"] == "fromfile"
        e2.save(); raw = json.load(open(path)); assert raw["hardcover"]["token"] == "" and raw["goodreads"]["cookie_refresh_token"] == ""   # env values never written
    finally:
        del os.environ["HELIOGRAPH__hardcover__token"], os.environ["HELIOGRAPH__goodreads__cookie_refresh_token_FILE"]
import heliograph.crypto as cr
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    f = os.path.join(tmp, "key"); os.environ["HELIOGRAPH_KEY_FILE"] = f
    try:
        open(f, "w").write("")
        try: Crypto(); assert False
        except CryptoError as e: assert "empty" in str(e)
        try: Config(os.path.join(tmp, "c.json")); assert False
        except ConfigError as e: assert "empty" in str(e)
        k2 = generate_key()
        open(f, "w").write(f" Container x-run Creating\r\n Container x-run Created\r\n{k2}\r\n")       # polluted by compose output
        c2 = Crypto(); assert c2.enabled and c2.source == f and c2.decrypt(Crypto(k2).encrypt("v")) == "v"
        os.environ["HELIOGRAPH_KEY_FILE"] = os.path.join(tmp, "missing")
        try: Crypto(); assert False
        except CryptoError as e: assert "Can't read" in str(e)
    finally:
        del os.environ["HELIOGRAPH_KEY_FILE"]
    d = Crypto(); assert not d.enabled and "heliograph_key" in d.describe()
print("crypto ok")
