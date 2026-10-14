# -*- coding: utf-8 -*-
"""
超星学习通 Web 控制面板（持久化 + 双进程架构）
- 基于 Samueli924/chaoxing
- 任务与日志存入 SQLite，页面重开/服务重启后可恢复
- 任务由独立的 worker.py 执行，Web 重启不影响正在运行的刷课任务
- 通过心跳判断任务存活，不依赖进程 PID（跨容器也准确）
"""
import os
import re
import sys
import json
import time
import io
import signal
import subprocess
import configparser

from flask import (Flask, request, jsonify, Response, render_template,
                   stream_with_context, make_response)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import auth  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_DIR = os.path.join(DATA_DIR, "configs")
os.makedirs(CONFIG_DIR, exist_ok=True)

app = Flask(__name__)
db.init_db()

# ------------------------------------------------------------
# 救急开关：忘记管理员密码时，启动加 PANEL_ADMIN_RESET=1，
# 会把管理员密码重置成 PANEL_ADMIN_PASSWORD（默认 admin123456）。
# 登录后请立刻改密码，然后去掉这个环境变量。
# ------------------------------------------------------------
if os.environ.get("PANEL_ADMIN_RESET", "").strip().lower() in ("1", "true", "yes"):
    _au = (os.environ.get("PANEL_ADMIN_USER") or "admin").strip() or "admin"
    _ap = (os.environ.get("PANEL_ADMIN_PASSWORD") or "admin123456").strip() or "admin123456"
    if db.get_user(_au):
        db.set_user_password(_au, _ap)
        print(f"[提示] 已按 PANEL_ADMIN_RESET 重置管理员 {_au} 的密码，请登录后立刻修改", flush=True)
    else:
        # 指定的管理员名不存在（例如你当初把管理员起成了别的名字）：
        # 直接建一个，避免「忘记密码 + 名字不是 admin」时被彻底锁在门外。
        db.create_user(_au, _ap, role="admin")
        print(f"[提示] 管理员 {_au} 不存在，已按 PANEL_ADMIN_RESET 直接创建，请登录后立刻修改密码", flush=True)

# ============================================================
# 登录鉴权（v14：一人一个账号）
# 用「用户名 + 密码」登录，登录后只看到自己的任务 / 日志 / 绑定的超星账号。
# 账号由管理员在「成员管理」里创建（不开放自助注册）。
# ============================================================
TOKEN_COOKIE = "cx_token"
TOKEN_TTL_SECONDS = int(os.environ.get("PANEL_TOKEN_TTL", str(7 * 24 * 3600)))

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5]{2,20}$")
MIN_PASSWORD_LEN = 6

# 登录失败限流（内存计数，重启清零）：同一用户名 10 分钟内失败 5 次就暂时锁住
_login_fail = {}
LOGIN_FAIL_LIMIT = 5
LOGIN_FAIL_WINDOW = 600


def _validate_new_account(username, password):
    """校验新账号的用户名/密码，返回错误信息（空字符串 = 通过）"""
    if not USERNAME_RE.match(username or ""):
        return "用户名 2-20 位（中文、字母、数字、下划线）"
    if len(password or "") < MIN_PASSWORD_LEN:
        return f"密码至少 {MIN_PASSWORD_LEN} 位"
    return ""


def _set_login_cookie(resp, token):
    """把登录令牌写进 HttpOnly Cookie（同源的 fetch / EventSource 会自动带上）"""
    resp.set_cookie(TOKEN_COOKIE, token, max_age=TOKEN_TTL_SECONDS,
                    httponly=True, samesite="Lax", path="/")
    return resp


def _token_from_request():
    """依次从 Cookie、Authorization 头、查询参数取令牌
    （SSE/EventSource 无法自定义请求头，只能用 ?token=）"""
    t = request.cookies.get(TOKEN_COOKIE)
    if t:
        return t.strip()
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    return (request.args.get("token") or "").strip()


def _current_user():
    """返回当前登录用户 dict；未登录 / 已停用 / 已删除 返回 None"""
    username = auth.parse_token(_token_from_request())
    if not username:
        return None
    u = db.get_user(username)
    if not u or u.get("banned"):
        return None
    return u


