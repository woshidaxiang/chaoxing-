# -*- coding: utf-8 -*-
"""回归测试：v14 登录体系 + 原有功能不回归

覆盖：初始化/登录/成员管理/隔离/越权/单课/默认跳过/暂停停止/日志/前端元素/
      统计口径/改密/停用删除/Cookie 账号隔离/线程健壮/每线程 session/worker 并发

用法（项目根目录）：
    .venv/Scripts/python.exe tests/regression_test.py
"""
import os
import sys
import json
import time
import threading

# 让脚本在项目根目录或 tests/ 目录下都能跑
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

_TMP = os.path.join(_ROOT, 'data', '_regress_tmp')
os.makedirs(_TMP, exist_ok=True)

OUT = open(os.path.join(_TMP, 'result.txt'), 'w', encoding='utf-8')


def _log(msg):
    print(msg, flush=True)
    OUT.write(str(msg) + '\n')
    OUT.flush()


_TEST_DB = os.path.join(_TMP, 'test_all.db')

# 【关键】必须在 import db / web_panel 之前设置环境并清库：
#   web_panel 模块级会 db.init_db()（读 CHAOXING_DB）；auth 读 PANEL_SECRET
os.environ['CHAOXING_DB'] = _TEST_DB
os.environ['PANEL_SECRET'] = 'regress-test-secret'
os.environ.pop('OWNER_CODES', None)
for _suf in ('', '-wal', '-shm'):
    try:
        os.remove(_TEST_DB + _suf)
    except OSError:
        pass

import web_panel as wp  # noqa: E402
import db  # noqa: E402

_ADMIN_PW = 'admin12345'

results = []


def check(name, cond):
    try:
        ok = bool(cond)
    except Exception as e:
        ok = False
        _log(f'  [异常] {name}: {e}')
    results.append((name, ok))


def step(title):
    _log(f'--- {title} ---')


def new_client():
    return wp.app.test_client()


def login(client, username, password):
    return client.post('/api/auth/login', json={'username': username, 'password': password})


c_anon = new_client()
c_admin = new_client()
c_alice = new_client()
c_bob = new_client()

