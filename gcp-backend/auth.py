"""ログイン(Google OAuth)とセッション。FastAPI/ADKに依存しない部分。

流れ:
  1. APKが login_id と challenge(= verifierのSHA-256)を作り、外部ブラウザで /auth/login を開く
  2. Googleでログイン → /auth/callback でメールアドレスを確認し、許可リスト(ALLOWED_EMAILS)にあれば成立
  3. APKが login_id と verifier で /auth/poll を呼び、署名つきセッショントークンを受け取る
セッションは HMAC 署名つきの文字列で、サーバー側には保存しない。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
LOGIN_SCOPES = ["openid", "email"]
GRANT_SCOPES = ["openid", "email", "https://www.googleapis.com/auth/cloud-platform"]

SESSION_SECONDS = 30 * 24 * 3600
STATE_SECONDS = 15 * 60
LOGIN_TTL = 10 * 60

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class AuthError(Exception):
    """利用者に見せてよい認証エラー。"""


# ---------------------------------------------------------------- 署名

def _b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret():
    s = os.environ.get("SESSION_SECRET", "")
    if len(s) < 16:
        raise AuthError("SESSION_SECRET が未設定か短すぎます（16文字以上）")
    return s.encode()


def sign(payload):
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def unsign(token):
    """署名と期限が正しければペイロードを返す。だめなら None。"""
    secret = _secret()  # 設定ミスは握りつぶさず AuthError として伝える
    try:
        body, sig = token.split(".", 1)
        good = _b64e(hmac.new(secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        payload = json.loads(_b64d(body))
    except (ValueError, AttributeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


# ---------------------------------------------------------------- 許可リスト・セッション

def allowed_emails():
    return {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}


def is_allowed(email):
    return bool(email) and email.lower() in allowed_emails()


def issue_session(email):
    return sign({"k": "s", "email": email.lower(), "exp": int(time.time()) + SESSION_SECONDS})


def verify_session(token):
    """有効なセッションなら（許可リストに今も載っている場合に限り）メールアドレスを返す。"""
    payload = unsign(token or "")
    if not payload or payload.get("k") != "s":
        return None
    email = payload.get("email", "")
    return email if is_allowed(email) else None


def bearer_email(authorization_header):
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        return None
    return verify_session(authorization_header[7:].strip())


def make_state(kind, ident):
    return sign({"k": "state", "kind": kind, "id": ident, "exp": int(time.time()) + STATE_SECONDS})


def read_state(state):
    payload = unsign(state or "")
    if not payload or payload.get("k") != "state":
        return None
    return payload


def challenge_of(verifier):
    return _b64e(hashlib.sha256(verifier.encode()).digest())


def new_id():
    return secrets.token_urlsafe(18)


# ---------------------------------------------------------------- ログインの受け渡し

class LoginStore:
    """APKとブラウザの間で、ログイン結果を一度だけ受け渡す（メモリ上・短時間）。"""

    def __init__(self):
        self._d = {}
        self._lock = threading.Lock()

    def _purge(self):
        now = time.time()
        for k in [k for k, v in self._d.items() if now - v["created"] > LOGIN_TTL]:
            del self._d[k]

    def create(self, login_id, challenge):
        if not _ID_RE.match(login_id or "") or not _CHALLENGE_RE.match(challenge or ""):
            raise AuthError("ログインの要求が正しくありません")
        with self._lock:
            self._purge()
            self._d[login_id] = {"challenge": challenge, "email": None, "created": time.time()}

    def complete(self, login_id, email):
        with self._lock:
            self._purge()
            entry = self._d.get(login_id)
            if not entry:
                raise AuthError("ログインの有効期限が切れました。アプリからやり直してください")
            entry["email"] = email

    def poll(self, login_id, verifier):
        """('pending'|'ready'|'invalid', email) を返す。readyは一度きり。"""
        with self._lock:
            self._purge()
            entry = self._d.get(login_id or "")
            if not entry or not verifier or not hmac.compare_digest(challenge_of(verifier), entry["challenge"]):
                return "invalid", None
            if not entry["email"]:
                return "pending", None
            del self._d[login_id]
            return "ready", entry["email"]


# ---------------------------------------------------------------- Google連携

def client_config():
    cid, secret = os.environ.get("GOOGLE_CLIENT_ID", ""), os.environ.get("GOOGLE_CLIENT_SECRET", "")
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not (cid and secret and base):
        raise AuthError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / PUBLIC_BASE_URL が未設定です")
    return cid, secret, base + "/auth/callback"


def public_base():
    return client_config()[2].rsplit("/auth/callback", 1)[0]


def build_auth_url(state, scopes, login_hint=None, prompt="select_account"):
    cid, _secret_, redirect = client_config()
    params = {"client_id": cid, "redirect_uri": redirect, "response_type": "code",
              "scope": " ".join(scopes), "state": state, "access_type": "online", "prompt": prompt}
    if login_hint:
        params["login_hint"] = login_hint
    return GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code):
    """認可コードをトークンに交換する。テストでは差し替える。"""
    cid, secret, redirect = client_config()
    data = urllib.parse.urlencode({"code": code, "client_id": cid, "client_secret": secret,
                                   "redirect_uri": redirect, "grant_type": "authorization_code"}).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise AuthError(f"Googleとの連携に失敗しました ({e.code})")
    except (urllib.error.URLError, ValueError) as e:
        raise AuthError(f"Googleに接続できません: {e}")


def email_from_id_token(id_token):
    """Googleのトークンエンドポイントから直接受け取ったIDトークンの中身を確認する（TLSで直接受信したため署名検証は省略可）。"""
    cid, _s, _r = client_config()
    try:
        claims = json.loads(_b64d(id_token.split(".")[1]))
    except (ValueError, IndexError, AttributeError):
        raise AuthError("IDトークンを読み取れません")
    if claims.get("aud") != cid or claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise AuthError("IDトークンの発行元が正しくありません")
    if claims.get("exp", 0) < time.time() or not claims.get("email_verified"):
        raise AuthError("メールアドレスを確認できませんでした")
    return claims["email"].lower()