def _require_user():
    """返回 (user, error_response)"""
    u = _current_user()
    if not u:
        return None, (jsonify({"success": False, "code": "UNAUTHORIZED",
                               "message": "请先登录"}), 401)
    return u, None


def _require_admin():
    """管理员专用：返回 (user, error_response)"""
    u, err = _require_user()
    if err:
        return None, err
    if (u.get("role") or "") != "admin":
        return None, (jsonify({"success": False, "code": "FORBIDDEN",
                               "message": "需要管理员权限"}), 403)
    return u, None


def _check_login():
    """各业务接口的鉴权入口：返回 (owner, error_response)，owner = 登录用户名"""
    u, err = _require_user()
    if err:
        return "", err
    return u["username"], None



# ============================================================
# 进程工具
# ============================================================
def is_pid_alive(pid):
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k = ctypes.windll.kernel32
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == STILL_ACTIVE
        else:
            os.kill(int(pid), 0)
            return True
    except Exception:
        return False


def kill_pid(pid):
    if not pid:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        else:
            os.kill(int(pid), signal.SIGTERM)
        return True
    except Exception:
        return False


def task_view(t):
    """把任务记录转成前端友好的结构"""
    alive = db.is_task_alive(t)
    return {
        "id": t["id"],
        "status": t["status"],
        "username": t.get("username", ""),
        "courses": t.get("courses", ""),
        "speed": t.get("speed", 1),
        "jobs": t.get("jobs", 4),
        "pid": t.get("script_pid") or t.get("worker_pid") or 0,
        "start_time": t.get("start_time"),
        "end_time": t.get("end_time"),
        "exit_code": t.get("exit_code"),
        "created_at": t.get("created_at"),
        "log_count": db.count_logs(t["id"]),
        "is_alive": alive,
        "paused": bool(t.get("paused", 0)),
    }


# ============================================================
# 页面
# ============================================================
@app.route("/")
def index():
    # 禁止浏览器缓存页面，避免更新代码后手机仍加载旧版（曾导致暂停按钮不显示）
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "time": time.time()})


# ============================================================
# 初始化（首次创建管理员）/ 登录 / 登出
# ============================================================
@app.route("/api/setup/status")
def api_setup_status():
    """系统里还没有任何账号 → 前端显示「创建管理员」"""
    return jsonify({"need_setup": db.count_users() == 0})


@app.route("/api/setup", methods=["POST"])
def api_setup():
    """首次初始化：创建管理员账号（只在系统没有任何账号时可用）"""
    if db.count_users() > 0:
        return jsonify({"success": False, "message": "系统已初始化，请直接登录"})
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    msg = _validate_new_account(username, password)
    if msg:
        return jsonify({"success": False, "message": msg})
    if not db.create_user(username, password, role="admin"):
        return jsonify({"success": False, "message": "创建失败，请换个用户名"})
    db.touch_user_login(username)
    resp = jsonify({"success": True, "message": "管理员账号已创建"})
    return _set_login_cookie(resp, auth.make_token(username))


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"success": False, "message": "请输入用户名和密码"})

    fails, first = _login_fail.get(username, (0, 0))
    if fails >= LOGIN_FAIL_LIMIT and time.time() - first < LOGIN_FAIL_WINDOW:
        return jsonify({"success": False, "message": "失败次数过多，请 10 分钟后再试"})

    u = db.get_user(username)
    if not u or not db.verify_password(password, u.get("password_hash") or ""):
        now = time.time()
        if now - first > LOGIN_FAIL_WINDOW:
            _login_fail[username] = (1, now)
        else:
            _login_fail[username] = (fails + 1, first)
        return jsonify({"success": False, "message": "用户名或密码错误"})

    if u.get("banned"):
        return jsonify({"success": False, "message": "账号已被停用，请联系管理员"})

    _login_fail.pop(username, None)
    db.touch_user_login(username)
    resp = jsonify({"success": True, "message": "登录成功",
                    "user": {"username": u["username"], "role": u.get("role") or "user"}})
    return _set_login_cookie(resp, auth.make_token(username))


