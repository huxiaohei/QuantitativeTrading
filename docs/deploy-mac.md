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
| `qt admin sub set --email E --set 路径=值 ...` | 修改参数（从下一次运行处理的交易日起生效，命令会打印这一天） |
| `qt admin sub show / pause / resume / close --email E` | 查看、暂停、恢复、关闭订阅 |
| `qt admin trade buy\|sell 代码 --email E --price P --shares N [--day D] [--fees F]` | 录入手动账的成交；日期缺省为最近已收盘的交易日 |
| `qt admin holding add 代码 --email E --cost P --shares N --bought D` | 把已有持仓转入手动账 |
| `qt admin cash deposit\|withdraw --email E --amount A [--day D]` | 入金、出金 |
| `qt admin entry void --email E ID` | 作废一条流水（录错时作废后重录） |
| `qt admin book show --email E [--book manual\|system]` | 查看账本、持仓和最近流水 |
| `qt admin signals --email E [--book ...] [--status S] [--days 30]` | 查看信号 |
| `qt admin journal --email E [--book ...]` | 查看交易日志与收益跟踪 |
| `qt admin runs [--last 10]` | 查看每日运行记录（第一行是发件箱里待发、发送失败的邮件数） |
| `qt admin outbox list [--status pending\|sent\|failed] [--last 50]` | 查看发件箱：状态、尝试次数、最近一次错误 |
| `qt admin outbox retry ID` / `outbox retry --all-failed` | 把发送失败的邮件重新排队（重新开始 48 小时重试期） |
| `qt run-day [--day YYYY-MM-DD] [--notify\|--no-notify] [--update\|--no-update]` | 手动跑某一天（缺省为当晚该跑的那天）；不做数据库备份；worker 正在运行时会提示“另一个每日运行正在进行”并退出 |

加 `--help` 可看每条命令的全部选项。

## 5. 查看运行记录和日志

```bash
docker compose run --rm worker qt admin runs --last 10   # 每个交易日的运行状态与失败原因
docker compose logs -f worker                              # worker 的实时日志（Ctrl-C 退出查看，不影响运行）
docker compose logs --since 24h worker                     # 最近 24 小时
```

运行状态含义：`done` 正常完成；`done_with_errors` 完成但个别使用者处理出错（管理员邮箱会收到告警）；
`data_failed` 数据更新或检查失败（包括持仓股票当天行情下载失败、再下载一次仍失败），当晚没有出信号，
worker 每 30 分钟重试，直到**下一个交易日** 08:30（周五的失败周末也会一直重试）；到期仍未完成会发超时告警。

### 数据完整性检查一直不通过怎么办

`qt admin runs` 会逐条列出检查发现的问题（如“某股票在某日复权收盘价单日变动 +30%”“仅 N/M 只在市股票有日线”）
和下载失败的股票。某一只股票的本地日线有错、反复让检查失败时，可以只重新下载这一只：

```bash
mkdir -p data/bad-bars && mv data/bars/600001.parquet data/bad-bars/      # 先挪走这只股票的日线文件（不要删）
docker compose run --rm worker qt data update --symbols 600001          # 从头重新下载这一只
docker compose run --rm worker qt data check                            # 可选：扫描全部历史
```

之后不用做别的：worker 下一次重试（最多 30 分钟）会重新检查并补跑。也可以手动
`docker compose run --rm worker qt run-day` 立即重跑当天（若 worker 正在运行会提示稍后再试）。
重新下载后问题仍在，多半是数据源本身的数据有误，请看 `docs/data-gaps.md` 并记录下来；
覆盖率不足（很多股票同时缺日线）一般是数据源当天还没更新完，等下一次重试即可。

邮件发送失败（如 SMTP 暂时不可用）时，邮件留在发件箱里，worker 每 30 分钟补发一次；入队满 48 小时仍未发出
才标记为发送失败并告警管理员。修好发信之后用 `qt admin outbox retry --all-failed` 重新排队。
管理员告警邮件本身发不出时，worker 日志里会有一条 ERROR（带告警标题）。

## 6. 数据库备份

- **自动备份：** worker 每次完成每日运行（状态为 `done` 或 `done_with_errors`）之后备份一次，文件是
  `backups/qt-YYYYMMDD.sql.gz`，保留最近 14 天。备份失败不影响当晚的运行，管理员邮箱会收到
  “【系统异常】数据库备份失败”（每天最多一封）。
- **手动备份：** `./scripts/backup_db.sh`（做法与自动备份相同）。手动执行 `qt run-day` **不会**备份数据库，
  需要时请在它之后运行这个脚本。
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

试运行时间：2026-11-15（周日）13:47 至 23:45，对 2026-11-13（周五）这个交易日跑完整流程。
环境：本机 Docker，`MAIL_BACKEND=file`（邮件只写文件，不发送），一个试用使用者
`trial@example.com`，本金 150,000，默认参数，订阅起始日 2026-11-13。