try:
    # ---------- 1. 基础 ----------
    step('1. 基础')
    check('healthz 正常', c_anon.get('/healthz').get_json().get('ok') is True)
    check('首页可访问', c_anon.get('/').status_code == 200)

    # ---------- 2. 首次初始化 ----------
    step('2. 首次初始化（创建管理员）')
    check('空库时 need_setup=true', c_anon.get('/api/setup/status').get_json().get('need_setup') is True)
    r = c_anon.post('/api/setup', json={'username': 'admin', 'password': '123'})
    check('初始化密码太短被拒', r.get_json().get('success') is False)
    r = c_admin.post('/api/setup', json={'username': 'admin', 'password': _ADMIN_PW})
    check('创建管理员成功', r.get_json().get('success') is True)
    check('创建后 need_setup=false', c_anon.get('/api/setup/status').get_json().get('need_setup') is False)
    r = new_client().post('/api/setup', json={'username': 'hacker', 'password': '12345678'})
    check('已初始化后不能再 setup', r.get_json().get('success') is False)

    # ---------- 3. 登录 ----------
    step('3. 登录')
    check('错误密码登录失败',
          c_alice.post('/api/auth/login', json={'username': 'admin', 'password': 'wrong'}).get_json().get('success') is False)
    check('未登录访问 /api/current 返回 401', c_anon.get('/api/current').status_code == 401)
    r = login(c_admin, 'admin', _ADMIN_PW)
    check('管理员登录成功', r.get_json().get('success') is True)
    check('登录返回 role=admin', (r.get_json().get('user') or {}).get('role') == 'admin')
    check('/api/auth/me 正常', c_admin.get('/api/auth/me').get_json()['user']['username'] == 'admin')
    check('已登录访问 /api/current 返回 200', c_admin.get('/api/current').status_code == 200)

    # ---------- 4. 管理员开号 ----------
    step('4. 管理员开号')
    r = c_admin.post('/api/admin/users/create', json={'username': 'alice', 'password': 'alice123'})
    check('创建 alice', r.get_json().get('success') is True)
    r = c_admin.post('/api/admin/users/create', json={'username': 'bob', 'password': 'bob12345'})
    check('创建 bob', r.get_json().get('success') is True)
    check('重复用户名被拒',
          c_admin.post('/api/admin/users/create', json={'username': 'alice', 'password': 'alice123'}).get_json().get('success') is False)
    check('非法用户名被拒',
          c_admin.post('/api/admin/users/create', json={'username': 'a', 'password': 'alice123'}).get_json().get('success') is False)
    check('密码太短被拒',
          c_admin.post('/api/admin/users/create', json={'username': 'carol', 'password': '123'}).get_json().get('success') is False)

    check('alice 登录成功', login(c_alice, 'alice', 'alice123').get_json().get('success') is True)
    check('bob 登录成功', login(c_bob, 'bob', 'bob12345').get_json().get('success') is True)

    r = c_alice.get('/api/admin/users')
    check('普通成员访问成员管理 403', r.status_code == 403)
    check('普通成员创建成员 403',
          c_alice.post('/api/admin/users/create', json={'username': 'x', 'password': '123456'}).status_code == 403)

    users = c_admin.get('/api/admin/users').get_json()['users']
    names = [u['username'] for u in users]
    check('成员列表含 admin/alice/bob', set(['admin', 'alice', 'bob']).issubset(set(names)))
    check('成员列表带 role 字段', users[0].get('role') is not None)
    check('成员数=3', len(names) == 3)

    # ---------- 5. 任务归属隔离 / 越权 ----------
    step('5. 隔离与越权')
    r = c_alice.post('/api/start', json={
        'username': '13800000000', 'password': 'pw',
        'course_ids': ['111', '222'], 'speed': 1, 'jobs': 1})
    check('alice 建任务成功', r.get_json().get('success') is True)
    alice_task = db.get_latest_task(owner='alice')
    check('任务归属 alice', alice_task and alice_task['owner'] == 'alice')
    check('两门课被截断为 1 门', alice_task['courses'] == '111')

    tasks_alice = c_alice.get('/api/tasks').get_json()['tasks']
    check('alice 能看到自己的任务', len(tasks_alice) == 1)
    tasks_bob = c_bob.get('/api/tasks').get_json()['tasks']
    check('bob 看不到 alice 的任务', len(tasks_bob) == 0)

    r = c_bob.post('/api/stop', json={'task_id': alice_task['id']})
    check('bob 不能停止 alice 的任务', r.get_json().get('success') is False)
    r = c_bob.post('/api/task/delete', json={'task_id': alice_task['id']})
    check('bob 不能删除 alice 的任务', r.get_json().get('success') is False)
    r = c_bob.get(f"/api/logs?task_id={alice_task['id']}")
    check('bob 看不到 alice 的日志', len(r.get_json().get('logs') or []) == 0)

    # ---------- 6. 章节默认跳过 ----------
    step('6. 章节默认跳过')
    cfg = open(alice_task['config_path'], encoding='utf-8').read()
    check('默认 notopen_action=continue', 'notopen_action = continue' in cfg)
    db.update_task(alice_task['id'], status='finished', script_pid=0)

    c_alice.post('/api/start', json={
        'username': '13800000000', 'password': 'pw',
        'course_ids': ['111'], 'speed': 1, 'jobs': 1, 'notopen_action': 'xxx'})
    t2 = db.get_latest_task(owner='alice')
    cfg2 = open(t2['config_path'], encoding='utf-8').read()
    check('非法 notopen_action 回落 continue', 'notopen_action = continue' in cfg2)
    check('默认不使用题库', 'provider = ' in cfg2)
    db.update_task(t2['id'], status='finished', script_pid=0)

    # ---------- 7. 暂停 / 继续 / 停止 ----------
    step('7. 暂停/继续/停止')
    c_alice.post('/api/start', json={
        'username': '13800000000', 'password': 'pw', 'course_ids': ['111'], 'speed': 1, 'jobs': 1})
    tid = db.get_latest_task(owner='alice')['id']
    db.update_task(tid, status='running', script_pid=0)
    db.touch_heartbeat(tid)

    d = c_alice.get('/api/current').get_json()
    check('/api/current 含 paused 字段', 'paused' in d['task'])
    check('/api/current is_alive=true', d['task']['is_alive'] is True)

    r = c_alice.post('/api/pause', json={'task_id': tid})
    check('暂停接口成功', r.get_json().get('success') is True)
    check('暂停写入成功', db.is_paused(tid) is True)
    check('current 显示 paused', c_alice.get('/api/current').get_json()['task']['paused'] is True)

    r = c_alice.post('/api/resume', json={'task_id': tid})
    check('继续接口成功', r.get_json().get('success') is True)
    check('继续写入成功', db.is_paused(tid) is False)

    r = c_alice.post('/api/stop', json={'task_id': tid})
    check('停止接口成功', r.get_json().get('success') is True)
    check('停止标记写入', bool(db.is_stop_requested(tid)))
    db.update_task(tid, status='finished', script_pid=0, stop_requested=0, paused=0)

    # ---------- 8. 日志接口 ----------
    step('8. 日志接口')
    c_alice.post('/api/start', json={
        'username': '13800000000', 'password': 'pw', 'course_ids': ['111'], 'speed': 1, 'jobs': 1})
    log_tid = db.get_latest_task(owner='alice')['id']
    db.update_task(log_tid, status='finished', script_pid=0)
    for i in range(50):
        db.add_logs_bulk(log_tid, [(f'msg {i}', 'info')])
    dd = c_alice.get(f'/api/logs?task_id={log_tid}&tail=20').get_json()
    check('日志 tail 接口正常', 'logs' in dd and len(dd['logs']) == 20)

    # ---------- 9. 日志裁剪 ----------
    step('9. 日志自动裁剪')
    db.MAX_LOGS_PER_TASK = 10
    for i in range(30):
        db.add_logs_bulk(log_tid, [(f'trim {i}', 'info')])
    check('日志自动裁剪到上限', len(db.get_logs(log_tid, 0, 99999)) == 10)
    db.MAX_LOGS_PER_TASK = 1500

    # ---------- 10. 首页与前端元素 ----------
    step('10. 首页与前端元素')
    r = c_anon.get('/')
    check('首页 200', r.status_code == 200)
    check('首页禁止缓存', 'no-store' in (r.headers.get('Cache-Control') or ''))
    html = r.get_data(as_text=True)
    check('前端有登录输入框', 'id="loginUser"' in html and 'id="loginPass"' in html)
    check('前端有 doLogin', 'function doLogin' in html)
    check('前端已无访问码字样', '访问码' not in html)
    check('前端有暂停按钮', 'pauseBtn' in html)
    check('前端有停止按钮', 'stopBtn' in html)
    check('前端默认选中自动跳过', 'value="continue" selected' in html)
    check('前端有单选提示', '一次只能选一门课程' in html)
    check('前端有 renderStatusFromApi', 'renderStatusFromApi' in html)
    check('前端默认不使用题库', '不使用题库（默认）' in html)
    check('前端有成员管理卡', 'id="adminCard"' in html)
    check('前端有 loadMembers', 'function loadMembers' in html)
    check('前端有批量添加', 'function createMembersBatch' in html)
    check('前端有修改密码', 'function changePassword' in html)

    # ---------- 11. 统计口径 ----------
    step('11. 进度统计口径')
    db.add_logs_bulk(log_tid, [
        ('INFO | main:run:587 - 课程列表过滤完毕, 当前课程任务数量: 12', 'info'),
        ('INFO | main:worker:396 - Task success: A - 视频1', 'success'),
        ('INFO | main:worker:396 - Task success: A - 视频2', 'success'),
        ('ERROR | main:worker:424 - 任务重试次数达到上限: A - 视频9', 'error'),
        ('ERROR | main:worker:431 - 任务 Video 的状态无效 视频10', 'error'),
    ])
    st = db.get_log_stats(log_tid)
    check('统计任务点总数', st.get('total_points') == 12)
    check('统计成功数', st.get('success') == 2)
    check('统计失败数', st.get('failed') == 2)

    # ---------- 12. 修改密码 ----------
    step('12. 修改密码')
    r = c_alice.post('/api/auth/password', json={'old_password': 'wrong', 'new_password': 'alice999'})
    check('原密码错误被拒', r.get_json().get('success') is False)
    r = c_alice.post('/api/auth/password', json={'old_password': 'alice123', 'new_password': '123'})
    check('新密码太短被拒', r.get_json().get('success') is False)
    r = c_alice.post('/api/auth/password', json={'old_password': 'alice123', 'new_password': 'alice999'})
    check('修改密码成功', r.get_json().get('success') is True)
    check('旧密码不能登录', login(new_client(), 'alice', 'alice123').get_json().get('success') is False)
    check('新密码可以登录', login(new_client(), 'alice', 'alice999').get_json().get('success') is True)

    # ---------- 13. 停用 / 删除成员 ----------
    step('13. 停用/删除成员')
    db.create_task('u', 'c', 1.0, 1, 'x.ini', owner='bob')
    r = c_admin.post('/api/admin/users/toggle', json={'username': 'bob', 'banned': True})
    check('停用 bob 成功', r.get_json().get('success') is True)
    check('被停用后不能登录', login(new_client(), 'bob', 'bob12345').get_json().get('success') is False)
    check('被停用后原会话失效', c_bob.get('/api/current').status_code == 401)
    check('管理员不能停用自己',
          c_admin.post('/api/admin/users/toggle', json={'username': 'admin', 'banned': True}).get_json().get('success') is False)
    check('管理员不能删除自己',
          c_admin.post('/api/admin/users/delete', json={'username': 'admin'}).get_json().get('success') is False)
    r = c_admin.post('/api/admin/users/delete', json={'username': 'bob'})
    check('删除 bob 成功', r.get_json().get('success') is True)
    names2 = [u['username'] for u in c_admin.get('/api/admin/users').get_json()['users']]
    check('删除后列表不含 bob', 'bob' not in names2)
    check('删除后其绑定清理', db.get_account('bob') is None)

    # ---------- 14. 管理员重置成员密码 ----------
    step('14. 管理员重置密码')
    r = c_admin.post('/api/admin/users/password', json={'username': 'alice', 'password': 'reset001'})
    check('重置 alice 密码成功', r.get_json().get('success') is True)
    check('用重置后的密码能登录', login(new_client(), 'alice', 'reset001').get_json().get('success') is True)
    check('重置不存在的成员被拒',
          c_admin.post('/api/admin/users/password', json={'username': 'nobody', 'password': '123456'}).get_json().get('success') is False)

    # ---------- 15. 登出 ----------
    step('15. 登出')
    c_tmp = new_client()
    login(c_tmp, 'alice', 'reset001')
    check('登出前 /api/current 200', c_tmp.get('/api/current').status_code == 200)
    c_tmp.post('/api/auth/logout')
    check('登出后 /api/current 401', c_tmp.get('/api/current').status_code == 401)

