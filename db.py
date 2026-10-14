# -*- coding: utf-8 -*-
"""
SQLite 持久化数据库层（任务队列 + 心跳）

设计要点：
- tasks 表保存任务状态，worker 用「认领」机制原子地取出待执行任务
- worker 运行时定期更新 heartbeat，Web 端据此判断任务是否真的在跑
- 即使 Web 服务或 worker 重启，数据都保留在磁盘上
"""
import os
import re
import base64
import sqlite3
import threading
import time

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CHAOXING_DB", os.path.join(_BASE_DIR, "data", "chaoxing.db"))

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

_write_lock = threading.Lock()

# 心跳超过该秒数视为任务已失联
HEARTBEAT_TIMEOUT = 30


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_conn()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            status      TEXT    NOT NULL DEFAULT 'pending',
            owner       TEXT    DEFAULT '',
            username    TEXT    DEFAULT '',
            courses     TEXT    DEFAULT '',
            speed       REAL    DEFAULT 1.0,
            jobs        INTEGER DEFAULT 4,
            config_path TEXT    DEFAULT '',
            pid         INTEGER DEFAULT 0,
            worker_pid  INTEGER DEFAULT 0,
            script_pid  INTEGER DEFAULT 0,
            stop_requested INTEGER DEFAULT 0,
            paused      INTEGER DEFAULT 0,
            heartbeat   REAL    DEFAULT 0,
            start_time  REAL    DEFAULT 0,
            end_time    REAL    DEFAULT 0,
            exit_code   INTEGER DEFAULT -999,
            created_at  REAL    DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS logs (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id  INTEGER NOT NULL,
            ts       REAL    NOT NULL,
            message  TEXT    NOT NULL,
            level    TEXT    DEFAULT 'info'
        );

        CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id, id);

        CREATE TABLE IF NOT EXISTS courses_cache (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT,
            course_id  TEXT,
            clazz_id   TEXT,
            title      TEXT,
            cpi        TEXT,
            updated_at REAL
        );

        -- 超星账号绑定：一个访问码(owner) 绑定一个超星账号
        CREATE TABLE IF NOT EXISTS accounts (
            owner        TEXT PRIMARY KEY,
            cx_username  TEXT DEFAULT '',
            cx_password  TEXT DEFAULT '',
            realname     TEXT DEFAULT '',
            courses      TEXT DEFAULT '',
            updated_at   REAL DEFAULT 0
        );

        -- ============ v14：用户体系 ============
        -- 面板用户（自助注册）。role: admin / user
        -- quota: 剩余可刷「不同课程」的额度；-1 表示不限
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT DEFAULT 'user',
            quota         INTEGER DEFAULT 0,
            banned        INTEGER DEFAULT 0,
            created_at    REAL DEFAULT 0,
            last_login    REAL DEFAULT 0
        );

        -- 注册名额码。type: invite（无限额度）/ redeem（+quota 门额度）
        -- 一次性：used_by 非空即已被使用
        CREATE TABLE IF NOT EXISTS invite_codes (
            code       TEXT PRIMARY KEY,
            type       TEXT NOT NULL DEFAULT 'invite',
            quota      INTEGER DEFAULT 0,
            note       TEXT DEFAULT '',
            used_by    TEXT DEFAULT '',
            used_at    REAL DEFAULT 0,
            created_by TEXT DEFAULT '',
            created_at REAL DEFAULT 0
        );

        -- 用户已刷过的课程（用于判断「同一门课重复刷不扣额度」）
        CREATE TABLE IF NOT EXISTS user_courses (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            course_id  TEXT NOT NULL,
            first_seen REAL DEFAULT 0,
            UNIQUE(username, course_id)
        );
        """)
        conn.commit()
        # 兼容旧库：补列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        for name, ddl in [("worker_pid", "ALTER TABLE tasks ADD COLUMN worker_pid INTEGER DEFAULT 0"),
                          ("heartbeat", "ALTER TABLE tasks ADD COLUMN heartbeat REAL DEFAULT 0"),
                          ("script_pid", "ALTER TABLE tasks ADD COLUMN script_pid INTEGER DEFAULT 0"),
                          ("stop_requested", "ALTER TABLE tasks ADD COLUMN stop_requested INTEGER DEFAULT 0"),
                          ("paused", "ALTER TABLE tasks ADD COLUMN paused INTEGER DEFAULT 0"),
                          ("owner", "ALTER TABLE tasks ADD COLUMN owner TEXT DEFAULT ''")]:
            if name not in cols:
                conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


# ============================================================
# 任务
# ============================================================
def create_task(username, courses, speed, jobs, config_path, owner=""):
    with _write_lock:
        conn = get_conn()
        try:
            cur = conn.execute(
                """INSERT INTO tasks
                   (status, owner, username, courses, speed, jobs, config_path, created_at)
                   VALUES ('pending', ?, ?, ?, ?, ?, ?, ?)""",
                (owner, username, courses, speed, jobs, config_path, time.time()),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def update_task(task_id, **fields):
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [task_id]
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute(f"UPDATE tasks SET {keys} WHERE id=?", values)
            conn.commit()
        finally:
            conn.close()


def get_task(task_id):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_latest_task(owner=None):
    conn = get_conn()
    try:
        if owner:
            row = conn.execute(
                "SELECT * FROM tasks WHERE owner=? ORDER BY id DESC LIMIT 1", (owner,)
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_tasks(limit=50, owner=None):
    conn = get_conn()
    try:
        if owner:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE owner=? ORDER BY id DESC LIMIT ?", (owner, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_active_tasks(owner=None):
    """状态为 pending / running 的任务"""
    conn = get_conn()
    try:
        if owner:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('pending','running') AND owner=? ORDER BY id DESC",
                (owner,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('pending','running') ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def claim_next_pending():
    """原子地认领下一个待执行任务，返回任务 dict 或 None"""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM tasks WHERE status='pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        now = time.time()
        conn.execute(
            """UPDATE tasks SET status='running', worker_pid=?, pid=?, heartbeat=?,
               start_time=?, exit_code=-999, end_time=0, script_pid=0, stop_requested=0,
               paused=0 WHERE id=?""",
            (os.getpid(), os.getpid(), now, now, row["id"]),
        )
        conn.execute("COMMIT")
        task = dict(row)
        task["status"] = "running"
        task["worker_pid"] = os.getpid()
        task["pid"] = os.getpid()
        task["heartbeat"] = now
        return task
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def touch_heartbeat(task_id):
    """更新心跳时间"""
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("UPDATE tasks SET heartbeat=? WHERE id=?", (time.time(), task_id))
            conn.commit()
        finally:
            conn.close()


def request_stop(task_id):
    """请求停止任务（worker 会读取该标记并终止脚本）"""
    update_task(task_id, stop_requested=1)


def is_stop_requested(task_id):
    """检查是否请求了停止"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT stop_requested FROM tasks WHERE id=?", (task_id,)).fetchone()
        return bool(row and row["stop_requested"])
    finally:
        conn.close()


