# 服务器部署说明（网页与每日运行，对外提供 HTTPS 访问）

本文从零开始，说明怎样把整套服务（数据库、每日运行、网页、HTTPS 反向代理）放到一台 Linux 服务器上。
迁移已有数据的步骤见 [迁移到服务器](migrate-to-server.md)；本机开发见 [Mac 部署说明](deploy-mac.md)。

文中的域名一律写成 `<你的域名>`，请换成你自己的；仓库是公开的，不要把真实域名或服务器地址写进任何被 git 跟踪的文件。

## 服务一览

| 服务 | 作用 |
| --- | --- |
| `db` | PostgreSQL 16；端口只绑定 `127.0.0.1:54330` |
| `migrate` | 一次性：把数据库结构升级到最新 |
| `worker` | 每个交易日定时更新数据、计算信号、发邮件、备份数据库 |
| `web` | 网页（`qt web`）；不映射宿主机端口，只被 `proxy` 访问；行情数据只读挂载 |
| `proxy` | Caddy 反向代理，自动申请并续期 HTTPS 证书；只在 `public` 档位启动，映射 80 和 443 |

## 1. 前置条件

1. 域名的 A 记录指向服务器公网 IP（解析生效后再启动，否则证书申请会失败）。
2. 云厂商防火墙/安全组只开放 **22、80、443**。数据库端口 54330 保持只绑定 `127.0.0.1`，不要改成对外开放。
3. 服务器已安装 Docker 和 Docker Compose 插件，并已获取代码（`git clone <仓库地址> quant && cd quant`）。

## 2. 填写 `.env`

```bash
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少要处理下面这些项：

- `POSTGRES_PASSWORD`：改成强密码（只在第一次创建数据库卷时生效）。
- `MAIL_BACKEND=smtp`，以及 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`MAIL_FROM`：
  **这些由你本人填写**。`SMTP_PASSWORD` 是邮箱的授权码，属于机密，只放在 `.env` 里，不要发给任何人，也不要写进文档或提交到 git。
- `ADMIN_EMAIL`：管理员邮箱，用于接收运行告警。
- `QT_BASE_URL=https://<你的域名>`：邮件里的链接和网页的来源校验都用它；线上必须是 https。
- `QT_SITE=<你的域名>`：反向代理据此申请证书，只写域名，不带 `https://` 和路径。
  **`QT_BASE_URL` 和 `QT_SITE` 必须是同一个域名**，否则邮件链接或登录表单会出问题。
- `HTTP_PROXY`、`HTTPS_PROXY`：服务器能直接访问数据源时留空（Mac 上的 `host.docker.internal:7897` 在服务器上不存在）。
- `QT_DATA_DIR`：行情数据在服务器上的路径（默认 `./data`）。

## 3. 内存与交换空间

每晚 `worker` 会构建全市场行情面板，峰值内存可达约 3 GB；网页进程不会构建全市场面板，并且 `compose.yaml` 给它设了 `mem_limit: 800m` 作为兜底。
数据库设了 `oom_score_adj: -500`：内存真的不够时，内核优先杀别的进程，而不是先杀数据库。

**服务器内存小于 4 GB 时，必须先加交换空间再启动**，否则夜间运行（再加上网页）可能把内存用完，进程被系统杀掉。
下面是 2 GB 交换文件的示例，**这是对系统的修改，由你本人在服务器上执行**，项目的脚本不会替你改：

```bash
# 示例：创建 2 GB 交换文件（需要 root）
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
# 重启后保持：在 /etc/fstab 末尾加一行
# /swapfile none swap sw 0 0
```

执行后用 `free -m` 确认 `Swap` 一行的总量不是 0。

第一次夜间运行期间（网页保持运行），用 `docker stats` 观察 `worker`、`web`、`db` 的内存占用，把峰值记到文末的"首次上线记录"里。

## 4. 启动

```bash
docker compose --profile public up -d --build
docker compose ps        # db healthy，migrate Exited (0)，worker、web、proxy running
```

第一次会构建镜像并申请证书，需要几分钟。之后用浏览器打开 `https://<你的域名>/login`，应看到登录页。

如果 `proxy` 启动后立刻退出，首先检查 `.env` 里是否设置了 `QT_SITE`：站点地址为空时 Caddy 的报错不好懂。
检查网页是否可访问时不要用 `curl -I`（HEAD 请求，`/login` 会返回 405），改用
`curl -s -o /dev/null -w '%{http_code}' https://<你的域名>/login`，或请求 `/healthz`。

注意：必须带 `--profile public`，否则不会启动 `proxy`。`docker compose up -d`（不带档位）不会启动 `proxy`。

