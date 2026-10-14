#!/bin/bash
# ============================================================
# 超星刷课面板 - 一键启动（非 Docker 方式）
# 启动两个进程：Web 控制面板 + 任务 Worker
# ============================================================
cd "$(dirname "$0")" || exit 1
BASE_DIR="$(pwd)"
PID_DIR="$BASE_DIR/data"
mkdir -p "$PID_DIR" "$BASE_DIR/data/logs" "$BASE_DIR/data/configs"

# ---- 选择 Python ----
if [ -x "$BASE_DIR/.venv/bin/python" ]; then
    PYTHON="$BASE_DIR/.venv/bin/python"
elif [ -x "$BASE_DIR/.venv/Scripts/python.exe" ]; then
    PYTHON="$BASE_DIR/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON="python"
else
    echo "❌ 未找到 Python，请先安装 Python 3.13+"
    exit 1
fi

echo "使用 Python: $PYTHON"

# ---- 检查依赖 ----
if ! "$PYTHON" -c "import flask" 2>/dev/null; then
    echo "📦 首次运行，正在安装依赖（请稍候）..."
    "$PYTHON" -m pip install -q -r requirements.txt
fi

# ---- 初始化数据库 ----
"$PYTHON" db.py

# ---- 启动 Worker ----
if [ -f "$PID_DIR/worker.pid" ] && kill -0 "$(cat "$PID_DIR/worker.pid")" 2>/dev/null; then
    echo "⚠️  Worker 已在运行 (PID $(cat "$PID_DIR/worker.pid"))"
else
    AUTO_RESUME=true MAX_CONCURRENT="${MAX_CONCURRENT:-3}" nohup "$PYTHON" worker.py > "$BASE_DIR/data/logs/worker.out" 2>&1 &
    echo $! > "$PID_DIR/worker.pid"
    echo "✅ Worker 已启动 (PID $!)"
fi

# ---- 启动 Web ----
if [ -f "$PID_DIR/web.pid" ] && kill -0 "$(cat "$PID_DIR/web.pid")" 2>/dev/null; then
    echo "⚠️  Web 面板已在运行 (PID $(cat "$PID_DIR/web.pid"))"
else
    nohup "$PYTHON" web_panel.py > "$BASE_DIR/data/logs/web.out" 2>&1 &
    echo $! > "$PID_DIR/web.pid"
    echo "✅ Web 面板已启动 (PID $!)"
fi

sleep 2
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$IP" ] && IP="<你的服务器IP>"

echo ""
echo "=================================================="
echo "  🎉 启动完成"
echo "  本机访问: http://localhost:5000"
echo "  手机访问: http://$IP:5000"
echo "=================================================="
echo "  停止服务: bash stop.sh"
echo "  查看日志: tail -f data/logs/web.out"
echo "=================================================="
