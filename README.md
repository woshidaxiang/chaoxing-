# 超星学习通 · 多用户刷课面板

在 [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing) 命令行脚本之上，加了一套
**「Web 面板 + 独立 Worker + SQLite 持久化 + 多用户登录」** 的自托管部署方案。

> 浏览器点几下就能用，任务跑在服务器上 —— 关掉手机、关掉电脑，课照样在刷。

---

## ✨ 特性

| 特性 | 说明 |
|------|------|
| 📱 **手机网页操作** | 不用装 App，手机浏览器打开就行 |
| 🌙 **无后台挂机** | 任务在服务器上执行，关掉浏览器/手机关机都不影响 |
| 👥 **多用户登录** | 一人一个账号，任务 / 日志 / 绑定的学习通账号**互相隔离** |
| 🛠 **成员管理** | 管理员在网页里开号、改密、停用、删除，支持**批量添加** |
| 🔗 **一账号绑一号** | 一个面板账号绑定一个学习通账号 |
| 💾 **状态持久化** | 任务与日志写 SQLite，重开页面自动恢复 |
| 🔀 **Web / Worker 分离** | 重启网页服务**不影响**正在跑的任务 |
| ⚙️ **并发可配** | `MAX_CONCURRENT` 控制同容器内并行任务数，任务自动排队 |
| ⏸ **暂停 / 继续 / 停止** | 真挂起进程（SIGSTOP），不丢进度 |

---

## 🏗 架构

```
┌──────────────────────┐        ┌──────────────────────┐
│  chaoxing-web        │        │  chaoxing-worker     │
│  Flask 面板 :5000     │        │  任务执行进程          │
│  · 登录鉴权(auth.py)  │        │  · 轮询认领 pending    │
│  · 创建任务入队        │        │  · 启动 main.py 子进程 │
│  · 状态/日志/SSE      │        │  · 日志落库 / 心跳     │
└──────────┬───────────┘        └──────────┬───────────┘
           └───────────┬──────────────────┘
                       ▼
            data/chaoxing.db  (SQLite, WAL)
            data/configs/     data/logs/
```

| 文件 | 职责 |
|------|------|
| `web_panel.py` | Flask 后端：登录、成员管理、任务、日志、暂停/停止 |
| `auth.py` | 登录令牌（HMAC-SHA256 签名，无状态，HttpOnly Cookie） |
| `worker.py` | 任务 Worker：认领任务、执行、日志、心跳、暂停监控 |
| `db.py` | SQLite 层：tasks / logs / courses_cache / accounts / users |
| `main.py` | 上游刷课主程序（命令行入口） |
| `templates/index.html` | 手机端界面（原生 JS，无框架） |
| `api/` | 上游 API 模块（请求、题库、验证码、字体解码等） |

---

## 📚 文档导航

| 我想… | 看这个 |
|---|---|
| **第一次部署**（从买服务器到跑起来） | [docs/小白部署教程.md](docs/小白部署教程.md) —— 零基础一步不落，含完整代码与排错 |
| **从 v13 升级到 v14** | [docs/v14部署说明.md](docs/v14部署说明.md) |
| **日常运维**（开号 / 日志 / 调并发 / 备份 / 迁移新服务器） | [docs/使用与维护速查卡.md](docs/使用与维护速查卡.md) |
| **读懂代码 / 改代码**（架构、数据表、接口） | [docs/架构说明.md](docs/架构说明.md) |
| 全部文档（含历史归档） | [docs/README.md](docs/README.md) |

---

## 🚀 部署

> 📖 **零基础、一步不落的完整教程（含完整代码与排错）：** [`docs/小白部署教程.md`](docs/小白部署教程.md)
> 下面是精简版，只想快速跑起来的可以照这个来。


### ① 构建镜像

```bash
docker build -t chaoxing-web:v14 .
```

### ② 启动 Web + Worker

```bash
docker run -d --name chaoxing-web -p 5000:5000 \
  -v /opt/chaoxing/data:/app/data \
  -e AUTO_RESUME=true --restart unless-stopped \
  chaoxing-web:v14 python3 web_panel.py
```

```bash
docker run -d --name chaoxing-worker \
  -v /opt/chaoxing/data:/app/data \
  -e AUTO_RESUME=true -e MAX_CONCURRENT=5 --restart unless-stopped \
  chaoxing-web:v14 python3 worker.py
```

> ⚠️ 注意镜像的 CMD 是 `python3 web_panel.py`，**启动 worker 必须显式写 `python3 worker.py`**。

### ③ 首次使用

