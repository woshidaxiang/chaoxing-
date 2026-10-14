#!/bin/bash
# ============================================================
# 超星刷课面板 - 停止服务
# ============================================================
cd "$(dirname "$0")" || exit 1
PID_DIR="$(pwd)/data"

stop_one() {
    local name="$1" file="$2"
    if [ -f "$file" ]; then
        local pid
        pid=$(cat "$file")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            sleep 1
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
            echo "✅ $name 已停止 (PID $pid)"
        else
            echo "ℹ️  $name 未在运行"
        fi
        rm -f "$file"
    else
        echo "ℹ️  $name 未在运行"
    fi
}

stop_one "Web 面板" "$PID_DIR/web.pid"
stop_one "Worker" "$PID_DIR/worker.pid"
echo ""
echo "注意：正在执行的刷课任务会被中断。历史记录仍保留在数据库中。"
