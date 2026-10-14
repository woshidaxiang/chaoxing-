# -*- coding: utf-8 -*-
"""
登录令牌（HMAC-SHA256 签名，无状态）

token 结构： base64url(payload_json) + "." + base64url(hmac_sha256(payload))
payload  ： {"u": 用户名, "e": 过期时间戳}

- 服务端不保存会话，令牌放在 HttpOnly Cookie 里；重启服务不掉线
- 密钥取环境变量 PANEL_SECRET；没设就自动生成并存到 data/secret.key
  （这个文件丢了 = 所有人需要重新登录一次）
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
os.makedirs(_DATA_DIR, exist_ok=True)
_SECRET_FILE = os.path.join(_DATA_DIR, "secret.key")

# 默认登录有效期：7 天
TOKEN_TTL = int(os.environ.get("PANEL_TOKEN_TTL", str(7 * 24 * 3600)))

_SECRET_CACHE = None


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _load_secret() -> bytes:
    """读取或生成签名密钥"""
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE
    env = (os.environ.get("PANEL_SECRET") or "").strip()
    if env:
        _SECRET_CACHE = env.encode("utf-8")
        return _SECRET_CACHE
    if os.path.exists(_SECRET_FILE):
        try:
            with open(_SECRET_FILE, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                _SECRET_CACHE = v.encode("utf-8")
                return _SECRET_CACHE
        except Exception:
            pass
    generated = secrets.token_urlsafe(48)
    try:
        with open(_SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(generated)
        os.chmod(_SECRET_FILE, 0o600)
    except Exception:
        pass
    _SECRET_CACHE = generated.encode("utf-8")
    return _SECRET_CACHE


def make_token(username: str, ttl: int = None) -> str:
    """签发令牌"""
    payload = {
        "u": username,
        "e": int(time.time()) + (TOKEN_TTL if ttl is None else int(ttl)),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = _b64e(hmac.new(_load_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def parse_token(token: str):
    """校验令牌，成功返回用户名，失败返回 None"""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expect = _b64e(hmac.new(_load_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("e", 0)) < time.time():
        return None
    return payload.get("u") or None
