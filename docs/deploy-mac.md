# Mac 部署说明（第二阶段：每日运行）

本文说明怎样在自己的 Mac 上用 Docker 把每日信号服务跑起来。以后搬到服务器见 `docs/migrate-to-server.md`。

整个服务由三个容器服务组成（见 `compose.yaml`）：

| 服务 | 作用 |
| --- | --- |
| `db` | PostgreSQL 16 数据库，数据存在 Docker 卷 `qt_pg` 里 |
| `migrate` | 一次性：启动时把数据库结构升级到最新，成功后退出 |
| `worker` | 常驻定时进程：每个交易日 18:00 开始更新数据、计算信号、发邮件；睡醒后自动补跑；每次跑完备份数据库 |

> 以下涉及你自己 Mac 的设置（Docker Desktop、系统设置、`launchctl`、`pmset` 等）都由你本人操作，
> 项目里的脚本和代码不会替你改动任何系统设置。

## 1. 安装容器运行环境

1. 安装 Docker Desktop（Mac 版）并启动，等菜单栏的鲸鱼图标显示“Docker Desktop is running”。
2. 在 Docker Desktop 的设置里勾选“Start Docker Desktop when you sign in”（登录时自动启动），并保持 Mac 登录状态。
3. 终端里确认：`docker --version`、`docker compose version` 都能输出版本号。

## 2. 准备 `.env`

```bash
cp .env.example .env
```

`.env` 已被 git 忽略，不会被提交。按下表填写：

| 变量 | 填什么 |
| --- | --- |
| `POSTGRES_PASSWORD` | 数据库密码。本机自用可保留默认；以后放到服务器上必须改成强密码 |
| `MAIL_BACKEND` | `file`：邮件只写成 `.eml` 文件放在 `./mail_out`，不发送（试运行用）；`smtp`：真正发邮件 |
| `SMTP_HOST`、`SMTP_PORT` | 邮箱服务商的发信服务器与端口，在邮箱设置的“POP3/SMTP 服务”里查看（端口 465 为 SSL，587 为 STARTTLS） |
| `SMTP_USER` | 发信邮箱地址 |
| `SMTP_PASSWORD` | **SMTP 授权码**，见下 |
| `MAIL_FROM` | 发件人地址，一般与 `SMTP_USER` 相同 |
| `ADMIN_EMAIL` | 管理员邮箱：数据更新失败、备份失败等系统异常邮件发到这里（必填，worker 启动需要） |
| `QT_DATA_DIR` | 行情数据目录，默认 `./data` |
| `HTTP_PROXY`、`HTTPS_PROXY` | 访问数据源需要的代理。容器访问 Mac 上的代理要写 `host.docker.internal`，例如 `http://host.docker.internal:7897`；不需要代理就留空 |

**SMTP 授权码怎么取：** 授权码不是邮箱登录密码。登录你的邮箱网页版，在“设置 → 账户（或 POP3/SMTP/IMAP）”里开启
SMTP 服务，按提示验证后会生成一串授权码，只显示一次。把它填到 `.env` 的 `SMTP_PASSWORD=` 后面即可。

- 授权码只放在你自己 Mac 的 `.env` 里：不要提交到 git，不要粘贴到聊天、工单、截图或任何文档里。
- 项目里的 `.env.example` 只有空的占位，任何人（包括帮你配置的 AI 助手）都不应该代你填真实授权码。
- 授权码泄露了就回到邮箱设置里作废重新生成。

先用 `MAIL_BACKEND=file` 跑通整条链路，确认邮件内容无误后，再改成 `smtp` 并填好授权码，然后 `docker compose up -d worker` 重启生效。

## 3. 启动

```bash
docker compose up -d
```

第一次会构建镜像，需要几分钟。检查：

```bash
docker compose ps          # db 为 healthy，migrate 为 Exited (0)，worker 为 running
docker compose logs migrate
```

`worker` 配置了 `restart: unless-stopped`：Docker 启动后它会自动起来，进程异常退出也会自动重启。它只在 `migrate` 成功之后才会启动。

### 先下载行情数据，再让 worker 干活

`worker` 一启动就会尝试补跑，而每日运行需要 `data/` 里已经有行情数据。所以**第一次**要按这个顺序：

```bash
docker compose up -d db migrate                  # 只起数据库并升级结构
docker compose run --rm --no-deps worker qt data update   # 首次完整下载，约 3 到 6 小时，可中断后重跑续传
docker compose up -d                             # 数据备好后再起 worker
```

如果 `data/` 里已经有数据（例如以前用 `uv run qt data update` 下载过），跳过中间那条即可。
若直接 `docker compose up -d`，worker 在没有订阅时什么也不做；创建订阅之后它第一次运行才会去更新数据，
首次会和上面一样要数小时，期间当晚没有信号。