except BaseException as e:
    import traceback
    _log('!!! 主体异常: ' + repr(e))
    _log(traceback.format_exc())

# ---------- 16. Cookie 多账号隔离 ----------
try:
    import tempfile
    import requests as _rq
    from api.config import GlobalConst as _gc
    import api.cookies as _ck

    _tmp = tempfile.mkdtemp()
    _orig_path = _gc.COOKIES_PATH
    _gc.COOKIES_PATH = os.path.join(_tmp, 'cookies.txt')

    _ck.set_current_account('13800000001')
    _sa = _rq.Session(); _sa.cookies.set('_uid', 'AAA111'); _ck.save_cookies(_sa)
    _ck.set_current_account('13800000002')
    _sb = _rq.Session(); _sb.cookies.set('_uid', 'BBB222'); _ck.save_cookies(_sb)
    _ck.set_current_account('13800000001'); _a = _ck.use_cookies()
    _ck.set_current_account('13800000002'); _b = _ck.use_cookies()

    check('Cookie 账号隔离: A 未被 B 覆盖', _a.get('_uid') == 'AAA111')
    check('Cookie 账号隔离: B 独立存放', _b.get('_uid') == 'BBB222')
    check('Cookie 按账号分文件', len([f for f in os.listdir(_tmp) if f.startswith('cookies.')]) == 2)

    _gc.COOKIES_PATH = _orig_path
    _ck.set_current_account('')