如果 `data/` 是完全空的（新服务器上还没有下载过行情），登录后的页面会出错，要等行情下载完才能正常使用。
首次完整下载约 3 到 6 小时：

```bash
docker compose run --rm worker qt data update
```

## 5. 第一个管理员与邀请码

```bash
docker compose run --rm worker qt admin user add --email <邮箱> --admin
```

这一步只创建账号，还没有密码。然后在网页上点"找回密码"，输入这个邮箱，从邮件里的链接设置密码并登录
（因此 SMTP 必须先配好）。

登录后，在"管理 → 邀请码"页面生成邀请码，发给要邀请的人。邀请码有有效期，只能用一次。

## 6. 日志与反向代理的取舍

```bash
docker compose logs -f web
docker compose logs -f proxy
docker compose logs -f worker
```

每个服务的容器日志都限制为单个文件 10 MB、最多 3 个（`compose.yaml` 里的 `logging`），不会把磁盘写满。

网页（uvicorn）和 Caddy **故意都不写访问日志**：邮箱验证和重置密码的令牌放在网址的查询串里，写访问日志会把令牌落到磁盘。
`deploy/Caddyfile` 里不要加 `log` 指令。`proxy` 的日志只有启动、证书申请等运行信息。

安全响应头（HSTS、CSP 等）由应用自己设置，Caddy 不重复也不覆盖。

## 7. 证书在哪里

证书和 Caddy 的账号数据存在具名卷 `caddy_data`（容器内 `/data`），配置缓存在 `caddy_config`。
这两个卷要保留：重建容器不会丢；不要执行 `docker compose down -v`，否则证书会丢，需要重新申请（有频率限制）。

## 8. 升级与回滚

升级：

```bash
./scripts/backup_db.sh                       # 先备份
git pull
docker compose --profile public up -d --build
docker compose ps
```

`migrate` 会在启动时自动升级数据库结构。**迁移只向前，不提供降级**。

回滚：

1. `git checkout <旧版本>`，重新 `docker compose --profile public up -d --build`。
2. 如果新版本做过数据库结构变更，旧代码可能无法使用新结构，此时需要用升级前的备份恢复数据库
   （恢复步骤见 [迁移到服务器](migrate-to-server.md) 第 4 节），再启动旧版本。

## 9. 备份

`worker` 每次每日运行后自动备份到 `backups/`（保留 14 天）；也可随时手动执行 `./scripts/backup_db.sh`。
请再用 cron 或云厂商快照把 `backups/` 同步到另一台机器。备份含全部使用者的数据，注意权限。

## 10. 上线前检查

- **邮件送达：** 新域名发出的邮件可能被判为垃圾邮件。上线前用 QQ、163、Gmail 各注册一次，确认验证邮件能收到（看一下垃圾箱）。
- **条款文字：** 注册页展示的条款在 `src/qt/web/templates/_terms_text.html`，上线前你必须亲自过目并按需修改。
- **来源校验：** 若登录或提交表单被拒绝，先检查 `QT_BASE_URL` 与你浏览器里访问的地址是否完全一致（含 `https`）。
- **交换空间（内存小于 4 GB 的主机）：** `free -m` 里 `Swap` 的总量不是 0。
- **夜间运行的内存：** 第一次夜间运行时网页保持运行，用 `docker stats` 看 `worker`、`web`、`db` 的内存，确认没有被杀掉的容器（`docker compose ps` 里没有反复重启的）。
- **来访 IP：** 第一次登录之后，用下面的只读命令看最近的登录记录，`ip` 应是你的公网 IP，而不是 `172.` 开头的容器网络地址
  （如果是 `172.x`，说明网页没拿到反向代理转来的真实地址，登录频率限制会把所有人算成同一个 IP）：

  ```bash
  docker compose exec db psql -U qt -d qt -c "SELECT created_at, action, detail->>'ip' AS ip FROM audit_log WHERE action IN ('login_ok', 'login_failed') ORDER BY id DESC LIMIT 5;"
  ```

## 首次上线记录

上线时由部署者填写：

- 上线日期：
- 域名：
- 服务器配置（核数 / 内存 / 是否加了交换空间）：
- 交换空间已生效（`free -m` 的 Swap 总量）：
- 证书申请是否一次成功：
- 第一次夜间运行（网页保持运行）：开始时间 / 结束时间 / `docker stats` 内存峰值 `worker` 　 `web` 　 `db` ：
- 第一次登录后 `audit_log` 里的 IP 是公网 IP（不是 172.x）：
- 邮件送达测试：QQ 　 163 　 Gmail ：
- 条款文字已过目：
- 遇到的问题与处理：