def set_paused(task_id, paused):
    """设置暂停状态（worker 会据此 SIGSTOP/SIGCONT 脚本进程）"""
    update_task(task_id, paused=1 if paused else 0)


def is_paused(task_id):
    """检查是否处于暂停状态"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT paused FROM tasks WHERE id=?", (task_id,)).fetchone()
        return bool(row and row["paused"])
    finally:
        conn.close()


def is_task_alive(task):
    """判断任务是否真的在运行（状态 running 且心跳新鲜）"""
    if not task:
        return False
    if task.get("status") != "running":
        return False
    hb = task.get("heartbeat", 0) or 0
    return (time.time() - hb) < HEARTBEAT_TIMEOUT


def mark_stale_interrupted(only_worker_pid=None):
    """把心跳超时但仍标记 running 的任务改为 interrupted；返回受影响任务列表"""
    now = time.time()
    affected = []
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM tasks WHERE status='running'").fetchall()
        for r in rows:
            hb = r["heartbeat"] or 0
            if now - hb >= HEARTBEAT_TIMEOUT:
                if only_worker_pid and r["worker_pid"] != only_worker_pid:
                    continue
                affected.append(dict(r))
        for t in affected:
            update_task(t["id"], status="interrupted", end_time=now)
        return affected
    finally:
        conn.close()


def requeue_task(task_id):
    """把任务重新放回待执行队列（用于中断后自动续跑）"""
    update_task(task_id, status="pending", exit_code=-999, end_time=0,
                worker_pid=0, pid=0, heartbeat=0, script_pid=0, stop_requested=0, paused=0)


def delete_task(task_id):
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM logs WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            conn.commit()
        finally:
            conn.close()


def clear_all(owner=None):
    with _write_lock:
        conn = get_conn()
        try:
            if owner:
                conn.execute(
                    "DELETE FROM logs WHERE task_id IN (SELECT id FROM tasks WHERE owner=?)", (owner,)
                )
                conn.execute("DELETE FROM tasks WHERE owner=?", (owner,))
            else:
                conn.execute("DELETE FROM logs")
                conn.execute("DELETE FROM tasks")
                conn.execute("DELETE FROM courses_cache")
            conn.commit()
        finally:
            conn.close()


# ============================================================
# 日志
# ============================================================
def add_log(task_id, message, level="info"):
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO logs (task_id, ts, message, level) VALUES (?, ?, ?, ?)",
                (task_id, time.time(), message, level),
            )
            conn.commit()
        finally:
            conn.close()


def add_logs_bulk(task_id, items):
    if not items:
        return
    now = time.time()
    with _write_lock:
        conn = get_conn()
        try:
            conn.executemany(
                "INSERT INTO logs (task_id, ts, message, level) VALUES (?, ?, ?, ?)",
                [(task_id, now, m, lv) for (m, lv) in items],
            )
            # 自动裁剪：每个任务只保留最近 MAX_LOGS_PER_TASK 条，防止数据库无限膨胀
            _trim_logs(conn, task_id)
            conn.commit()
        finally:
            conn.close()


MAX_LOGS_PER_TASK = int(os.environ.get("MAX_LOGS_PER_TASK", "1500"))


def _trim_logs(conn, task_id):
    """删除该任务超出的最旧日志（保留最近 MAX_LOGS_PER_TASK 条）

    优化：先数一遍，只有真正超限才执行 DELETE，避免每次写入都跑删除语句。
    """
    if MAX_LOGS_PER_TASK <= 0:
        return
    cnt = conn.execute("SELECT COUNT(*) FROM logs WHERE task_id=?", (task_id,)).fetchone()[0]
    if cnt <= MAX_LOGS_PER_TASK:
        return
    conn.execute(
        """DELETE FROM logs WHERE task_id=? AND id < (
               SELECT id FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 1 OFFSET ?
           )""",
        (task_id, task_id, MAX_LOGS_PER_TASK - 1),
    )


def get_logs(task_id, after_id=0, limit=5000):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, ts, message, level FROM logs WHERE task_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (task_id, after_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_logs_tail(task_id, limit=200):
    """只取最后 N 条日志（用于页面首次加载，避免一次性拉取几 MB 数据把手机卡死）"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, ts, message, level FROM logs WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def get_log_stats(task_id):
    """从日志里统计任务进度（成功/失败/任务点总数）

    注意：匹配的关键词必须与脚本实际输出的、且**不会被日志过滤丢掉**的日志一致。
    - 成功：main.py 里 `Task success: ...`（info 级）
    - 失败：`重试次数达到上限` / `状态无效`（error 级）
    - 总数：`任务数量: N`（课程任务点总数）
    """
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN message LIKE '%Task success%' THEN 1 ELSE 0 END) AS success,
                 SUM(CASE WHEN message LIKE '%重试次数达到上限%'
                            OR message LIKE '%状态无效%' THEN 1 ELSE 0 END) AS failed,
                 SUM(CASE WHEN message LIKE '%识别到视频任务%' THEN 1 ELSE 0 END) AS videos,
                 SUM(CASE WHEN message LIKE '%任务完成%' THEN 1 ELSE 0 END) AS videos_done,
                 COUNT(*) AS total
               FROM logs WHERE task_id=?""",
            (task_id,),
        ).fetchone()
        d = dict(row) if row else {}
        stats = {k: (v or 0) for k, v in d.items()}
        # 任务点总数：取日志里最后一条「任务数量: N」
        last = conn.execute(
            "SELECT message FROM logs WHERE task_id=? AND message LIKE '%任务数量:%' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        total_points = 0
        if last:
            m = re.search(r"任务数量[:：]\s*(\d+)", last["message"])
            if m:
                total_points = int(m.group(1))
        stats["total_points"] = total_points
        return stats
    finally:
        conn.close()


def get_last_message(task_id, pattern):
    """取最后一条匹配的日志内容"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT message FROM logs WHERE task_id=? AND message LIKE ? ORDER BY id DESC LIMIT 1",
            (task_id, pattern),
        ).fetchone()
        return row["message"] if row else ""
    finally:
        conn.close()