**结果**

| 项目 | 结果 |
|---|---|
| 运行状态 | `done`，没有使用者出错 |
| 两本账 | 系统账、手动账都推进到 2026-11-13 |
| 信号 | 手动账 10 个买入信号（已通知），系统账 10 个买入信号（已生成），两边股票和股数相同 |
| 邮件文件 | `mail_out/` 下 2 个 `.eml`：`【买入信号】2026-11-13 10 只`、`【周报】2026-11-13` |
| 数据更新 | 更新 3184 只，跳过 297 只（已退市等），失败 6 只 |
| 基本面复核 | 对 12 只当晚候选股票重新下载了财务与分红 |
| 重复运行 | 2026-11-16 00:41 用 `qt run-day --day 2026-11-13 --no-update` 再跑一次：信号仍是 20 条，发件箱仍是 2 行，`mail_out/` 仍是 2 个文件，没有任何新增 |

**耗时：远超预期，原因是当晚数据源响应很差**

| 阶段 | 起止 | 耗时 |
|---|---|---|
| 数据更新（日线为主） | 13:47 – 22:18 | 约 8.5 小时 |
| 校验、建面板、基本面复核、推进两本账、写邮件 | 22:18 – 23:45 | 约 87 分钟 |
| 合计 | | 约 10 小时 |

- 数据源响应正常的时段（15:00 – 17:04）每只股票约 2.75 秒，与下面"每日更新耗时实测"一致。
- 13:50 – 15:00 和 17:04 – 22:04 两段，每 20 只股票要 10 到 18 分钟（每只约 50 秒）。
  这是请求反复超时、重试后才成功的表现，最终只有 6 只失败。
- 校验和建面板本身很快：在本机单独计时，数据校验 0.4 秒，建面板 7.5 秒。
  结尾那 87 分钟、以及重复运行的 28 分钟，都花在基本面复核那 12 只股票的联网请求上。
- 2026-11-16 01:10 直接测试数据源，经代理和不经代理的响应都在 0.5 秒以内。
  所以变慢是那几个小时的情况，究竟是数据源限流还是代理线路拥堵，这次没能分辨
  （程序目前不记录每一次重试，无法事后区分）。

**这对日常使用意味着什么**

- 数据源正常时，18:00 开始，约 21:30 结束；数据源差时可能要跑到凌晨。
  期限是下一个交易日 08:30，这次即使 18:00 才开始也来得及，但电脑必须整晚不休眠。
- 邮件到达时间因此不固定，可能是深夜。

**说明**：这次第一遍运行用的是整体审阅修正之前的镜像，重复运行用的是修正之后的镜像。
`mail_out/` 里的两封邮件是修正之前生成的，其中"手动账（请据此操作）"一行的措辞
现已改为"手动账（你的实际账户，信号以此为准）"。

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

### 改用批量行情之后（2026-11-16 起）

上面的外推是逐只请求日线的耗时。现在每晚的日线先用一次新浪批量行情（`stock_zh_a_spot`，
约 15 到 20 秒取回全市场约 5500 只）写入当天的日线，只有对不上的少数股票（当天除权、落后超过一天、
从未下载过、批量数据里没有）才逐只请求；规则见 `docs/data-gaps.md` 第 4 节"每晚日线的批量行情"。
配置项 `data.bulk_bars`（默认 `true`）可以关掉它，回到全部逐只请求。

- 预期：日线从约 162 分钟降到几分钟，整晚约 **50 分钟**（主要剩财务分红约 47 分钟），数据源正常时。
- 实测整晚耗时：**待实测**。
- 每晚是否用了批量行情、批量写入多少只、判为停牌多少只、逐只请求多少只、没用的原因，见 `qt admin runs`
  每天那一行"日线批量行情：……"，以及 worker 日志里"数据更新完成"那一行。
- 批量行情只在当天 16:00 之后、下一个交易日 09:00 之前代表当天；补跑较早的日子、或第二天 09:00 之后
  才运行时，整晚自动用逐只请求（2026-11-16 09:06 实测：新浪批量行情已全部归零，成交量都是 0）。
- 想确认批量行情与逐只接口一致：`docker compose run --rm worker uv run qt data check-snapshot`
  （只读，默认抽 30 只，约 2 分钟；有不一致时退出码为 1）。

### 注意

- 单次请求耗时主要由网络往返与 `request_pause_seconds`（每次请求后 1 秒礼貌间隔）决定，与日线落后几天关系不大。
- 这是单个晚上、单次抽样的数据，数据源的响应速度波动会直接改变结果；整晚任务应按“可能超过 4 小时”设计，
  即中途失败可续传、白天不依赖夜间任务一定已跑完。
- 若要缩短，可调小 `request_pause_seconds`，但有被数据源限流的风险，需要另行评估。
