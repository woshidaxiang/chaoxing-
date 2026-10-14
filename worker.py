# -*- coding: utf-8 -*-
"""
任务 Worker 守护进程
- 长期运行，循环从数据库认领 pending 任务并执行
- 执行刷课脚本 main.py，实时把日志写入数据库
- 定期更新心跳，Web 端据此判断任务是否在跑
- 与 Web 服务完全解耦：重启 Web 不影响正在执行的任务

用法: python worker.py
环境变量:
  AUTO_RESUME=true   启动时自动续跑上次被中断的任务（默认 false）
"""
import os
import sys
import re
import time
import signal
import subprocess
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "data", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

AUTO_RESUME = os.environ.get("AUTO_RESUME", "false").lower() in ("1", "true", "yes")

# tqdm 进度条特征（如 "76%|███████▌ | 06:57/09:09"），每 0.1 秒刷新一次，必须过滤
_PROGRESS_RE = re.compile(r"\d+%\|")
# loguru 日志级别特征（形如 "2026-10-13 14:41:00.123 | DEBUG    | ..."）
_LEVEL_RE = re.compile(r"\|\s*(TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)\s*\|")
# 进度条最多每 N 秒保留一条
PROGRESS_KEEP_INTERVAL = float(os.environ.get("PROGRESS_KEEP_INTERVAL", "10"))
# 是否丢弃 DEBUG/TRACE 级别日志（默认丢，避免心跳/进度上报刷屏）
DROP_DEBUG_LOG = os.environ.get("DROP_DEBUG_LOG", "true").lower() in ("1", "true", "yes")

_stop = False
# 支持同一容器内并行执行多个任务：task_id -> Popen
_procs = {}
_procs_lock = threading.Lock()

# 同一容器内最多同时执行几个任务（总并发 = 各 worker 容器的 MAX_CONCURRENT 之和）
# 说明：真正卡上限的是服务器内存和超星风控，不建议调太大（3~5 以内）。
MAX_CONCURRENT = max(1, int(os.environ.get("MAX_CONCURRENT", "1")))


def _handle_signal(signum, frame):
    global _stop
    _stop = True
    with _procs_lock:
        for p in list(_procs.values()):
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass


def classify_level(msg):
    low = msg.lower()
    if "错误" in msg or "error" in low or "exception" in low or "traceback" in low:
        return "error"
    if "警告" in msg or "warning" in low:
        return "warning"
    if "成功" in msg or "完成" in msg or "success" in low:
        return "success"
    return "info"


def _pause_monitor(task_id, proc, stop_event):
    """
    独立线程：轮询数据库的 paused 标记，用 SIGSTOP / SIGCONT 暂停或恢复脚本进程。
    必须独立于主循环——因为脚本被暂停后不再输出，主循环会阻塞在 readline 上，
    没法再去检查"是否要恢复"。
    暂停期间持续上报心跳，避免被判定为任务失联。
    """
    if not hasattr(signal, "SIGSTOP"):
        return  # Windows 不支持，直接返回
    paused = False
    while not stop_event.is_set():
        try:
            if proc.poll() is not None:
                break
            want_pause = db.is_paused(task_id)
            if want_pause and not paused:
                os.kill(proc.pid, signal.SIGSTOP)
                paused = True
                db.add_log(task_id, "[系统] ⏸ 任务已暂停（继续后可接着跑，不会丢进度）", "warning")
            elif not want_pause and paused:
                os.kill(proc.pid, signal.SIGCONT)
                paused = False
                db.add_log(task_id, "[系统] ▶ 任务已继续", "info")
            # 暂停期间保持心跳
            if paused:
                db.touch_heartbeat(task_id)
        except Exception:
            pass
        stop_event.wait(1.5)
    # 退出前确保进程已恢复，避免留下被冻结的进程
    if paused:
        try:
            os.kill(proc.pid, signal.SIGCONT)
        except Exception:
            pass