@app.route("/api/auth/me")
def api_auth_me():
    u, err = _require_user()
    if err:
        return err
    return jsonify({"success": True,
                    "user": {"username": u["username"], "role": u.get("role") or "user"}})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    resp = jsonify({"success": True, "message": "已退出登录"})
    resp.delete_cookie(TOKEN_COOKIE)
    return resp


@app.route("/api/auth/password", methods=["POST"])
def api_auth_password():
    """修改自己的密码"""
    u, err = _require_user()
    if err:
        return err
    data = request.json or {}
    old = (data.get("old_password") or "").strip()
    new = (data.get("new_password") or "").strip()
    if not db.verify_password(old, u.get("password_hash") or ""):
        return jsonify({"success": False, "message": "原密码不正确"})
    if len(new) < MIN_PASSWORD_LEN:
        return jsonify({"success": False, "message": f"新密码至少 {MIN_PASSWORD_LEN} 位"})
    db.set_user_password(u["username"], new)
    return jsonify({"success": True, "message": "密码已修改"})



# ============================================================
# 登录并获取课程
# ============================================================
@app.route("/api/login", methods=["POST"])
def api_login():
    owner, err = _check_login()
    if err:
        return err
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    use_cookies = data.get("use_cookies", False)

    if not use_cookies and (not username or not password):
        return jsonify({"success": False, "message": "请输入手机号和密码"})

    try:
        from api.base import Chaoxing, Account
        account = Account(username, password)
        chaoxing = Chaoxing(account=account, tiku=None)

        login_result = chaoxing.login(login_with_cookies=use_cookies)
        if not login_result["status"]:
            return jsonify({"success": False, "message": f"登录失败: {login_result['msg']}"})

        courses = chaoxing.get_course_list()
        course_list = [{
            "courseId": str(c.get("courseId", "")),
            "clazzId": str(c.get("clazzId", "")),
            "title": c.get("title", "未知课程"),
            "cpi": str(c.get("cpi", "")),
        } for c in courses]

        db.save_courses(username, course_list)

        try:
            realname = chaoxing.get_name()
        except Exception:
            realname = ""

        # 登录成功即自动绑定到该访问码（一个访问码绑定一个超星账号）
        if owner and not use_cookies:
            db.save_account(owner, username, password, realname,
                            json.dumps(course_list, ensure_ascii=False))

        return jsonify({"success": True, "message": "登录成功",
                        "realname": realname, "courses": course_list})
    except Exception as e:
        import traceback
        return jsonify({"success": False,
                        "message": f"发生错误: {type(e).__name__}: {e}",
                        "trace": traceback.format_exc()})


# ============================================================
# 超星账号绑定
# ============================================================
@app.route("/api/account", methods=["GET"])
def api_account_get():
    owner, err = _check_login()
    if err:
        return err
    acc = db.get_account(owner)
    if not acc:
        return jsonify({"success": True, "bound": False})
    courses = []
    try:
        courses = json.loads(acc.get("courses") or "[]")
    except Exception:
        courses = []
    return jsonify({
        "success": True,
        "bound": True,
        "account": {
            "username": acc.get("cx_username", ""),
            "realname": acc.get("realname", ""),
            "updated_at": acc.get("updated_at", 0),
        },
        "courses": courses,
    })


@app.route("/api/account/bind", methods=["POST"])
def api_account_bind():
    owner, err = _check_login()
    if err:
        return err
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"success": False, "message": "请输入超星账号和密码"})

    try:
        from api.base import Chaoxing, Account
        account = Account(username, password)
        chaoxing = Chaoxing(account=account, tiku=None)
        result = chaoxing.login(login_with_cookies=False)
        if not result["status"]:
            return jsonify({"success": False, "message": f"登录失败：{result['msg']}"})

        courses = chaoxing.get_course_list()
        course_list = [{
            "courseId": str(c.get("courseId", "")),
            "clazzId": str(c.get("clazzId", "")),
            "title": c.get("title", "未知课程"),
            "cpi": str(c.get("cpi", "")),
        } for c in courses]
        try:
            realname = chaoxing.get_name()
        except Exception:
            realname = ""

        db.save_account(owner, username, password, realname,
                        json.dumps(course_list, ensure_ascii=False))
        return jsonify({"success": True, "message": "绑定成功",
                        "realname": realname, "courses": course_list})
    except Exception as e:
        import traceback
        return jsonify({"success": False,
                        "message": f"绑定失败：{type(e).__name__}: {e}",
                        "trace": traceback.format_exc()})