浏览器打开 `http://<你的服务器IP>:5000`
→ 出现「首次使用 · 创建管理员」→ 填用户名密码 → 进入
→ 「设置 → 成员管理」给成员开号 → 把 网址 + 账号 发给他们。

### 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `PORT` | 5000 | Web 监听端口 |
| `CHAOXING_DB` | `data/chaoxing.db` | 数据库路径 |
| `CHAOXING_COOKIES` | `/app/data/cookies.txt` | Cookie 路径（**必须在共享卷内**） |
| `AUTO_RESUME` | false | Worker 启动时自动续跑被中断的任务 |
| `MAX_CONCURRENT` | 1 | 单容器内最多同时执行几个任务 |
| `PANEL_SECRET` | 自动生成 | 登录令牌签名密钥（不设则写入 `data/secret.key`） |
| `PANEL_ADMIN_RESET` | — | 设为 1 时启动会把管理员密码重置为 `admin123456`（救急用） |

---

## 📖 使用流程

1. 管理员：设置 → 成员管理 → 添加成员
2. 成员：打开网址 → **登录** → 控制面板 → **绑定超星账号**（自己的学习通手机号 + 密码）
3. 成员：我的课程 → 勾一门课 → 点「▶ 开始刷课」
4. 之后可以关掉浏览器；下次打开自动显示进度和历史日志
5. ⚠️ **一次只能刷一门课**，刷完再回来选下一门

> 同时能刷几个账号 = 所有 worker 容器的 `MAX_CONCURRENT` 之和；
> 超出的任务显示「排队中」，前面跑完自动顶上。

---

## ❓ 常见问题

**Q：日志里大量 403 / 换账号也 403？**
多半是**服务器 IP 被超星风控**了（并发越高越容易）。降低 `MAX_CONCURRENT`，或者换公网 IP。

**Q：网页打不开？**
用 `http://` 不要 `https://`；确认云服务器安全组放行了端口；`docker ps` 看两个容器是否 `Up`。

**Q：想调整同时刷课人数？**
重建 worker 容器，改 `-e MAX_CONCURRENT=N`。注意内存占用（每个任务约 100~150MB，触发验证码时更高）。

**Q：忘记管理员密码？**
删掉 web 容器，启动时加 `-e PANEL_ADMIN_RESET=1`，会把管理员密码重置为 `admin123456`，登录后立刻改掉。

**Q：`data/secret.key` 能删吗？**
删了不会丢数据，但所有人需要重新登录。

---

## 🔄 更新、备份与迁移

**备份（30 秒，强烈建议定期做）**

```bash
cd /opt/chaoxing && tar czf ~/cx-data-$(date +%m%d).tar.gz data
```

这一个文件就是全部数据（数据库 + 任务配置 + Cookie + 密钥）。
详细版（自动备份、恢复、**迁移到新服务器**的完整步骤）见
[docs/使用与维护速查卡.md](docs/使用与维护速查卡.md)。

**升级版本**：上传新包 → `docker build -t chaoxing-web:v新版本 .` → 删旧容器 → 用新标签重跑。
**回退**：只要旧标签的镜像还在，换个标签重跑即可 —— 所以**构建时一定用带版本号的标签**，
不要一直用 `latest`（会覆盖旧镜像，想回退就没得退了）。

---

## 🧪 测试

```bash
python tests/regression_test.py
```

覆盖登录、成员管理、越权隔离、单课限制、暂停/停止、日志裁剪、统计口径、Cookie 账号隔离、线程安全、Worker 并发等 **96 项**检查。

---

## 📁 目录结构

```
.
├── main.py / web_panel.py / worker.py / db.py / auth.py   # 程序
├── api/                     # 上游 API 模块
├── templates/index.html      # 前端界面
├── resource/                 # 字体映射表
├── tests/                    # 回归测试
├── docs/                     # 文档（★ 新手先看 小白部署教程.md；索引见 docs/README.md；历史文档在 docs/archive/）
├── Dockerfile
├── docker-compose.yml
└── data/                     # 运行时生成（已 gitignore，含账号密码，切勿提交）
```

---

## 🙏 致谢 / 许可 / 免责声明

- 核心刷课逻辑来自开源项目 **[Samueli924/chaoxing](https://github.com/Samueli924/chaoxing)**，遵循 **GPL-3.0**。
- 本项目同样是 **GPL-3.0** 衍生作品，**开源免费**，**禁止用于盈利**，不得作为闭源商业软件发布或销售。
- 本代码**仅用于学习讨论**，请勿用于任何违反所在学校规定或法律法规的用途。
- 使用者对自己的一切行为负责。
