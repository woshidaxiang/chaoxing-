#!/bin/bash
# ============================================================
#  超星刷课面板 — 服务器一键部署脚本
#  用法：把项目上传到服务器后，在项目目录执行：
#        bash deploy.sh
#  脚本会自动：装 Docker → 配置镜像加速 → 构建 → 启动 → 显示访问地址
# ============================================================
set -e
cd "$(dirname "$0")" || exit 1
BASE_DIR="$(pwd)"

# ---------------- 可调参数 ----------------
# 镜像名：带版本号，方便和旧镜像并存 / 回退（不要用 latest）
IMAGE="${IMAGE:-chaoxing-web:v14}"
# 单容器内最多同时执行几个任务（总并发 = 所有 worker 的该值之和）
MAX_CONCURRENT="${MAX_CONCURRENT:-3}"
# 改并发示例：  MAX_CONCURRENT=5 bash deploy.sh
# 改端口示例：  IMAGE=chaoxing-web:myver PORT=8080 bash deploy.sh
PORT="${PORT:-5000}"

# 颜色
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;36m'; N='\033[0m'
info(){ echo -e "${B}[信息]${N} $1"; }
ok(){   echo -e "${G}[成功]${N} $1"; }
warn(){ echo -e "${Y}[提示]${N} $1"; }
err(){  echo -e "${R}[错误]${N} $1"; }

echo ""
echo "======================================================"
echo "       超星刷课面板 — 一键部署"
echo "======================================================"
echo ""

# ---------- 0. 权限检查 ----------
if [ "$(id -u)" != "0" ]; then
    warn "建议用 root 用户运行。如果遇到权限错误，请先执行：sudo -i"
fi

# ---------- 1. 检查并安装 Docker ----------
info "检查 Docker..."
if ! command -v docker >/dev/null 2>&1; then
    warn "未检测到 Docker，开始安装（约 1-3 分钟）..."

    # 先把 apt 源换成阿里云镜像（国内加速，避免连不上官方源）
    if [ -f /etc/apt/sources.list ]; then
        cp /etc/apt/sources.list /etc/apt/sources.list.bak.$(date +%s) 2>/dev/null || true
        sed -i 's|//archive.ubuntu.com|//mirrors.aliyun.com|g; s|//security.ubuntu.com|//mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true
    fi

    DOCKER_OK=0

    # 方法 1：官方脚本 + 阿里云镜像（最常用，速度最快）
    if command -v curl >/dev/null 2>&1; then
        info "尝试方法1：官方脚本 + 阿里云镜像源..."
        if curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun; then
            DOCKER_OK=1
        fi
    fi

    # 方法 2：从系统源安装 docker.io（保底方案）
    if [ "$DOCKER_OK" != "1" ] || ! command -v docker >/dev/null 2>&1; then
        warn "方法1失败，改用方法2：系统源安装 docker.io ..."
        apt-get update -qq || true
        if apt-get install -y -qq docker.io; then
            DOCKER_OK=1
        fi
    fi

    if ! command -v docker >/dev/null 2>&1; then
        err "Docker 安装失败。请手动执行以下命令后重试："
        echo ""
        echo "    apt update && apt install -y docker.io"
        echo "    systemctl enable docker && systemctl start docker"
        echo ""
        exit 1
    fi

    systemctl enable docker >/dev/null 2>&1 || true
    systemctl start docker  >/dev/null 2>&1 || true
    ok "Docker 安装完成：$(docker --version)"
else
    ok "Docker 已安装：$(docker --version)"
fi

# 判断用 docker compose 还是 docker-compose
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    warn "未找到 compose，尝试安装..."
    # 先试 compose 插件（新版）
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || true
    if docker compose version >/dev/null 2>&1; then
        DC="docker compose"
    else
        # 退回旧版 docker-compose
        apt-get install -y -qq docker-compose >/dev/null 2>&1 || true
        if command -v docker-compose >/dev/null 2>&1; then
            DC="docker-compose"
        else
            err "无法安装 docker compose，请手动执行后重试："
            echo ""
            echo "    apt install -y docker-compose"
            echo ""
            exit 1
        fi
    fi
fi
info "使用命令：$DC"

# ---------- 2. 配置国内镜像加速（加速构建） ----------
if [ ! -f /etc/docker/daemon.json ]; then
    info "配置 Docker 国内镜像加速..."
    mkdir -p /etc/docker
    cat > /etc/docker/daemon.json << 'EOF'
{
  "registry-mirrors": [
    "https://mirror.ccs.tencentyun.com",
    "https://docker.m.daocloud.io",
    "https://hub-mirror.c.163.com"
  ]
}
EOF
    systemctl restart docker >/dev/null 2>&1 || true
    ok "镜像加速已配置"
else
    info "已存在 Docker 配置，跳过镜像加速"
fi

# ---------- 3. 准备目录 ----------
info "准备数据目录..."
mkdir -p data/configs data/logs
ok "data/ 目录就绪"

# ---------- 4. 检查关键文件 ----------
MISSING=""
for f in Dockerfile docker-compose.yml requirements.txt main.py web_panel.py worker.py db.py auth.py; do
    [ -f "$f" ] || MISSING="$MISSING $f"
done
[ -d api ] || MISSING="$MISSING api/"
[ -d templates ] || MISSING="$MISSING templates/"
if [ -n "$MISSING" ]; then
    err "缺少文件：$MISSING"
    err "请确认项目文件已完整上传到 $BASE_DIR"
    exit 1
fi
ok "文件检查通过"

# ---------- 5. 构建并启动 ----------
info "开始构建镜像（首次约 3-8 分钟，请耐心等待）..."

# 先构建镜像（build 不会触发 recreate bug）
if ! $DC build; then
    err "镜像构建失败，请检查上方日志"
    exit 1
fi
ok "镜像构建完成"

# 优先用 compose 启动（用 compose 时，「端口 / 并发」以 docker-compose.yml 里的值为准）
# 若遇到旧版 compose 与新版 Docker 不兼容（报 ContainerConfig 之类）则回退到 docker run
# —— 回退分支使用本脚本顶部的 PORT / MAX_CONCURRENT 变量
START_OK=0
info "启动容器..."
if $DC up -d 2>/dev/null; then
    START_OK=1
else
    warn "compose 启动失败（旧版 docker-compose 与新版 Docker 不兼容），改用 docker run ..."
    $DC down >/dev/null 2>&1 || true
    docker rm -f chaoxing-web chaoxing-worker >/dev/null 2>&1 || true

    docker run -d --name chaoxing-web --restart unless-stopped \
        -p "$PORT:$PORT" \
        -v "$BASE_DIR/data:/app/data" \
        -e PORT="$PORT" -e HOST=0.0.0.0 \
        -e PYTHONUNBUFFERED=1 -e PYTHONIOENCODING=utf-8 \
        -e CHAOXING_DB=/app/data/chaoxing.db \
        -e AUTO_RESUME=true \
        "$IMAGE" python3 web_panel.py >/dev/null && \
    docker run -d --name chaoxing-worker --restart unless-stopped \
        -v "$BASE_DIR/data:/app/data" \
        -e PYTHONUNBUFFERED=1 -e PYTHONIOENCODING=utf-8 \
        -e CHAOXING_DB=/app/data/chaoxing.db \
        -e AUTO_RESUME=true \
        -e MAX_CONCURRENT="${MAX_CONCURRENT:-3}" \
        "$IMAGE" python3 worker.py >/dev/null && \
    START_OK=1
fi

if [ "$START_OK" != "1" ]; then
    err "容器启动失败，请执行 docker ps -a 查看状态"
    exit 1
fi

# ---------- 6. 等待并检查 ----------
info "等待服务启动..."
sleep 6
echo ""
docker ps --filter "name=chaoxing" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" || true
echo ""

# ---------- 7. 显示访问地址 ----------
# 优先获取「公网 IP」：阿里云/腾讯云的 hostname -I 返回的是内网 IP，不能用
IP=""
# 阿里云 ECS 元数据服务
[ -z "$IP" ] && IP=$(curl -s --max-time 3 http://100.100.100.200/latest/meta-data/eipv4 2>/dev/null)
# 腾讯云元数据服务
[ -z "$IP" ] && IP=$(curl -s --max-time 3 http://metadata.tencentyun.com/latest/meta-data/public-ipv4 2>/dev/null)
# 通用公网 IP 查询
[ -z "$IP" ] && IP=$(curl -s --max-time 4 https://api.ipify.org 2>/dev/null)
[ -z "$IP" ] && IP=$(curl -s --max-time 4 http://ifconfig.me 2>/dev/null)
[ -z "$IP" ] && IP=$(curl -s --max-time 4 http://ip.3322.net 2>/dev/null)
# 兜底：内网 IP（仅供参考）
INNER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$IP" ] && IP="<请在云控制台查看公网IP>"

# 简单健康检查
if curl -s --max-time 5 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    ok "服务已就绪！"
else
    warn "服务可能还在启动中，稍等 10 秒后可重试访问"
fi

echo ""
echo "======================================================"
echo -e "${G}  🎉 部署完成！${N}"
echo "======================================================"
echo ""
echo "  手机/电脑浏览器访问："
echo -e "     ${B}http://$IP:$PORT${N}"
if [ -n "$INNER_IP" ] && [ "$INNER_IP" != "$IP" ]; then
    echo ""
    echo "  （服务器内网 IP 是 $INNER_IP，仅本机可用，手机访问要用上面的公网 IP）"
fi
echo ""
echo "  ⚠️  如果打不开，请检查："
echo "     1. 云服务器控制台的【防火墙/安全组】是否放行了 5000 端口"
echo "     2. 地址用的是 http:// 而不是 https://"
echo ""
echo "  常用命令："
echo "     查看状态： docker ps --filter name=chaoxing"
echo "     查看日志： docker logs -f chaoxing-worker"
echo "     停止服务： docker stop chaoxing-web chaoxing-worker"
echo "     启动服务： docker start chaoxing-web chaoxing-worker"
echo "     重启服务： docker restart chaoxing-web chaoxing-worker"
echo ""
echo "======================================================"
echo ""