@app.route("/api/account/unbind", methods=["POST"])
def api_account_unbind():
    owner, err = _check_login()
    if err:
        return err
    db.delete_account(owner)
    return jsonify({"success": True, "message": "已解绑"})


# ============================================================
# 创建任务（入队，由 worker 执行）
# ============================================================
@app.route("/api/start", methods=["POST"])
def api_start():
    owner, err = _check_login()
    if err:
        return err

    data = request.json or {}

    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    use_cookies = data.get("use_cookies", False)
    course_ids = data.get("course_ids", [])

    # 没传账号则用已绑定的超星账号
    if not use_cookies and (not username or not password):
        acc = db.get_account(owner, with_password=True)
        if acc:
            username = acc.get("cx_username", "")
            password = acc.get("cx_password", "")

    # 没选课则用绑定的全部课程（否则脚本会进入交互式输入而卡死）
    if not course_ids:
        acc2 = db.get_account(owner)
        if acc2:
            try:
                course_ids = [c["courseId"] for c in json.loads(acc2.get("courses") or "[]")]
            except Exception:
                course_ids = []
    if not course_ids:
        return jsonify({"success": False, "message": "请至少选择一门课程"})
    # 强制一次只能刷一门课程（防止多课并发出错）
    if isinstance(course_ids, list) and len(course_ids) > 1:
        course_ids = course_ids[:1]
    speed = float(data.get("speed", 1))
    jobs = int(data.get("jobs", 4))
    notopen_action = data.get("notopen_action", "continue")
    if notopen_action not in ("retry", "ask", "continue"):
        notopen_action = "continue"

    tiku_provider = data.get("tiku_provider", "")
    tiku_tokens = data.get("tiku_tokens", "")
    tiku_submit = data.get("tiku_submit", False)
    tiku_cover_rate = data.get("tiku_cover_rate", 0.9)
    ai_endpoint = data.get("ai_endpoint", "")
    ai_key = data.get("ai_key", "")
    ai_model = data.get("ai_model", "")
    siliconflow_key = data.get("siliconflow_key", "")
    siliconflow_model = data.get("siliconflow_model", "deepseek-ai/DeepSeek-R1")
    tiku_adapter_url = data.get("tiku_adapter_url", "")
    notify_provider = data.get("notify_provider", "")
    notify_url = data.get("notify_url", "")

    if not use_cookies and (not username or not password):
        return jsonify({"success": False, "message": "请输入手机号和密码"})

    # 已有活跃任务则拒绝（只检查自己的任务）
    for t in db.get_active_tasks(owner=owner or None):
        if db.is_task_alive(t):
            return jsonify({"success": False, "message": f"任务 #{t['id']} 正在运行，请先停止"})

    # 生成配置
    config = configparser.ConfigParser()
    config["common"] = {
        "use_cookies": str(use_cookies).lower(),
        "username": username,
        "password": password,
        "course_list": ",".join(course_ids) if course_ids else "",
        "speed": str(speed),
        "jobs": str(jobs),
        "notopen_action": notopen_action,
        "retry_interval": "1.0",
        "work_redo_enabled": "false",
        "work_max_retries": "3",
        "add_learning_count": "false",
        "target_count": "100",
    }
    tiku_config = {
        # 默认「不使用题库」：没题库/AI 时若默认言溪，会在章节测验处反复查询失败刷屏报错
        "provider": tiku_provider if tiku_provider else "",
        "check_llm_connection": "false",
        # 不使用题库时强制关闭提交（没有答案可交）
        "submit": str(bool(tiku_submit) and bool(tiku_provider)).lower(),
        "cover_rate": str(tiku_cover_rate),
        "delay": "1.0",
        "tokens": tiku_tokens,
        "manual_mode_default": "batch",
        "manual_mode_separator": ";",
        "true_list": "正确,对,√,是",
        "false_list": "错误,错,×,否,不对,不正确",
    }
    if ai_endpoint:
        tiku_config["endpoint"] = ai_endpoint
    if ai_key:
        tiku_config["key"] = ai_key
    if ai_model:
        tiku_config["model"] = ai_model
    if siliconflow_key:
        tiku_config["siliconflow_key"] = siliconflow_key
    if siliconflow_model:
        tiku_config["siliconflow_model"] = siliconflow_model
    if tiku_adapter_url:
        tiku_config["url"] = tiku_adapter_url
    config["tiku"] = tiku_config
    config["notification"] = {
        "provider": notify_provider if notify_provider else "ServerChan",
        "url": notify_url,
    }

    buf = io.StringIO()
    config.write(buf)

    task_id = db.create_task(username=username, courses=",".join(course_ids),
                             speed=speed, jobs=jobs, config_path="", owner=owner)
    config_path = os.path.join(CONFIG_DIR, f"task_{task_id}.ini")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    db.update_task(task_id, config_path=config_path)

    db.add_log(task_id, f"[系统] 任务 #{task_id} 已创建并入队", "info")
    db.add_log(task_id, "[系统] 等待 Worker 认领执行（通常 1-2 秒内开始）", "info")

    return jsonify({"success": True, "message": "任务已入队", "task_id": task_id})


