"""Firebase email/password sign-in + token refresh, shared by Fable and Pagebound (as in ShelfSync)."""
import requests
from .base import ProviderError

SECURE_TOKEN = "https://securetoken.googleapis.com/v1/token"


def _err(r, fallback):
    try:
        m = r.json().get("error", {}).get("message")
        return m or fallback
    except Exception:
        return fallback


def sign_in(url, key, body):
    r = requests.post(url, params={"key": key}, json=body, timeout=30)
    d = r.json() if r.content else {}
    if r.status_code != 200 or not d.get("idToken") or not d.get("refreshToken"):
        raise ProviderError(_err(r, f"Firebase sign-in failed (HTTP {r.status_code})"))
    return {"id_token": d["idToken"], "refresh_token": d["refreshToken"],
            "expires_in": int(d.get("expiresIn") or 3600), "email": d.get("email")}


def refresh(key, refresh_token):
    r = requests.post(SECURE_TOKEN, params={"key": key}, timeout=30,
                      data={"grant_type": "refresh_token", "refresh_token": refresh_token})
    d = r.json() if r.content else {}
    if r.status_code != 200 or not d.get("id_token") or not d.get("refresh_token"):
        return None
    return {"id_token": d["id_token"], "refresh_token": d["refresh_token"],
            "expires_in": int(d.get("expires_in") or 3600)}