def run_task(task):
    """执行单个任务，阻塞直到结束（可由多个线程并行调用，各自独立进程/心跳/暂停）"""
    task_id = task["id"]
    config_path = task.get("config_path")

    if not config_path or not os.path.exists(config_path):
        db.add_log(task_id, f"[系统错误] 配置文件不存在: {config_path}", "error")
        db.update_task(task_id, status="error", end_time=time.time(), exit_code=-1)
        return

    db.add_log(task_id, "[系统] Worker 已认领任务，开始执行", "info")

    log_file_path = os.path.join(LOG_DIR, f"task_{task_id}.log")
    log_file = open(log_file_path, "a", encoding="utf-8", buffering=1)

    main_py = os.path.join(BASE_DIR, "main.py")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

    _pause_stop = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", main_py, "-c", config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=BASE_DIR,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        with _procs_lock:
            _procs[task_id] = proc
        db.update_task(task_id, script_pid=proc.pid)
        db.add_log(task_id, f"[系统] 刷课脚本已启动 (PID: {proc.pid})", "info")

        # 启动暂停/继续监控线程
        _pause_stop = threading.Event()
        threading.Thread(target=_pause_monitor, args=(task_id, proc, _pause_stop), daemon=True).start()

        buffer = []
        last_flush = time.time()
        last_hb = time.time()
        last_stop_check = time.time()

        # 用于折叠超星返回的 HTML 错误页（一次 403 会输出几十行 HTML，会撑爆日志和手机浏览器）
        in_html_block = False
        html_line_count = 0
        last_progress_ts = 0.0

        for line in proc.stdout:
            line = line.rstrip("\n\r")
            if not line:
                continue

            low = line.lower()

            # ---- 过滤 DEBUG/TRACE 级别日志（心跳、进度上报等，每秒刷屏）----
            # 完整内容仍写在 data/logs/task_N.log 文件里，排查时可用
            log_file.write(line + "\n")
            if DROP_DEBUG_LOG:
                _m = _LEVEL_RE.search(line)
                if _m and _m.group(1) in ("TRACE", "DEBUG"):
                    continue

            # ---- 过滤 tqdm 进度条刷新（每 0.1 秒一条，多章节并发时会刷屏）----
            if _PROGRESS_RE.search(line):
                now_p = time.time()
                if now_p - last_progress_ts < PROGRESS_KEEP_INTERVAL:
                    continue  # 丢弃这条进度刷新
                last_progress_ts = now_p

            # ---- 折叠 HTML 错误页 ----
            if not in_html_block and ("<!doctype html>" in low or "<!doctype html" in low):
                in_html_block = True
                html_line_count = 0
                buffer.append(("[已折叠] 超星返回 HTML 错误页（通常是 403 无权限），完整内容已存入日志文件", "error"))
                continue
            if in_html_block:
                html_line_count += 1
                # 遇到 </html> 或超过 300 行（防止未闭合时卡死）就退出折叠
                if "</html>" in low or html_line_count > 300:
                    in_html_block = False
                    html_line_count = 0
                continue

            # ---- 普通日志 ----
            # 超长行截断，避免单行几 KB 撑爆前端
            if len(line) > 400:
                line = line[:400] + " …(已截断)"
            buffer.append((line, classify_level(line)))

            now = time.time()
            if len(buffer) >= 20 or (now - last_flush) >= 0.5:
                db.add_logs_bulk(task_id, buffer)
                buffer = []
                last_flush = now

            if now - last_hb >= 5:
                db.touch_heartbeat(task_id)
                last_hb = now

            # 每 2 秒检查一次停止请求
            if now - last_stop_check >= 2:
                last_stop_check = now
                if db.is_stop_requested(task_id):
                    db.add_log(task_id, "[系统] 收到停止请求，正在终止脚本...", "warning")
                    proc.terminate()
                    break

            if _stop and proc.poll() is None:
                proc.terminate()
                break

        if buffer:
            db.add_logs_bulk(task_id, buffer)

        retcode = proc.wait()

        if _stop or db.is_stop_requested(task_id):
            db.add_log(task_id, "[系统] 任务已被停止", "warning")
            db.update_task(task_id, status="stopped", end_time=time.time(),
                           exit_code=retcode, script_pid=0, stop_requested=0, paused=0)
        elif retcode == 0:
            db.add_log(task_id, "[系统] ✅ 所有课程学习任务已完成", "success")
            db.update_task(task_id, status="finished", end_time=time.time(),
                           exit_code=0, script_pid=0, paused=0)
        else:
            db.add_log(task_id, f"[系统] ❌ 任务异常退出，返回码: {retcode}", "error")
            db.update_task(task_id, status="error", end_time=time.time(),
                           exit_code=retcode, script_pid=0, paused=0)

    except Exception as e:
        err = f"[系统错误] {type(e).__name__}: {e}\n{traceback.format_exc()}"
        db.add_log(task_id, err, "error")
        db.update_task(task_id, status="error", end_time=time.time(), exit_code=-1,
                       script_pid=0, paused=0)
        try:
            log_file.write(err + "\n")
        except Exception:
            pass
    finally:
        with _procs_lock:
            _procs.pop(task_id, None)
        # 停掉暂停监控线程（其中会确保被冻结的进程恢复）
        if _pause_stop is not None:
            _pause_stop.set()
        try:
            log_file.close()
        except Exception:
            pass


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _handle_signal)

    db.init_db()

    # 启动时：清理失联任务
    stale = db.mark_stale_interrupted()
    for t in stale:
        db.add_log(t["id"], "[系统] 检测到任务中断（Worker 重启或异常退出）", "warning")
        if AUTO_RESUME:
            db.add_log(t["id"], "[系统] AUTO_RESUME 已开启，任务重新排队续跑", "info")
            db.requeue_task(t["id"])

    print(f"[Worker] 已启动 (PID: {os.getpid()})，最多同时执行 {MAX_CONCURRENT} 个任务，"
          f"等待任务... AUTO_RESUME={AUTO_RESUME}", flush=True)

    # 并发槽位：攒够空闲才认领新任务；空闲任务由数据库原子认领，多容器同时跑也不会重复
    slots = threading.BoundedSemaphore(MAX_CONCURRENT)
    running = []
    last_reap = time.time()

    while not _stop:
        try:
            # 周期性清理失联任务（例如 worker 被杀）
            now = time.time()
            if now - last_reap >= 10:
                db.mark_stale_interrupted()
                last_reap = now

            running = [t for t in running if t.is_alive()]

            if not slots.acquire(timeout=1.5):
                continue  # 槽位已满，等一会儿再看
            task = db.claim_next_pending()
            if not task:
                slots.release()
                time.sleep(1.5)
                continue

            running = [t for t in running if t.is_alive()]
            print(f"[Worker] 认领任务 #{task['id']}"
                  f"（并行 {len(running) + 1}/{MAX_CONCURRENT}）", flush=True)

            def _run(t=task):
                try:
                    run_task(t)
                except Exception as e:
                    print(f"[Worker] 任务 #{t['id']} 执行异常: {e}", flush=True)
                    traceback.print_exc()
                finally:
                    slots.release()
                    print(f"[Worker] 任务 #{t['id']} 结束", flush=True)

            th = threading.Thread(target=_run, daemon=True)
            th.start()
            running.append(th)
        except Exception as e:
            print(f"[Worker] 循环异常: {e}", flush=True)
            traceback.print_exc()
            time.sleep(3)

    print("[Worker] 收到退出信号，等待正在执行的任务收尾...", flush=True)
    for t in running:
        t.join(timeout=30)
    print("[Worker] 已退出", flush=True)


if __name__ == "__main__":
    main()