# ============================================================
# 停止任务
# ============================================================
@app.route("/api/stop", methods=["POST"])
def api_stop():
    owner, err = _check_login()
    if err:
        return err

    data = request.json or {}
    task_id = data.get("task_id")

    task = _get_own_task(task_id, owner)
    if not task:
        return jsonify({"success": False, "message": "没有找到任务"})

    if not db.is_task_alive(task) and task["status"] not in ("pending",):
        return jsonify({"success": False, "message": "任务已经结束"})

    # 1) 标记停止请求（worker 会读取）
    db.request_stop(task["id"])
    db.add_log(task["id"], "[系统] 已发送停止请求...", "warning")

    # 2) 若知道脚本 PID，直接终止以加速
    spid = task.get("script_pid")
    if spid and is_pid_alive(spid):
        kill_pid(spid)

    # 若还在 pending（worker 尚未认领），直接标记停止
    if task["status"] == "pending":
        db.update_task(task["id"], status="stopped", end_time=time.time())

    return jsonify({"success": True, "message": "停止请求已发送"})


# ============================================================
# 暂停 / 继续
# ============================================================
def _get_own_task(task_id, owner):
    """取任务并校验归属（登录后只能操作自己的任务）"""
    task = db.get_task(task_id) if task_id else db.get_latest_task(owner=owner or None)
    if not task:
        return None
    if (task.get("owner") or "") != owner:
        return None
    return task


def _active_task_or_none(task_id, owner):
    task = _get_own_task(task_id, owner)
    if not task:
        return None
    if not db.is_task_alive(task):
        return None
    return task


@app.route("/api/pause", methods=["POST"])
def api_pause():
    owner, err = _check_login()
    if err:
        return err
    data = request.json or {}
    task = _active_task_or_none(data.get("task_id"), owner)
    if not task:
        return jsonify({"success": False, "message": "没有正在运行的任务"})

    db.set_paused(task["id"], True)
    db.add_log(task["id"], "[系统] 收到暂停请求...", "warning")
    return jsonify({"success": True, "message": "已暂停", "paused": True})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    owner, err = _check_login()
    if err:
        return err
    data = request.json or {}
    task = _active_task_or_none(data.get("task_id"), owner)
    if not task:
        return jsonify({"success": False, "message": "没有正在运行的任务"})

    db.set_paused(task["id"], False)
    db.add_log(task["id"], "[系统] 收到继续请求...", "info")
    return jsonify({"success": True, "message": "已继续", "paused": False})


# ============================================================
# 当前/最近任务
# ============================================================
@app.route("/api/current", methods=["GET"])
def api_current():
    owner, err = _check_login()
    if err:
        return err
    task = db.get_latest_task(owner=owner or None)
    if not task:
        return jsonify({"has_task": False})
    return jsonify({"has_task": True, "task": task_view(task)})


