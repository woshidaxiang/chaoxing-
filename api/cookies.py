# -*- coding: utf-8 -*-
"""
Cookie 持久化工具。

【多账号隔离】说明：
本项目的 Web 面板支持多个 Worker 容器并发跑不同账号的任务。
如果所有账号共用同一个 cookies.txt，会造成互相覆盖：
    Worker-A 登录账号A -> 写 cookies.txt(A 的 _uid)
    Worker-B 登录账号B -> 覆盖 cookies.txt(B 的 _uid)
    Worker-A 上报视频进度 -> 读到 B 的 Cookie -> _uid 丢失 -> Cannot get uid !
因此这里把 Cookie 文件按「账号」隔离，不同账号使用不同的文件。

文件命名规则（由 api.base 通过环境变量 / contextvar 注入当前账号）：
    cookies.txt                  -> 默认（未指定账号时，保持与旧版兼容）
    cookies.<账号哈希>.txt        -> 指定账号时
"""
import os
import hashlib
import threading

import requests

from api.config import GlobalConst as gc

# 定义全局 Cookie 文件锁，保证读写绝对安全
cookie_lock = threading.RLock()

# 当前线程正在运行的账号（用于让 Cookie 按账号隔离）
# 使用 threading.local 而不是全局变量，因为多个 Worker 是独立进程、
# 单进程内也可能有多个线程处理不同任务。
_ctx = threading.local()


def set_current_account(username: str):
    """
    标记「当前线程」正在使用哪个账号，后续 use_cookies()/save_cookies()
    都会读写该账号专属的 Cookie 文件。
    """
    _ctx.username = (username or "").strip()


def get_current_account() -> str:
    return getattr(_ctx, "username", "") or ""


def _account_hash(username: str) -> str:
    """账号 -> 短哈希，避免文件名里出现手机号等隐私信息。"""
    return hashlib.sha1(username.encode("utf-8")).hexdigest()[:12]


def _cookies_path() -> str:
    """
    返回当前账号应使用的 Cookie 文件路径。
    - 未指定账号：退回默认 cookies.txt（兼容单账号 / 旧行为）
    - 指定账号：cookies.<hash>.txt，不同账号互不干扰
    """
    username = get_current_account()
    if not username:
        return gc.COOKIES_PATH

    base = gc.COOKIES_PATH
    # 支持 COOKIES_PATH 带目录的情况（如 /app/data/cookies.txt）
    directory = os.path.dirname(base)
    default_name = os.path.basename(base) or "cookies.txt"
    stem, ext = os.path.splitext(default_name)
    if not ext:
        ext = ".txt"

    per_account_name = f"{stem}.{_account_hash(username)}{ext}"
    return os.path.join(directory, per_account_name) if directory else per_account_name


def save_cookies(session: requests.Session):
    path = _cookies_path()
    with cookie_lock:
        buffer = ""
        for k, v in session.cookies.items():
            buffer += f"{k}={v};"
        buffer = buffer.removesuffix(";")
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w") as f:
            f.write(buffer)


def use_cookies() -> dict:
    path = _cookies_path()
    with cookie_lock:
        if not os.path.exists(path):
            return {}

        cookies = {}
        try:
            with open(path, "r") as f:
                buffer = f.read().strip()
                if not buffer:
                    return {}
                for item in buffer.split(";"):
                    item = item.strip()
                    if not item:
                        continue
                    parts = item.split("=", 1)
                    if len(parts) == 2:
                        cookies[parts[0]] = parts[1]
        except Exception:
            return {}

        return cookies
