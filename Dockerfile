FROM python:3.13-slim

WORKDIR /app

# 换用阿里云 Debian 镜像源（国内加速，避免卡在 deb.debian.org）
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi

# 系统依赖（ddddocr / onnxruntime 运行所需）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libgl1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖清单，利用 Docker 层缓存加速后续构建
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目代码
COPY . /app

# 数据目录（数据库、任务配置、日志）—— 通过卷持久化
RUN mkdir -p /app/data/configs /app/data/logs

ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8
ENV PORT=5000
ENV HOST=0.0.0.0
ENV CHAOXING_DB=/app/data/chaoxing.db
# 【重要】Cookie 必须落在共享卷内，否则容器重建后登录态丢失
ENV CHAOXING_COOKIES=/app/data/cookies.txt

# 以下几个只对 worker 容器有意义（启动 worker 时用 -e 传入）：
#   AUTO_RESUME=true      服务器重启后自动续跑被中断的任务
#   MAX_CONCURRENT=N      容器内最多同时跑几个任务（默认 1）
#   PANEL_SECRET=xxx      登录令牌签名密钥（不设则自动生成到 data/secret.key）
#   PANEL_ADMIN_RESET=1   启动时把管理员密码重置为 admin123456（救急用）

EXPOSE 5000

# 启动 Web 控制面板
# 注意：必须用 CMD 而不是 ENTRYPOINT，
# 否则 docker-compose 里 worker 服务的 command 会被当成参数追加，
# 导致 worker 容器也去跑 web_panel.py
CMD ["python3", "web_panel.py"]