except BaseException as e:
    _log('!!! Cookie 隔离测试异常: ' + repr(e))

# ---------- 17. 线程健壮性（源码检查） ----------
try:
    _src = open(os.path.join(_ROOT, 'main.py'), encoding='utf-8').read()
    check('worker_thread 有异常兜底', '处理任务点异常' in _src)
    _bsrc = open(os.path.join(_ROOT, 'api', 'base.py'), encoding='utf-8').read()
    check('get_uid 有兜底逻辑', '从 Cookie 文件兜底读取 uid' in _bsrc)
    _csrc = open(os.path.join(_ROOT, 'api', 'cookies.py'), encoding='utf-8').read()
    check('Cookie 支持按账号隔离', 'set_current_account' in _csrc)
except BaseException as e:
    _log('!!! 线程健壮性测试异常: ' + repr(e))

# ---------- 18. 每线程独立 session ----------
try:
    from api.base import SessionManager as _SM
    _ids = {}

    def _w(i):
        _ids[i] = id(_SM.get_session())

    _ts = [threading.Thread(target=_w, args=(i,)) for i in range(4)]
    [_t.start() for _t in _ts]
    [_t.join() for _t in _ts]
    check('每线程独立 session', len(set(_ids.values())) == 4)

    _bsrc2 = open(os.path.join(_ROOT, 'api', 'base.py'), encoding='utf-8').read()
    check('SessionManager 使用 threading.local', '_tls' in _bsrc2)
    _cfgsrc = open(os.path.join(_ROOT, 'api', 'config.py'), encoding='utf-8').read()
    check('Cookie 路径落在共享卷', '/app/data/cookies.txt' in _cfgsrc)