## 4. 创建第一个使用者和订阅

下面的命令都在项目根目录运行，通过容器执行（容器里已经配好数据库地址和数据目录）：

```bash
# 创建使用者（加 --admin 为管理员）
docker compose run --rm worker qt admin user add --email you@example.com --admin

# 创建订阅：起始资金 150000，使用默认参数；也可以用 --set 改参数，如 --set strategy.stop_loss=0.10
# （起始日缺省为下一个交易日；想立刻试运行见下文 --start）
docker compose run --rm worker qt admin sub create --email you@example.com --cash 150000

# 查看订阅与参数
docker compose run --rm worker qt admin sub show --email you@example.com
```

**第一批信号什么时候来：** 订阅缺省的起始日是创建当天之后的第一个交易日，所以今天创建的订阅，要等那个交易日 18:00 之后的运行才会出信号和邮件；
现在立刻 `qt run-day` 不会有任何信号、也没有邮件（订阅还没开始）。每人同时只有一个生效的订阅。

**想马上把整条链路走通（试运行）：** 创建订阅时用 `--start` 指定起始日，选**最近一个已收盘的交易日**
（交易日 15:00 收盘后是当天，否则是上一个交易日）：

```bash
docker compose run --rm worker qt admin sub create --email you@example.com --cash 150000 --start 2026-11-13
docker compose run --rm worker qt run-day            # 跑那一天，应产生信号并写出邮件
docker compose run --rm worker qt run-day            # 再跑一次，不应有新增记录和邮件
ls mail_out/                                         # MAIL_BACKEND=file 时邮件是这里的 .eml 文件
```

`--start` 必须是交易日、不晚于缺省起始日，最早只能是 10 个交易日之前（`service.backdate_limit_days`）；
起始日在过去时，每日运行会按交易日逐日补算，只给最新一天发信号邮件，更早日子的信号标为“未通知”。

### 常用管理命令

| 命令 | 用途 |
| --- | --- |
| `qt admin user add --email E [--admin]` / `user list` / `user weekly --email E --on\|--off` | 使用者管理、周报开关 |
| `qt admin sub create --email E --cash N [--start 日期] [--set 路径=值 ...]` | 创建订阅；`--start` 可选过去的交易日 |
| `qt admin sub set --email E --set 路径=值 ...` | 修改参数（从下一次运行起生效） |
| `qt admin sub show / pause / resume / close --email E` | 查看、暂停、恢复、关闭订阅 |
| `qt admin trade buy\|sell 代码 --email E --price P --shares N [--day D] [--fees F]` | 录入手动账的成交；日期缺省为最近已收盘的交易日 |
| `qt admin holding add 代码 --email E --cost P --shares N --bought D` | 把已有持仓转入手动账 |
| `qt admin cash deposit\|withdraw --email E --amount A [--day D]` | 入金、出金 |
| `qt admin entry void --email E ID` | 作废一条流水（录错时作废后重录） |
| `qt admin book show --email E [--book manual\|system]` | 查看账本、持仓和最近流水 |
| `qt admin signals --email E [--book ...] [--status S] [--days 30]` | 查看信号 |
| `qt admin journal --email E [--book ...]` | 查看交易日志与收益跟踪 |
| `qt admin runs [--last 10]` | 查看每日运行记录 |
| `qt run-day [--day YYYY-MM-DD] [--notify\|--no-notify] [--update\|--no-update]` | 手动跑某一天（缺省为当晚该跑的那天） |

加 `--help` 可看每条命令的全部选项。

## 5. 查看运行记录和日志

```bash
docker compose run --rm worker qt admin runs --last 10   # 每个交易日的运行状态与失败原因
docker compose logs -f worker                              # worker 的实时日志（Ctrl-C 退出查看，不影响运行）
docker compose logs --since 24h worker                     # 最近 24 小时
```

运行状态含义：`done` 正常完成；`done_with_errors` 完成但个别使用者处理出错（管理员邮箱会收到告警）；
`data_failed` 数据更新或检查失败，当晚没有出信号，worker 每 30 分钟重试，次日 08:30 仍未完成会发超时告警。

## 6. 数据库备份

- **自动备份：** worker 每次完成每日运行（状态为 `done` 或 `done_with_errors`）之后备份一次，文件是
  `backups/qt-YYYYMMDD.sql.gz`，保留最近 14 天。备份失败不影响当晚的运行，管理员邮箱会收到
  “【系统异常】数据库备份失败”（每天最多一封）。
- **手动备份：** `./scripts/backup_db.sh`（做法与自动备份相同）。
- 备份里有全部使用者的数据：文件权限是 0600，`backups/` 目录已被 git 和 Docker 构建忽略，不要拷给别人。
- 备份只保存数据库；行情数据在 `data/` 里，需要自己另行备份。恢复方法见 `docs/migrate-to-server.md`。