# ============================================================
# 任务历史
# ============================================================
@app.route("/api/tasks", methods=["GET"])
def api_tasks():
    owner, err = _check_login()
    if err:
        return err
    limit = int(request.args.get("limit", 30))
    return jsonify({"tasks": [task_view(t) for t in db.list_tasks(limit=limit, owner=owner or None)]})


# ============================================================
# 日志（增量拉取）
# ============================================================
@app.route("/api/logs", methods=["GET"])
def api_logs():
    owner, err = _check_login()
    if err:
        return err
    task_id = request.args.get("task_id", type=int)
    after_id = request.args.get("after", default=0, type=int)
    tail = request.args.get("tail", default=0, type=int)

    if not task_id:
        task = db.get_latest_task(owner=owner or None)
        if not task:
            return jsonify({"logs": [], "last_id": 0})
        task_id = task["id"]
    else:
        # 校验归属，防止看别人的日志
        if not _get_own_task(task_id, owner):
            return jsonify({"logs": [], "last_id": 0})

    if tail:
        # 只返回最后 N 条（页面首次加载用，防止数据量过大卡死手机）
        logs = db.get_logs_tail(task_id, limit=min(tail, 500))
    else:
        logs = db.get_logs(task_id, after_id=after_id, limit=300)

    last_id = logs[-1]["id"] if logs else after_id
    return jsonify({
        "task_id": task_id,
        "logs": [{
            "id": l["id"],
            "time": time.strftime("%H:%M:%S", time.localtime(l["ts"])),
            "msg": l["message"],
            "level": l["level"],
        } for l in logs],
        "last_id": last_id,
    })


# ============================================================
# 实时日志流（SSE）
# ============================================================
@app.route("/api/logs/stream")
def api_logs_stream():
    owner, err = _check_login()
    if err:
        return err
    task_id = request.args.get("task_id", type=int)

    def generate():
        if not task_id:
            task = db.get_latest_task(owner=owner or None)
            tid = task["id"] if task else None
        else:
            tid = task_id if _get_own_task(task_id, owner) else None

        if not tid:
            yield f"data: {json.dumps({'type': 'no_task'})}\n\n"
            return

        last_id = 0
        tick = 0
        while True:
            logs = db.get_logs(tid, after_id=last_id, limit=200)
            if logs:
                for l in logs:
                    yield "data: " + json.dumps({
                        "type": "log", "id": l["id"],
                        "time": time.strftime("%H:%M:%S", time.localtime(l["ts"])),
                        "msg": l["message"], "level": l["level"],
                    }, ensure_ascii=False) + "\n\n"
                last_id = logs[-1]["id"]

            tick += 1
            if tick % 4 == 0:
                task = db.get_task(tid)
                if task:
                    yield "data: " + json.dumps({
                        "type": "status",
                        "status": task["status"],
                        "is_alive": db.is_task_alive(task),
                        "paused": bool(task.get("paused", 0)),
                    }, ensure_ascii=False) + "\n\n"
            time.sleep(0.5)

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


# ============================================================
# 任务进度（供任务状态卡显示）
# ============================================================
def _strip_log_prefix(msg):
    """去掉 loguru 前缀，只留正文"""
    if not msg:
        return ""
    if " - " in msg:
        return msg.split(" - ", 1)[1].strip()
    return msg.strip()


@app.route("/api/progress", methods=["GET"])
def api_progress():
    owner, err = _check_login()
    if err:
        return err
    task_id = request.args.get("task_id", type=int)
    task = _get_own_task(task_id, owner)
    if not task:
        return jsonify({"success": False, "message": "没有任务"})
    tid = task["id"]
    stats = db.get_log_stats(tid)
    cur_chapter = _strip_log_prefix(db.get_last_message(tid, "%当前章节%"))
    cur_task = _strip_log_prefix(db.get_last_message(tid, "%开始任务%"))
    return jsonify({
        "success": True,
        "task_id": tid,
        "stats": stats,
        "current_chapter": cur_chapter.replace("当前章节:", "").strip(),
        "current_task": cur_task,
    })