except BaseException as e:
    _log('!!! 线程 session 测试异常: ' + repr(e))

# ---------- 19. worker 容器内并行 ----------
try:
    import signal as _sig
    import worker as W

    _wsrc = open(os.path.join(_ROOT, 'worker.py'), encoding='utf-8').read()
    check('worker 支持 MAX_CONCURRENT', 'MAX_CONCURRENT' in _wsrc)
    check('worker 有并发槽位', 'BoundedSemaphore' in _wsrc)

    _real_db = W.db
    _real_run = W.run_task
    _orig_term = _sig.getsignal(_sig.SIGTERM)
    _orig_int = _sig.getsignal(_sig.SIGINT)

    _pending = [{'id': i, 'config_path': 'x'} for i in range(1, 7)]
    _lock = threading.Lock()
    _active = []
    _peak = [0]

    class _FakeDB:
        def init_db(self):
            pass

        def mark_stale_interrupted(self, *a, **k):
            return []

        def requeue_task(self, *a, **k):
            pass

        def claim_next_pending(self):
            with _lock:
                return _pending.pop(0) if _pending else None

    def _fake_run(task):
        with _lock:
            _active.append(task['id'])
            _peak[0] = max(_peak[0], len(_active))
        time.sleep(0.4)
        with _lock:
            _active.remove(task['id'])

    try:
        W.db = _FakeDB()
        W.run_task = _fake_run
        W.MAX_CONCURRENT = 3
        W._stop = False
        _timer = threading.Timer(2.2, lambda: setattr(W, '_stop', True))
        _timer.start()
        W.main()
        _timer.cancel()
    finally:
        W.db = _real_db
        W.run_task = _real_run
        _sig.signal(_sig.SIGTERM, _orig_term)
        _sig.signal(_sig.SIGINT, _orig_int)

    check('容器内确实并行执行（峰值>=2）', _peak[0] >= 2)
    check('并发不超过 MAX_CONCURRENT', _peak[0] <= 3)
    _log(f'  [并发] 峰值 {_peak[0]} 个任务同时在跑（MAX_CONCURRENT=3）')
except BaseException as e:
    import traceback as _tb
    _log('!!! worker 并发测试异常: ' + repr(e))
    _log(_tb.format_exc())

# ---------- 输出 ----------
lines = []
lines.append('=' * 56)
lines.append('           回归测试报告')
lines.append('=' * 56)
for name, passed in results:
    lines.append(('  [PASS] ' if passed else '  [FAIL] ') + name)
lines.append('=' * 56)
failed = [n for n, p in results if not p]
lines.append(f'  共 {len(results)} 项，通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项')
if failed:
    lines.append('  失败项: ' + ', '.join(failed))
lines.append('=' * 56)

text = '\n'.join(lines)
_log('')
_log(text)
OUT.close()
sys.exit(1 if failed else 0)
