# 迁移到服务器

把每日运行服务从 Mac 搬到一台 Linux 服务器。思路：新服务器装好 Docker、放好代码与 `.env`，用备份恢复数据库，
再放好 `data/`，启动后核对运行记录。迁移期间旧机器上的 worker 要先停掉，避免两边同时发邮件。

## 1. 在旧机器上准备

```bash
docker compose stop worker          # 先停掉定时进程，避免备份之后又有新写入
./scripts/backup_db.sh              # 生成 backups/qt-YYYYMMDD.sql.gz
```

备份文件含全部使用者的数据，传输时请用 `scp` 等加密方式，不要通过聊天工具或网盘公开链接传。

## 2. 在新服务器上安装 Docker

按 Docker 官方文档安装 Docker Engine 和 Compose 插件（Debian/Ubuntu 用官方 apt 源）。确认
`docker --version`、`docker compose version` 正常，并让当前用户能运行 docker（加入 `docker` 组后重新登录）。

## 3. 拷贝代码与 `.env`

```bash
git clone <仓库地址> quant && cd quant     # 或把项目目录整个拷过去
scp 旧机器:项目目录/.env .env                # .env 含密码和授权码，只走加密通道
chmod 600 .env
```

然后编辑 `.env`：

- **去掉代理设置：** 服务器若能直接访问数据源，把 `HTTP_PROXY`、`HTTPS_PROXY` 两行留空或删除。
  （`host.docker.internal:7897` 是 Mac 上的代理，服务器上不存在。）
- 把 `POSTGRES_PASSWORD` 改成强密码（新建数据库卷时才会生效，所以要在第 4 步之前改）。
- `QT_DATA_DIR` 改成服务器上的实际路径（默认 `./data`）。
- 确认 `MAIL_BACKEND=smtp` 与 SMTP 各项仍然正确。
- 要对外提供网页：设置 `QT_BASE_URL=https://<你的域名>` 和 `QT_SITE=<你的域名>`（同一个域名），其余步骤见 [服务器部署说明](deploy-server.md)。

## 4. 恢复数据库

只启动数据库，让它建空库，再导入备份：

```bash
docker compose up -d db
gunzip -c backups/qt-YYYYMMDD.sql.gz | docker compose exec -T db psql -U qt -d qt -v ON_ERROR_STOP=1
```

备份是纯 SQL 文本（镜像里的 pg_dump 16 生成，已去掉属主与权限信息，与服务端同一主版本），可以导入一个全新的空库。
这条恢复命令已在 postgres:16 上用同样的写法实测过（备份、导入空库、各表行数一致）。
如果库里已经有表（例如已经执行过 `migrate`），先清空再导入：

```bash
docker compose exec db psql -U qt -d qt -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
```

## 5. 放好行情数据 `data/`

两种办法，任选其一：

- **拷贝：** `rsync -a 旧机器:项目目录/data/ ./data/`（能省下数小时下载）。
- **重新下载：** 在服务器上 `docker compose run --rm worker qt data update`，首次完整下载约 3 到 6 小时。

## 6. 启动并核对

```bash
docker compose up -d                     # 提供网页时改用 docker compose --profile public up -d --build，见 deploy-server.md
docker compose ps                        # db healthy，migrate Exited (0)，worker running（有网页时还有 web、proxy）
docker compose run --rm worker qt admin runs --last 10
docker compose run --rm worker qt admin user list
docker compose logs -f worker
```

核对要点：

1. `qt admin runs` 里的最近几条运行记录与旧机器一致；
2. `qt admin user list` 的使用者都在；
3. 对一个使用者执行 `qt admin book show --email E`，持仓与现金与旧机器一致；
4. 第一个交易日晚上之后，`qt admin runs` 出现新的一条，状态为 `done`，使用者收到邮件。

旧机器上的 worker 确认不再需要后保持停止（`docker compose down`，注意不要加 `-v`，旧数据库卷先留着做保险）。

## 7. 服务器上的备份与安全

- worker 每次每日运行完成后会自动备份到 `backups/`（保留 14 天）。请再用 cron 或云厂商快照把 `backups/` 同步到另一台机器，
  服务器坏了才有办法恢复。
- 数据库端口只映射到本机回环地址（`127.0.0.1:54330`），不要改成对外开放。
- 服务器没有“休眠”问题，不需要防休眠设置。