# ============================================================
# 删除 / 清空
# ============================================================
@app.route("/api/task/delete", methods=["POST"])
def api_task_delete():
    owner, err = _check_login()
    if err:
        return err
    data = request.json or {}
    task_id = data.get("task_id")
    if not task_id:
        return jsonify({"success": False, "message": "缺少 task_id"})
    task = _get_own_task(task_id, owner)
    if not task:
        return jsonify({"success": False, "message": "任务不存在或不属于你"})
    if db.is_task_alive(task):
        return jsonify({"success": False, "message": "任务正在运行，请先停止"})
    db.delete_task(task_id)
    return jsonify({"success": True, "message": "已删除"})


@app.route("/api/tasks/clear", methods=["POST"])
def api_tasks_clear():
    owner, err = _check_login()
    if err:
        return err
    for t in db.get_active_tasks(owner=owner or None):
        if db.is_task_alive(t):
            return jsonify({"success": False, "message": f"任务 #{t['id']} 正在运行，请先停止"})
    db.clear_all(owner=owner or None)
    return jsonify({"success": True, "message": "历史已清空"})


# ============================================================
# 管理员：成员管理（开号 / 改密 / 停用 / 删除）
# ============================================================
def _public_user(u):
    return {
        "id": u.get("id"),
        "username": u["username"],
        "role": u.get("role") or "user",
        "banned": int(u.get("banned") or 0),
        "created_at": u.get("created_at") or 0,
        "last_login": u.get("last_login") or 0,
    }


@app.route("/api/admin/users", methods=["GET"])
def api_admin_users():
    _, err = _require_admin()
    if err:
        return err
    users = []
    for u in db.list_users():
        pu = _public_user(u)
        acc = db.get_account(u["username"]) or {}
        pu["cx_username"] = acc.get("cx_username", "")
        users.append(pu)
    return jsonify({"success": True, "users": users})


@app.route("/api/admin/users/create", methods=["POST"])
def api_admin_user_create():
    _, err = _require_admin()
    if err:
        return err
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    msg = _validate_new_account(username, password)
    if msg:
        return jsonify({"success": False, "message": msg})
    if not db.create_user(username, password, role="user"):
        return jsonify({"success": False, "message": "该用户名已存在"})
    return jsonify({"success": True, "message": f"已创建成员 {username}"})


@app.route("/api/admin/users/password", methods=["POST"])
def api_admin_user_password():
    _, err = _require_admin()
    if err:
        return err
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not db.get_user(username):
        return jsonify({"success": False, "message": "成员不存在"})
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({"success": False, "message": f"密码至少 {MIN_PASSWORD_LEN} 位"})
    db.set_user_password(username, password)
    return jsonify({"success": True, "message": f"{username} 的密码已重置"})


@app.route("/api/admin/users/toggle", methods=["POST"])
def api_admin_user_toggle():
    me, err = _require_admin()
    if err:
        return err
    data = request.json or {}
    username = (data.get("username") or "").strip()
    if username == me["username"]:
        return jsonify({"success": False, "message": "不能停用自己"})
    if not db.get_user(username):
        return jsonify({"success": False, "message": "成员不存在"})
    banned = 1 if data.get("banned") else 0
    db.set_user_banned(username, banned)
    return jsonify({"success": True, "message": "已停用" if banned else "已启用"})


@app.route("/api/admin/users/delete", methods=["POST"])
def api_admin_user_delete():
    me, err = _require_admin()
    if err:
        return err
    data = request.json or {}
    username = (data.get("username") or "").strip()
    if username == me["username"]:
        return jsonify({"success": False, "message": "不能删除自己"})
    if not db.get_user(username):
        return jsonify({"success": False, "message": "成员不存在"})
    db.delete_user(username)
    return jsonify({"success": True, "message": f"已删除成员 {username}"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"\n{'=' * 52}")
    print(f"  超星学习通 Web 控制面板")
    print(f"  数据库: {db.DB_PATH}")
    print(f"  访问: http://0.0.0.0:{port}")
    print(f"{'=' * 52}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