def count_logs(task_id):
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM logs WHERE task_id=?", (task_id,)).fetchone()
        return row["c"] if row else 0
    finally:
        conn.close()


# ============================================================
# 课程缓存
# ============================================================
def save_courses(username, courses):
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM courses_cache WHERE username=?", (username,))
            now = time.time()
            conn.executemany(
                """INSERT INTO courses_cache (username, course_id, clazz_id, title, cpi, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(username, c.get("courseId", ""), c.get("clazzId", ""),
                  c.get("title", ""), c.get("cpi", ""), now) for c in courses],
            )
            conn.commit()
        finally:
            conn.close()


def get_courses(username):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT course_id, clazz_id, title, cpi, updated_at FROM courses_cache WHERE username=? ORDER BY id",
            (username,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ============================================================
# 超星账号绑定（一个 owner 绑定一个超星账号）
# 密码做 base64 编码存储，避免配置文件里直接看到明文
# ============================================================
def _enc(s):
    if not s:
        return ""
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _dec(s):
    if not s:
        return ""
    try:
        return base64.b64decode(s.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def save_account(owner, cx_username, cx_password, realname="", courses=""):
    """绑定/更新超星账号"""
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO accounts (owner, cx_username, cx_password, realname, courses, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(owner) DO UPDATE SET
                     cx_username=excluded.cx_username,
                     cx_password=excluded.cx_password,
                     realname=excluded.realname,
                     courses=excluded.courses,
                     updated_at=excluded.updated_at""",
                (owner, cx_username, _enc(cx_password), realname, courses, time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def get_account(owner, with_password=False):
    """读取绑定的超星账号（默认不返回密码）"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM accounts WHERE owner=?", (owner,)).fetchone()
        if not row:
            return None
        d = dict(row)
        if with_password:
            d["cx_password"] = _dec(d.get("cx_password", ""))
        else:
            d.pop("cx_password", None)
        return d
    finally:
        conn.close()


def delete_account(owner):
    """解绑"""
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM accounts WHERE owner=?", (owner,))
            conn.commit()
        finally:
            conn.close()


def update_account_courses(owner, courses_json):
    """更新绑定的课程列表缓存"""
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE accounts SET courses=?, updated_at=? WHERE owner=?",
                (courses_json, time.time(), owner),
            )
            conn.commit()
        finally:
            conn.close()


# ============================================================
# v14：用户体系（注册 / 登录 / 邀请码 / 额度）
# ============================================================
def _hash_password(password: str) -> str:
    """
    口令哈希：PBKDF2-HMAC-SHA256 + 随机盐。
    存储格式：pbkdf2$<迭代次数>$<盐hex>$<哈希hex>
    """
    import hashlib
    import secrets
    salt = secrets.token_bytes(16)
    iterations = 120_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令"""
    import hashlib
    import hmac
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def create_user(username, password, role="user", quota=0, banned=0):
    """创建用户；用户名重复返回 None"""
    username = (username or "").strip()
    if not username:
        return None
    with _write_lock:
        conn = get_conn()
        try:
            exists = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            if exists:
                return None
            conn.execute(
                """INSERT INTO users (username, password_hash, role, quota, banned, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (username, _hash_password(password), role, quota, banned, time.time()),
            )
            conn.commit()
            return username
        finally:
            conn.close()


def get_user(username):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_users():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, username, role, quota, banned, created_at, last_login FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def touch_user_login(username):
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("UPDATE users SET last_login=? WHERE username=?", (time.time(), username))
            conn.commit()
        finally:
            conn.close()


def set_user_banned(username, banned):
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("UPDATE users SET banned=? WHERE username=?", (1 if banned else 0, username))
            conn.commit()
        finally:
            conn.close()


def set_user_password(username, password):
    """修改密码（管理员重置 / 用户自己改）"""
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("UPDATE users SET password_hash=? WHERE username=?",
                         (_hash_password(password), username))
            conn.commit()
        finally:
            conn.close()


def count_users():
    """账号总数（用于判断是否需要首次初始化管理员）"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return row["c"] if row else 0
    finally:
        conn.close()


def delete_user(username):
    """删除用户，同时清理其绑定与任务数据"""
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM users WHERE username=?", (username,))
            conn.execute("DELETE FROM accounts WHERE owner=?", (username,))
            conn.execute("DELETE FROM user_courses WHERE username=?", (username,))
            conn.commit()
        finally:
            conn.close()


def add_user_quota(username, delta):
    """给用户增加额度（delta 可为负）；返回新的额度，-1 表示不限"""
    with _write_lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT quota FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                return None
            cur = row["quota"]
            if cur is None:
                cur = 0
            if cur < 0:  # 不限额度，加多少都还是不限
                return -1
            new = cur + int(delta)
            if new < 0:
                new = 0
            conn.execute("UPDATE users SET quota=? WHERE username=?", (new, username))
            conn.commit()
            return new
        finally:
            conn.close()


def set_user_quota(username, quota):
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("UPDATE users SET quota=? WHERE username=?", (int(quota), username))
            conn.commit()
        finally:
            conn.close()


# ---------- 邀请码 / 兑换码 ----------
def create_invite_code(code, type_="invite", quota=0, note="", created_by=""):
    """创建邀请码/兑换码；重复返回 None"""
    code = (code or "").strip().upper()
    if not code:
        return None
    with _write_lock:
        conn = get_conn()
        try:
            exists = conn.execute("SELECT 1 FROM invite_codes WHERE code=?", (code,)).fetchone()
            if exists:
                return None
            conn.execute(
                """INSERT INTO invite_codes (code, type, quota, note, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (code, type_, int(quota), note, created_by, time.time()),
            )
            conn.commit()
            return code
        finally:
            conn.close()


def list_invite_codes():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM invite_codes ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_invite_code(code):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM invite_codes WHERE code=?", ((code or "").strip().upper(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def consume_invite_code(code, username):
    """
    原子地消费一个邀请码。
    返回 (ok, type, quota, message)。已被使用则 ok=False。
    """
    code = (code or "").strip().upper()
    with _write_lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()
            if not row:
                return False, "", 0, "邀请码不存在"
            if row["used_by"]:
                return False, row["type"], row["quota"] or 0, "该邀请码已被使用"
            conn.execute(
                "UPDATE invite_codes SET used_by=?, used_at=? WHERE code=? AND (used_by IS NULL OR used_by='')",
                (username, time.time(), code),
            )
            conn.commit()
            return True, row["type"], row["quota"] or 0, "ok"
        finally:
            conn.close()


def delete_invite_code(code):
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM invite_codes WHERE code=?", ((code or "").strip().upper(),))
            conn.commit()
        finally:
            conn.close()


# ---------- 用户已刷课程（额度判定） ----------
def user_has_course(username, course_id):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM user_courses WHERE username=? AND course_id=?",
            (username, str(course_id)),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def mark_user_course(username, course_id):
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO user_courses (username, course_id, first_seen) VALUES (?, ?, ?)",
                (username, str(course_id), time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def list_user_courses(username):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT course_id, first_seen FROM user_courses WHERE username=? ORDER BY first_seen DESC",
            (username,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"数据库初始化完成: {DB_PATH}")