## 7. 防休眠：Mac 必须醒着并接电

每晚更新实测约 3.5 小时（从 18:00 起，见文末实测一节）。**这段时间里 Mac 必须保持唤醒并接通电源。**
容器不能阻止 Mac 休眠，下面两种办法都要由你自己在 Mac 上操作：

**办法一（最简单）：** 把 Mac 接上电源，在“系统设置 → 电池（或节能）”里，把接电源时的自动休眠关闭
（如“接通电源时防止自动进入睡眠”）。合上屏幕盖通常仍会休眠，请不要合盖。

**办法二：** 安装 `scripts/mac/com.qt.keepawake.plist`。它在每个工作日 17:55 运行 `caffeinate -i -t 14400`，
让 Mac 在之后 4 小时内不因空闲休眠。它只是模板，项目里的任何命令都不会替你加载；要用的话你自己这样做：

```bash
cp scripts/mac/com.qt.keepawake.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.qt.keepawake.plist
```

不再需要时：`launchctl unload ~/Library/LaunchAgents/com.qt.keepawake.plist` 后删除该文件。
注意 4 小时只比实测的 3.5 小时多半小时，行情源慢的夜晚可能不够，所以仍建议同时用办法一。

**错过了怎么办：** 如果 Mac 睡过了 18:00，或者整晚没开机，worker 在恢复运行后的下一个 60 秒节拍就会自动补跑，
不需要手动操作。睡了几天也会按交易日逐日补算；补跑时只给最新一天发信号邮件，更早日期的信号记为“未通知”。

## 8. 日常维护

- 更新代码后：`docker compose build && docker compose up -d`（`migrate` 会自动升级数据库结构）。
- 停止：`docker compose stop`；再启动：`docker compose start`。
- 不要执行 `docker compose down -v`：`-v` 会删除数据库卷。

## 首次试运行记录

<!-- 由首次用真实数据试运行之后填写：运行日期、总耗时、各阶段耗时、产生的信号数、写出的邮件文件、重复运行是否无新增 -->

（尚未填写）

## 每日更新耗时实测

实测日期：2026-11-13（周五，数据截止日 2026-11-13）。命令：

```
docker compose run --rm worker uv run python scripts/probe_daily_update.py --symbols 60
```

环境：Docker Desktop（Mac），容器经宿主机代理 `http://host.docker.internal:7897` 访问数据源；
AKShare 在容器里能正常安装并取到数据，抽样 60 只全部成功、无失败。脚本把 `data/` 复制到容器临时目录后
在副本上运行，原目录没有被改动；通过 `qt.data.update.update_all` 调用，不直接访问 AKShare。
60 只是在市证券按代码排序后均匀抽样；已退市且很久没有新日线的股票会被直接跳过，几乎不耗时，
所以只抽在市股票是偏保守的。

| 阶段 | 次数 | 合计耗时 | 平均每只 |
| --- | --- | --- | --- |
| 日线（增量） | 60 只 | 167.1 秒 | 2.79 秒 |
| 分红 | 60 只 | 79.9 秒 | 1.33 秒 |
| 财务 | 60 只 | 258.7 秒 | 4.31 秒 |
| 分红 + 财务 | 60 只 | 339.2 秒 | 5.65 秒 |
| 共享表（证券列表 8.3 秒、ST 12.2 秒、基准 4.3 秒、交易日历 1.3 秒、行业 1.7 秒） | 各 1 次 | 27.8 秒 | - |

整个 `update_all`（共享表 + 60 只日线 + 行业 1 只）用时 203.6 秒。滚动刷新规则下这一轮恰好没有轮到
任何一只的分红或财务，所以财务分红阶段另用 `refresh_fundamentals` 对 60 只强制下载来计时。

### 整晚外推

全市场 3487 只，日线每晚全部检查，财务分红每晚约七分之一（`fundamentals_refresh_days = 7`）：

- 日线：2.79 秒 × 3487 = 约 162 分钟
- 财务分红：5.65 秒 × 3487 ÷ 7 = 约 47 分钟
- 共享表：约 0.4 分钟
- 合计约 **209 分钟，3.5 小时**，低于 4 小时的上限，但余量只有约半小时。

### 注意

- 单次请求耗时主要由网络往返与 `request_pause_seconds`（每次请求后 1 秒礼貌间隔）决定，与日线落后几天关系不大。
- 这是单个晚上、单次抽样的数据，数据源的响应速度波动会直接改变结果；整晚任务应按“可能超过 4 小时”设计，
  即中途失败可续传、白天不依赖夜间任务一定已跑完。
- 若要缩短，可调小 `request_pause_seconds`，但有被数据源限流的风险，需要另行评估。
