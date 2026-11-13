# 子阶段 2A：多用户每日运行核心 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让系统每个交易日自动更新数据、为每个使用者推进系统账和手动账、生成信号并发邮件，用命令行管理，先在用户的 Mac 上用 Docker Compose 跑起来开始纸上模拟。

**Architecture:** 市场数据仍是全站共用的 Parquet（第一阶段的 `ParquetStore`），每晚只建一次指标面板。使用者状态放 PostgreSQL。系统账复用回测引擎：把引擎拆成"推进一个交易日"的函数，状态每天序列化入库。手动账是事件溯源：只增不改的流水是唯一事实来源，持仓、现金、份额净值、风控状态全部由流水重放得出。每晚任务分阶段、可重跑、可补跑；邮件先进发件箱再发送。

**Tech Stack:** Python 3.12、uv、SQLAlchemy 2.0（Mapped 类型写法）、Alembic、psycopg 3、PostgreSQL 16、Jinja2、Typer、Docker Compose；沿用 pandas、DuckDB、pydantic、pytest、ruff、pyright。

**Spec:** `docs/superpowers/specs/2026-11-13-phase2-multiuser-service-design.md`（下称"设计"）。执行者必须同时阅读设计的第 4、5、7 节。本计划只覆盖设计 9.1 的子阶段 2A；网页、注册登录、邀请码、HTTPS 属于 2B，不在本计划内。

## Global Constraints

- Python `>=3.12,<3.13`；所有函数参数和返回值带类型标注。
- 零告警：`./scripts/check.sh`（ruff check、ruff format --check、pyright strict、pytest，`filterwarnings = ["error"]`）必须全绿。禁止 `# type: ignore`、`# pyright: ignore`、`# noqa`、`# fmt: skip`。文件级 pyright 放宽只允许出现在 `src/qt/data/akshare_source.py` 和 `scripts/probe_*.py`。第三方库缺类型时在边界处封装。
- ruff 行宽 100，中文字符按两列计。
- 界面、邮件、文档、代码注释、提交说明用中文。
- 不得绕过 `qt.data.source.DataSource` 直接调用 AKShare。
- `qt/service/` 不得引用任何网页框架，也不得引用 `qt/jobs/`、`qt/mail/` 的发送部分（可以写发件箱）。
- 所有属于使用者的表都有 `user_id` 列；`qt/db/repo.py` 里每个业务查询函数的第二个参数都是 `user_id: int`，查不到或不属于该使用者时抛 `NotFoundError`。跨使用者的查询只放在 `qt/db/admin_repo.py`。
- 金额和股数在数据库里用 `double precision`（与引擎的 float 一致，保证系统账与回测逐位相同）；有序集合存 JSONB 时用数组，不依赖对象键顺序。
- 手动账的流水只增不改；任何"修改"都是追加一条冲销记录。
- 密钥只来自环境变量：`DATABASE_URL`、`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`MAIL_FROM`、`ADMIN_EMAIL`、`MAIL_BACKEND`（`smtp` 或 `file`）、`QT_DATA_DIR`、`HTTP_PROXY`、`HTTPS_PROXY`。`.env` 不入库，提供 `.env.example`。
- 措辞：邮件和命令行输出用"信号""提示""按你设定的规则计算"，不出现"建议买入""推荐"。信号类邮件底部固定一行：`以上内容是按你设定的规则计算的结果，不构成投资建议。历史回测不代表未来收益，盈亏自负。`
- 时区：所有"今天""18:00""08:30"按 `Asia/Shanghai`；数据库时间戳用带时区类型。
- 任何任务都不得删除、重写或移动 `data/` 目录；需要数据的测试用 `tests/fakes.py` 的造数函数和临时目录。
- 提交只在分支 `phase2a-daily-core` 上，不推送。提交说明结尾加 `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`。

## Review Focus

设计隐含、最容易在真实使用中出问题的五种情况。每条都已把对应测试加到负责的任务里。

1. **电脑休眠了好几天**：恢复后按顺序补跑每个缺的交易日，系统账逐日推进，只有最新一天的信号发邮件，更早的记为"未通知"。（任务 10）
2. **当天数据不完整或下载失败**：不给任何人出信号，只通知管理员；第二天数据正常后把两天都补上。（任务 10）
3. **任务跑到一半被杀掉后重跑**：没有重复的信号、流水、风控事件和邮件。（任务 6、7、9、10）
4. **使用者录入了过去日期的成交，或卖出数量超过持仓，或录入日期之后有分红**：校验拒绝非法输入；合法的回溯录入重放后分红、净值、风控状态正确，已发邮件不变。（任务 5、7）
5. **手动账持有的股票停牌或退市**：停牌按最后收盘价计值、照常计算；退市发一次"已退市"的卖出信号，不自动清算。（任务 7）

## 文件结构

```
compose.yaml                     db、worker 两个服务（反向代理与网页在 2B 加）
compose.test.yaml                测试用 PostgreSQL（tmpfs，端口 54329）
Dockerfile
.env.example
alembic.ini
migrations/                      Alembic 迁移
scripts/check.sh                 修改：pytest 前拉起测试库
scripts/probe_daily_update.py    实测每日增量耗时
scripts/backup_db.sh
scripts/mac/com.qt.keepawake.plist   用户自行安装的防休眠配置模板
config/default.yaml              修改：新增 service 段
src/qt/config.py                 修改：ServiceConfig
src/qt/models.py                 修改：Position.cash_received、Fill
src/qt/risk.py                   修改：RiskState 导出与恢复
src/qt/backtest/engine.py        修改：EngineState、DayLog、step_day
src/qt/backtest/state.py         引擎状态与 JSON 互转
src/qt/data/update.py            修改：分红空表、滚动刷新、强制刷新
src/qt/db/engine.py              连接与会话
src/qt/db/models.py              全部表定义
src/qt/db/repo.py                带 user_id 的数据访问
src/qt/db/admin_repo.py          跨使用者的数据访问（任务与管理命令用）
src/qt/service/errors.py         NotFoundError、ValidationError
src/qt/service/accounts.py       使用者、订阅、参数
src/qt/service/manual_book.py    手动账重放（纯函数）
src/qt/service/manual_ops.py     手动账录入、校验、每晚推进
src/qt/service/system_book.py    系统账每晚推进与落库
src/qt/service/signals.py        信号状态流转
src/qt/service/journal.py        交易日志与跟踪
src/qt/mail/outbox.py            发件箱
src/qt/mail/render.py            邮件模板渲染
src/qt/mail/send.py              SMTP 与文件两种发送后端
src/qt/mail/templates/*.j2
src/qt/jobs/daily.py             单日运行的各阶段
src/qt/jobs/worker.py            定时循环与补跑
src/qt/admin_cli.py              管理命令，挂到 qt admin
docs/deploy-mac.md、docs/migrate-to-server.md、docs/live-vs-backtest.md
tests/db/、tests/service/、tests/mail/、tests/jobs/
```

## 开工前（由主控完成，不是任务）

- [ ] 第一阶段分支 `phase1-data-backtest` 是否合并到 `main` 由用户决定（走 superpowers:finishing-a-development-branch）。无论是否合并，从当前 `phase1-data-backtest` 的最新提交创建分支 `phase2a-daily-core`。
- [ ] 确认 `docker --version` 可用（已确认：29.9.3）。

---

### Task 1：数据层的三处修正

**Files:**
- Modify: `src/qt/data/akshare_convert.py`、`src/qt/data/update.py`
- Test: `tests/data/test_akshare_source.py`、`tests/data/test_update.py`（沿用现有文件）

**Interfaces:**
- Produces:
  - `refresh_fundamentals(source: DataSource, store: ParquetStore, cfg: AppConfig, symbols: Sequence[str]) -> dict[str, str]`：不看新鲜度，强制重新下载这些股票的分红和财务，返回 `symbol -> 错误信息`（成功的不在里面）。
  - `fundamentals_due(symbol: str, age_days: int | None, today: date, refresh_days: int) -> bool`：滚动刷新判定。

背景：全量下载时 9 只股票报 `KeyError: '除权除息日'`，原因是数据源对没有分红记录的股票返回缺列的空表。另外现在所有股票的分红和财务是同一天下载的，7 天后会在同一晚全部过期，那一晚的更新会回到 6.5 小时。

- [ ] **Step 1：写失败的测试**

```python
def test_dividends_without_history_convert_to_empty_frame() -> None:
    raw = pd.DataFrame()  # 数据源对没有分红记录的股票返回无列空表
    out = normalize_dividends("603293", raw)
    assert list(out.columns) == list(normalize_dividends("600000", SAMPLE_RAW).columns)
    assert len(out) == 0

def test_fundamentals_due_spreads_symbols_over_refresh_days() -> None:
    today = date(2026, 11, 16)
    symbols = [f"{i:06d}" for i in range(600000, 600700)]
    due = [s for s in symbols if fundamentals_due(s, age_days=3, today=today, refresh_days=7)]
    assert 60 <= len(due) <= 140          # 约七分之一
    assert fundamentals_due("600000", None, today, 7)            # 文件缺失必须下载
    assert fundamentals_due("600000", 14, today, 7)              # 超过两轮必须下载
    assert not fundamentals_due("600000", 0, today, 7)           # 当天已下载不重复
    week = [today + timedelta(days=i) for i in range(7)]
    assert sum(fundamentals_due("600000", 3, d, 7) for d in week) == 1   # 每轮恰好轮到一次

def test_refresh_fundamentals_ignores_freshness_and_reports_failures(tmp_path: Path) -> None:
    # 两只股票文件都是今天写的；source 对第二只抛异常
    failed = refresh_fundamentals(source, store, cfg, ["600001", "600002"])
    assert source.dividend_calls == ["600001", "600002"]
    assert list(failed) == ["600002"]
```

`normalize_dividends(symbol, raw)` 是 `akshare_convert.py` 里现有的函数；`SAMPLE_RAW` 用该测试文件里已有的分红样例。还要补一条：缺少 `除权除息日` 列但有其他列的表同样返回空表。

- [ ] **Step 2：运行确认失败** — `uv run pytest tests/data -q -k "without_history or fundamentals_due or refresh_fundamentals"`，预期 3 个失败。
- [ ] **Step 3：实现**
  - 分红转换：输入为空或缺少 `除权除息日` 列时返回列齐全的空表。
  - `fundamentals_due`：`age_days is None` → 真；`age_days == 0` → 假；`age_days >= 2 * refresh_days` → 真；`refresh_days == 0` → 真；否则 `zlib.crc32(symbol.encode()) % refresh_days == today.toordinal() % refresh_days`。
  - `_update_fundamentals` 改用 `fundamentals_due`（分红和财务各自按文件年龄判断）。
  - `refresh_fundamentals` 复用 `_update_fundamentals` 的下载与过滤逻辑，逐只捕获异常。
- [ ] **Step 4：运行确认通过**，再跑 `./scripts/check.sh`。
- [ ] **Step 5：提交** — `fix: 无分红记录的股票不再报错；财务与分红改为滚动刷新并支持强制刷新`

---

### Task 2：容器、数据库基础设施与每日增量耗时实测

**Files:**
- Create: `Dockerfile`、`compose.yaml`、`compose.test.yaml`、`.env.example`、`alembic.ini`、`migrations/env.py`、`migrations/versions/0001_baseline.py`、`src/qt/db/__init__.py`、`src/qt/db/engine.py`、`scripts/probe_daily_update.py`、`tests/db/__init__.py`、`tests/db/conftest.py`、`tests/db/test_engine.py`、`docs/deploy-mac.md`（先只写"实测结果"一节）
- Modify: `pyproject.toml`、`scripts/check.sh`、`.gitignore`

**Interfaces:**
- Produces:
  - `qt.db.engine.make_engine(url: str) -> Engine`、`session_scope(engine: Engine) -> Iterator[Session]`（上下文管理器，正常提交、异常回滚）、`database_url() -> str`（读 `DATABASE_URL`，缺失抛 `RuntimeError`，信息为中文）。
  - pytest fixture `db_engine`（会话级：对测试库执行 `alembic upgrade head`）和 `db`（函数级 `Session`，测试结束后清空所有表）。位于 `tests/db/conftest.py`，其他测试目录通过 `pytest_plugins` 或上移到 `tests/conftest.py` 复用，由实现者选一种。
  - 测试库地址固定为 `postgresql+psycopg://qt:qt@127.0.0.1:54329/qt_test`，可用环境变量 `QT_TEST_DATABASE_URL` 覆盖。

- [ ] **Step 1：加依赖** — `uv add sqlalchemy alembic "psycopg[binary]"`。
- [ ] **Step 2：写 `compose.test.yaml`**（`postgres:16`，`tmpfs` 数据目录，端口 `54329:5432`，健康检查）并修改 `scripts/check.sh`：在 `pytest` 之前执行 `docker compose -f compose.test.yaml up -d --wait`。数据库连不上时测试必须失败，不允许跳过。
- [ ] **Step 3：写失败的测试**

```python
def test_session_scope_commits_and_rolls_back(db_engine: Engine) -> None:
    with session_scope(db_engine) as s:
        s.execute(text("create table t_probe (id int primary key)"))
        s.execute(text("insert into t_probe values (1)"))
    with pytest.raises(RuntimeError), session_scope(db_engine) as s:
        s.execute(text("insert into t_probe values (2)"))
        raise RuntimeError("boom")
    with session_scope(db_engine) as s:
        assert s.execute(text("select count(*) from t_probe")).scalar_one() == 1
        s.execute(text("drop table t_probe"))

def test_database_url_missing_gives_chinese_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        database_url()
```

- [ ] **Step 4：实现 `qt/db/engine.py`、Alembic 配置和空的基线迁移**，运行 `./scripts/check.sh` 全绿。
- [ ] **Step 5：写 `Dockerfile` 和 `compose.yaml`**
  - 镜像：`python:3.12-slim` + uv，安装项目，入口 `qt`。
  - `compose.yaml` 服务：`db`（`postgres:16`，命名卷 `qt_pg`，只在容器网络内暴露，另映射到 `127.0.0.1:54330` 供本机管理命令使用）、`worker`（命令先用 `qt --help` 占位，任务 10 改成 `qt worker`；挂载 `${QT_DATA_DIR:-./data}:/app/data`；`env_file: .env`；`TZ=Asia/Shanghai`）。
  - `.env.example` 列出 Global Constraints 里的全部变量，代理示例写 `http://host.docker.internal:7897`。
- [ ] **Step 6：写 `scripts/probe_daily_update.py` 并在容器里实测**
  - 脚本参数：`--symbols N`（默认 60，从证券表均匀抽样）、`--data-dir`。**把 `data/` 复制到临时目录后在副本上运行，不碰原目录。** 对抽样股票执行一次 `update_all`（`as_of` 取最近已收盘交易日），分别打印日线阶段、财务分红阶段的总耗时和每只平均耗时，并按 3487 只、财务分红每晚约七分之一外推整晚耗时。
  - 运行：`docker compose run --rm worker uv run python scripts/probe_daily_update.py --symbols 60`。这一步同时验证 AKShare 能在容器里安装并经代理访问数据源。
- [ ] **Step 7：检查点**
  - 把实测数字写进 `docs/deploy-mac.md` 的"每日更新耗时实测"一节。
  - 外推整晚耗时 **超过 4 小时**，或 AKShare 在容器里装不上、访问不了：状态报 BLOCKED，写明数字和现象，不继续后面的任务。
- [ ] **Step 8：提交** — `feat: 容器与数据库基础设施，实测每日增量更新耗时`

---

### Task 3：表结构、站点配置、使用者与订阅

**Files:**
- Create: `src/qt/db/models.py`、`src/qt/db/repo.py`、`src/qt/db/admin_repo.py`、`src/qt/service/__init__.py`、`src/qt/service/errors.py`、`src/qt/service/accounts.py`、`migrations/versions/0002_core_tables.py`、`tests/service/test_accounts.py`、`tests/db/test_isolation.py`
- Modify: `src/qt/config.py`、`config/default.yaml`、`tests/test_config.py`

**Interfaces:**
- Produces（`qt.config`）：

```python
class ServiceConfig(_Frozen):
    run_time: str                      # "18:00"
    run_deadline: str                  # "08:30"
    signal_valid_days: PositiveInt     # 3
    abandon_resignal_days: NonNegativeInt   # 20
    rejection_episode_gap_days: PositiveInt # 20
    backdate_limit_days: NonNegativeInt     # 10
    followup_days: list[PositiveInt]        # [20, 60]
```
  `AppConfig` 新增必填字段 `service: ServiceConfig`；`config/default.yaml` 按注释里的值填写。

- Produces（`qt.db.models`，全部表；后续任务只加迁移不改这里已有列的含义）：

| 表 | 列（除 `id`、`created_at` 外） |
|---|---|
| `users` | `email`（小写，唯一）、`password_hash` 可空、`is_admin`、`status`（`active`/`disabled`）、`plan`（默认 `free`）、`email_verified_at`、`terms_version`、`terms_accepted_at` |
| `subscriptions` | `user_id`、`strategy_name`、`params` JSONB（`{"strategy": {...}, "risk": {...}}`）、`initial_cash`、`status`（`active`/`paused`/`closed`）、`start_day`、`closed_at` |
| `param_changes` | `subscription_id`、`user_id`、`old` JSONB、`new` JSONB、`effective_day` |
| `books` | `subscription_id`、`user_id`、`kind`（`system`/`manual`）、`processed_through` 可空日期；唯一 `(subscription_id, kind)` |
| `system_states` | `book_id` 主键、`as_of_day`、`state` JSONB |
| `book_days` | `book_id`、`day`、`equity`、`cash`、`unit_nav`；主键 `(book_id, day)` |
| `ledger_entries` | `book_id`、`user_id`、`kind`（`buy`/`sell`/`transfer_in`/`deposit`/`withdraw`/`void`）、`symbol` 可空、`trade_day`、`effective_day`、`shares`、`price`、`fees`、`fees_estimated`、`cost_price` 可空、`signal_id` 可空、`origin`（`signal`/`self`，可空）、`voids_entry_id` 可空、`created_by_user_id`、`note` |
| `signals` | `book_id`、`user_id`、`side`、`symbol`、`signal_day`、`shares`、`ref_price`、`reason`、`exit_reason` 可空、`metrics` JSONB、`stop_price` 可空、`take_price` 可空、`status`（`created`/`notified`/`executed`/`abandoned`/`voided`/`unnotified`）、`status_day`、`valid_until` 可空、`reminded_day` 可空、`void_note` 可空；唯一 `(book_id, side, symbol, signal_day)` |
| `rejections` | `book_id`、`user_id`、`symbol`、`rule`、`message`、`metrics` JSONB、`first_day`、`last_day` |
| `risk_events` | `book_id`、`user_id`、`day`、`kind`、`drawdown`、`message`；唯一 `(book_id, day, kind)` |
| `trades` | `book_id`、`user_id`、`symbol`、`entry_day`、`exit_day`、`shares`、`cost`、`proceeds`、`cash_received`、`exit_reason`（字符串，手动卖出且无关联信号时为 `manual`）、`holding_days`、`entry_metrics` JSONB、`origin`、`signal_ref_price` 可空、`fill_price` |
| `followups` | `book_id`、`user_id`、`symbol`、`event`（`exit`/`abandoned`/`rejected`）、`event_day`、`base_adj_close`、`ret_20` 可空、`ret_60` 可空；唯一 `(book_id, event, symbol, event_day)` |
| `outbox` | `user_id` 可空、`to_addr`、`kind`、`dedupe_key`（唯一）、`subject`、`body_html`、`body_text`、`status`（`pending`/`sent`/`failed`）、`attempts`、`last_error`、`sent_at` |
| `daily_runs` | `trade_day` 主键、`status`（`running`/`data_failed`/`done`/`done_with_errors`）、`stage`、`started_at`、`finished_at`、`detail` JSONB |
| `audit_log` | `actor_user_id` 可空、`action`、`target`、`detail` JSONB |

- Produces（`qt.service.accounts`）：

```python
def create_user(s: Session, email: str, is_admin: bool = False) -> User
def create_subscription(s: Session, user_id: int, cfg: AppConfig, initial_cash: float,
                        start_day: date, overrides: Mapping[str, object] | None = None) -> Subscription
def update_params(s: Session, user_id: int, subscription_id: int, cfg: AppConfig,
                  overrides: Mapping[str, object], effective_day: date) -> Subscription
def pause_subscription(s: Session, user_id: int, subscription_id: int) -> None
def resume_subscription(s: Session, user_id: int, subscription_id: int) -> None
def close_subscription(s: Session, user_id: int, subscription_id: int) -> None
def subscription_config(cfg: AppConfig, sub: Subscription) -> AppConfig   # 站点配置 + 该订阅的 strategy、risk
```
  - `overrides` 的键写成 `strategy.take_profit_fixed`、`risk.max_positions` 这种路径，复用 `qt.backtest.sweep.override` 做校验。
  - `create_subscription` 同时创建 `system` 和 `manual` 两本账。
- Produces（`qt.service.errors`）：`class NotFoundError(Exception)`、`class ValidationError(Exception)`（消息为中文，可直接给使用者看）。

- [ ] **Step 1：写失败的测试**（`tests/service/test_accounts.py`）

```python
def test_create_subscription_creates_two_books_with_default_params(db: Session) -> None:
    u = create_user(db, "A@Example.com")
    assert u.email == "a@example.com"
    sub = create_subscription(db, u.id, CFG, 150_000.0, date(2026, 11, 16))
    assert sub.params["strategy"]["take_profit_fixed"] == 0.20
    assert {b.kind for b in books_of(db, u.id, sub.id)} == {"system", "manual"}

def test_duplicate_email_rejected(db: Session) -> None:
    create_user(db, "a@example.com")
    with pytest.raises(ValidationError, match="已注册"):
        create_user(db, "A@example.com")

def test_only_one_active_subscription_per_user(db: Session) -> None:
    # 第二次 create_subscription 抛 ValidationError("已有生效的订阅")；close 之后可以再建

def test_lookback_days_and_site_sections_cannot_be_overridden(db: Session) -> None:
    for path in ("strategy.lookback_days", "universe.min_listed_years", "costs.slippage"):
        with pytest.raises(ValidationError, match="全站统一"):
            create_subscription(db, uid, CFG, 1e5, DAY, {path: 1})

def test_invalid_param_value_rejected_with_same_rules_as_config(db: Session) -> None:
    with pytest.raises(ValidationError):
        create_subscription(db, uid, CFG, 1e5, DAY, {"strategy.stop_loss": 1.5})

def test_update_params_records_history_and_keeps_books(db: Session) -> None:
    update_params(db, uid, sub.id, CFG, {"strategy.stop_loss": 0.10}, date(2026, 11, 17))
    (change,) = param_changes_of(db, uid, sub.id)
    assert change.old["strategy"]["stop_loss"] == 0.15
    assert change.new["strategy"]["stop_loss"] == 0.10
    assert change.effective_day == date(2026, 11, 17)
    assert len(books_of(db, uid, sub.id)) == 2

def test_subscription_config_merges_user_params_over_site_config(db: Session) -> None:
    merged = subscription_config(CFG, sub)
    assert merged.strategy.stop_loss == 0.10
    assert merged.costs == CFG.costs and merged.universe == CFG.universe
```

`tests/db/test_isolation.py`：

```python
def test_repo_functions_refuse_other_users_rows(db: Session) -> None:
    # 使用者甲、乙各有一个订阅
    with pytest.raises(NotFoundError):
        repo.get_subscription(db, user_b.id, sub_a.id)
    with pytest.raises(NotFoundError):
        update_params(db, user_b.id, sub_a.id, CFG, {"strategy.stop_loss": 0.1}, DAY)
    with pytest.raises(NotFoundError):
        pause_subscription(db, user_b.id, sub_a.id)

def test_every_repo_function_takes_user_id_second() -> None:
    for name, fn in inspect.getmembers(repo, inspect.isfunction):
        if name.startswith("_") or fn.__module__ != repo.__name__:
            continue
        params = list(inspect.signature(fn).parameters)
        assert params[:2] == ["s", "user_id"], name
```

- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现表定义、迁移 `0002`、`repo`、`admin_repo`、`accounts`。** 每个改动使用者数据的 `accounts` 函数写一条 `audit_log`。
- [ ] **Step 4：运行确认通过**，`./scripts/check.sh` 全绿（`tests/test_config.py` 增加对 `service` 段默认值的断言）。
- [ ] **Step 5：提交** — `feat: 使用者、订阅与全部表结构`

---

### Task 4：引擎拆成"推进一个交易日"，状态可序列化

**Files:**
- Create: `src/qt/backtest/state.py`、`tests/backtest/test_step_equivalence.py`
- Modify: `src/qt/backtest/engine.py`、`src/qt/risk.py`、`src/qt/models.py`、`tests/test_risk.py`

**Interfaces:**
- Produces（`qt.models`）：

```python
@dataclass(frozen=True)
class Fill:
    symbol: str
    side: Side
    day: date
    shares: float
    price: float      # 成交价（含滑点）
    amount: float     # 买入为含费用的支出，卖出为扣费后的收入
    signal_day: date
```

- Produces（`qt.risk`）：

```python
@dataclass(frozen=True)
class RiskState:
    peak: float | None
    warned: bool
    paused: bool
    pause_day: date | None
    pause_drawdown: float
    pause_days: int
    exits: tuple[tuple[str, date], ...]   # 按记录先后

class RiskManager:
    def dump_state(self) -> RiskState
    def load_state(self, state: RiskState) -> None
```

- Produces（`qt.backtest.engine`）：

```python
@dataclass
class EngineState:            # 原 _State 去掉历史列表后的部分
    cash: float
    positions: dict[str, Position]
    sell_orders: list[Order]
    buy_orders: list[Order]
    candidates: dict[str, BuyCandidate]

@dataclass(frozen=True)
class DayLog:
    day: date
    equity: float
    cash: float
    fills: list[Fill]
    trades: list[Trade]
    rejections: list[Rejection]
    risk_events: list[RiskEvent]
    expired_buys: list[Order]
    new_sell_orders: list[Order]
    new_buy_orders: list[Order]

def step_day(state: EngineState, panel: FeaturePanel, strategy: Strategy, risk: RiskManager,
             costs: CostModel, calendar: TradingCalendar, day: date, prev_day: date | None,
             initial_cash: float, buy_order_valid_days: int) -> DayLog
```
  `run_backtest` 的签名和返回值不变，内部改为循环调用 `step_day` 并汇总 `DayLog`。

- Produces（`qt.backtest.state`）：

```python
def dump_state(state: EngineState, risk: RiskState) -> dict[str, object]   # 只含 JSON 类型，有序集合用列表
def load_state(raw: Mapping[str, object]) -> tuple[EngineState, RiskState]
```

- [ ] **Step 1：写失败的测试**（`tests/backtest/test_step_equivalence.py`，数据集用 `tests.fakes.populate_trading_store`，区间用样本内起止并延伸到数据集末尾以覆盖暴跌后的止损与暂停）

```python
def _assert_same(a: BacktestResult, b: BacktestResult) -> None:
    pd.testing.assert_series_equal(a.equity, b.equity)
    pd.testing.assert_series_equal(a.cash, b.cash)
    assert a.trades == b.trades
    assert a.rejections == b.rejections
    assert a.risk_events == b.risk_events
    assert a.expired_buys == b.expired_buys
    assert a.open_positions == b.open_positions

@pytest.mark.parametrize("overrides", [
    {}, {"risk.pause_drawdown": 0.05, "risk.warn_drawdown": 0.03},
    {"strategy.take_profit_mode": "trailing"}, {"risk.cooldown_days": 0},
])
def test_stepping_with_json_roundtrip_every_day_equals_run_backtest(tmp_path, overrides) -> None:
    whole = run_backtest(panel, strategy(), risk(), costs, calendar, start, end, cash, 3)
    raw = dump_state(EngineState(cash=cash, positions={}, sell_orders=[], buy_orders=[],
                                 candidates={}), fresh_risk().dump_state())
    logs: list[DayLog] = []
    for prev, day in pairwise_days:
        state, rstate = load_state(json.loads(json.dumps(raw)))   # 每天都过一遍 JSON
        r = fresh_risk(); r.load_state(rstate)                    # 每天新建风控对象
        logs.append(step_day(state, panel, fresh_strategy(), r, costs, calendar, day, prev, cash, 3))
        raw = dump_state(state, r.dump_state())
    _assert_same(whole, result_from_logs(logs, final_state))

def test_fixture_exercises_stop_loss_take_profit_pause_and_pending_orders() -> None:
    # 防止等价测试退化：至少一组参数下出现止盈、止损、PAUSE 事件，
    # 且至少有一天序列化时 buy_orders 或 sell_orders 非空
```

`tests/test_risk.py` 追加：

```python
def test_risk_state_roundtrip_preserves_pause_and_cooldown() -> None:
    # 触发 PAUSE 和一次止损冷却后 dump → 新对象 load → 之后 70 个交易日
    # on_day_close、size_buys 的输出与原对象逐项相同
```

- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现。** 只做结构调整，七步的逻辑和顺序一行都不改；`Fill` 在撮合成功和退市清算处记录。
- [ ] **Step 4：运行全部测试**，包括第一阶段原有的回测、扫描、防未来函数测试，全部通过即证明没有改变行为。`./scripts/check.sh` 全绿。
- [ ] **Step 5：提交** — `refactor: 回测引擎拆出单日推进函数，状态可序列化`

---

### Task 5：手动账重放（纯函数，不碰数据库）

**Files:**
- Create: `src/qt/service/manual_book.py`、`tests/service/test_manual_book.py`
- Modify: `src/qt/models.py`、`tests/test_models.py`

**Interfaces:**
- Consumes: `RiskManager`、`RiskState`（任务 4）、`CostModel`、`TradingCalendar`。
- Produces（`qt.models.Position`）：新增字段 `cash_received: float = 0.0`；`ret(close)` 改为 `(shares * close + cash_received) / cost - 1`。系统账从不设置该字段，行为不变。
- Produces（`qt.service.manual_book`）：

```python
class EntryKind(StrEnum): BUY, SELL, TRANSFER_IN, DEPOSIT, WITHDRAW, VOID

@dataclass(frozen=True)
class Entry:
    id: int
    kind: EntryKind
    trade_day: date            # 成交日或买入日
    effective_day: date        # 进入净值的交易日；TRANSFER_IN 为录入日起第一个交易日，其余等于 trade_day
    symbol: str | None
    shares: float
    price: float               # TRANSFER_IN 为成本价
    fees: float
    origin: str | None         # "signal" | "self"
    exit_reason: ExitReason | None   # 卖出关联的信号原因，无关联为 None
    voids_entry_id: int | None
    industry: str
    entry_metrics: Mapping[str, float]

@dataclass(frozen=True)
class CorporateAction:         # 由分红表推算出来的展示用流水，不入库
    day: date; symbol: str; cash: float; bonus_shares: float

class Market(Protocol):
    def last_close(self, day: date, symbol: str) -> float: ...
    def dividends(self, symbol: str) -> Sequence[tuple[date, float, float]]: ...  # (除权日, 每股现金, 每股送转)
    def delisted_before(self, day: date, symbol: str) -> bool: ...

@dataclass(frozen=True)
class ManualState:
    positions: dict[str, Position]          # origin 记在 entry_metrics 之外，见 origins
    origins: dict[str, str]
    cash: float
    units: float
    days: list[date]
    equity: list[float]
    unit_nav: list[float]
    trades: list[ManualTrade]               # 字段同 trades 表
    actions: list[CorporateAction]
    risk_events: list[RiskEvent]
    risk: RiskState

def replay(entries: Sequence[Entry], market: Market, calendar: TradingCalendar,
           risk_cfg: RiskConfig, costs: CostModel, start_cash: float,
           start_day: date, through: date) -> ManualState
def validate_entry(state_before: ManualState, new: Entry, calendar: TradingCalendar,
                   today: date, backdate_limit_days: int, known_symbols: Set[str]) -> None  # 不合法抛 ValidationError
```

重放规则（设计 7.6，逐日按此顺序）：

1. 当日除权的持仓：现金分红 `shares × 每股现金` 加到现金，并累加到该持仓的 `cash_received`；送转 `shares × 每股送转` 加到股数。只对除权日之前已持有的股数生效。
2. 应用 `trade_day == 当日` 的买入、卖出、入金、出金，以及 `effective_day == 当日` 的转入。被 `VOID` 指向的流水和 `VOID` 本身都跳过。
   - 买入：现金减 `price × shares + fees`，成本同额；同一股票加仓则股数、成本相加，`entry_day` 保持最早。
   - 卖出：现金加 `price × shares − fees`；按比例减少成本和 `cash_received`；全部卖完时生成一条 `ManualTrade` 并调用 `risk.note_exit(symbol, day, exit_reason)`（`exit_reason` 为 `None` 时不调用）。部分卖出也生成一条 `ManualTrade`（对应卖出的那部分）。
   - 转入：不动现金；持仓成本为 `price × shares`；`entry_day = trade_day`；`days_held` 为 `trade_day` 到当日的交易日数；`peak_return` 用 `trade_day` 到当日每个交易日的收盘价和期间分红补算。
3. 计值：`equity = cash + Σ shares × last_close`；每个持仓 `days_held += 1`、更新 `peak_return`（转入当日不重复加）。
4. 份额净值：首日 `units = start_cash`。当日的入金、出金、转入不改变当日净值：`unit_nav = (equity − 当日净流入) / 前一日 units`，然后 `units += 当日净流入 / unit_nav`。转入的流入额按当日收盘市值计。
5. `risk.on_day_close(day, unit_nav)`，事件记入 `risk_events`。

`validate_entry` 的规则：股票代码在 `known_symbols` 内；`trade_day` 是交易日且不晚于 `today`；买入、卖出、入金、出金的 `trade_day` 距 `today` 不超过 `backdate_limit_days` 个交易日（转入不限）；股数为正且买入、转入为 100 的整数倍（卖出不要求）；价格为正；卖出股数不超过 `trade_day` 当日重放后的持仓；出金不超过当日现金；`VOID` 的目标存在、未被作废、不是 `VOID`，且作废后重放不出现负持仓或负现金。

- [ ] **Step 1：写失败的测试**（每个测试用内存里的假 `Market`）

```python
def test_buy_then_hold_values_position_and_unit_nav() -> None:
    # 起始 100000；D1 以 10.00 买 1000 股，费用 5；D2 收盘 11.00
    st = replay([buy], market, cal, RISK, COSTS, 100_000.0, D1, D2)
    assert st.cash == pytest.approx(89_995.0)
    assert st.equity[-1] == pytest.approx(100_995.0)
    assert st.unit_nav[-1] == pytest.approx(1.00995)
    assert st.positions["600001"].days_held == 2

def test_deposit_and_withdraw_do_not_move_unit_nav() -> None:
    # D2 入金 50000：unit_nav 与不入金时逐日相同，units 增加 50000 / unit_nav(D2)
    # D3 出金 20000：同理

def test_transfer_in_adds_units_at_market_value_and_keeps_user_cost() -> None:
    # 成本价 8.00、1000 股，effective_day 收盘 10.00
    assert st.cash == pytest.approx(100_000.0)                 # 不扣现金
    assert st.units == pytest.approx(100_000.0 + 10_000.0 / nav_d)
    assert st.positions["600002"].ret(10.0) == pytest.approx(0.25)
    assert st.origins["600002"] == "self"

def test_transfer_in_backfills_days_held_and_peak_return() -> None:
    # 买入日在 30 个交易日前，期间最高收盘 12.00、成本 8.00
    assert pos.days_held == 31
    assert pos.peak_return == pytest.approx(0.5)

def test_cash_dividend_goes_to_cash_and_counts_in_return() -> None:
    # 10.00 买 1000 股；除权日每股派 0.5，收盘 9.50
    assert st.cash == pytest.approx(start - 10_005.0 + 500.0)
    assert pos.cash_received == pytest.approx(500.0)
    assert pos.ret(9.5) == pytest.approx((9_500.0 + 500.0) / 10_005.0 - 1)   # 不因除息而变成亏损

def test_bonus_shares_increase_shares() -> None:
    # 每股送转 0.5 → 1000 股变 1500 股

def test_dividend_applies_only_to_shares_held_before_ex_date() -> None:
    # 除权日当天买入的股数不享受这次分红

def test_partial_sell_scales_cost_and_emits_trade() -> None:
    # 1000 股卖 400：剩余成本为原来的 60%，trades 里一条 shares == 400 的记录

def test_full_sell_with_stop_loss_reason_starts_cooldown() -> None:
    # 卖出 exit_reason=STOP_LOSS → st.risk.exits 含该股票；exit_reason=None 则不含

def test_voided_entry_and_its_void_are_ignored() -> None:
    assert replay([buy, void_of_buy], ...).positions == {}

def test_backdated_entry_replays_dividend_between_trade_day_and_today() -> None:
    # 买入日在 5 个交易日前，其后第 2 天除权：重放结果含这笔分红

def test_drawdown_pause_event_from_unit_nav_not_from_withdrawal() -> None:
    # 出金 30% 不触发 PAUSE；持仓真实下跌使 unit_nav 回撤 20% 才触发

@pytest.mark.parametrize("bad, message", [
    (sell_more_than_held, "超过持仓"), (trade_on_weekend, "不是交易日"),
    (trade_in_future, "不能晚于今天"), (buy_11_days_ago, "最多可以早于今天 10 个交易日"),
    (odd_lot_buy, "100 的整数倍"), (unknown_symbol, "代码不存在"),
    (withdraw_more_than_cash, "超过现金"), (void_twice, "已作废"),
    (void_buy_that_was_later_sold, "作废后持仓为负"),
])
def test_validate_entry_rejects(bad, message) -> None:
    with pytest.raises(ValidationError, match=message):
        validate_entry(state, bad, cal, TODAY, 10, KNOWN)

def test_transfer_in_may_have_any_past_buy_date() -> None:
    validate_entry(state, transfer_in_bought_3_years_ago, cal, TODAY, 10, KNOWN)   # 不抛
```

- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现。** `tests/test_models.py` 增加 `Position.ret` 含 `cash_received` 的断言。
- [ ] **Step 4：运行确认通过**，`./scripts/check.sh` 全绿（第一阶段测试不得有变化）。
- [ ] **Step 5：提交** — `feat: 手动账重放——份额净值、分红送转、回溯录入与校验`

---

### Task 6：系统账每晚推进与落库

**Files:**
- Create: `src/qt/service/system_book.py`、`src/qt/service/signals.py`、`tests/service/test_system_book.py`
- Modify: `src/qt/db/repo.py`、`src/qt/db/admin_repo.py`

**Interfaces:**
- Consumes: `step_day`、`dump_state`、`load_state`（任务 4）；`subscription_config`（任务 3）。
- Produces：

```python
def advance_system_book(s: Session, sub: Subscription, cfg: AppConfig, panel: FeaturePanel,
                        calendar: TradingCalendar, day: date) -> DayLog | None
```
  - `books.processed_through >= day` 时什么都不做，返回 `None`（可重跑的依据）。
  - 首次运行时状态为全部现金 `sub.initial_cash`，`prev_day` 为 `None`。
  - 一次调用内完成：读状态 → `step_day` → 写 `system_states`、`book_days`（`unit_nav = equity / initial_cash`）、`ledger_entries`（每个 `Fill` 一条，`origin="signal"`）、`signals`、`rejections`、`risk_events`、`trades`、`books.processed_through`。调用方负责事务边界。
  - 信号流转：`new_buy_orders`、`new_sell_orders` → `signals` 状态 `created`（系统账不发邮件，不会变成 `notified`）；成交 → `executed`；`expired_buys` → `voided`，`void_note` 写明"超过有效期未成交"或"暂停买入时取消"。
  - `trades.shares` 取对应卖出 `Fill` 的股数，`fill_price` 取其成交价，`signal_ref_price` 取卖出信号当日收盘价。
  - `rejections` 按"连续出现算一次"入库：同一 `(book, symbol, rule)` 最近一条的 `last_day` 距 `day` 不超过 `cfg.service.rejection_episode_gap_days` 个交易日时只更新 `last_day`，否则新建。
- Produces（`qt.service.signals`）：`record_rejection(...)`、`mark_signal(s, signal, status, day, note=None)`，手动账复用。

- [ ] **Step 1：写失败的测试**

```python
def test_daily_advance_through_database_equals_run_backtest(db: Session, tmp_path: Path) -> None:
    # populate_trading_store；逐日调用 advance_system_book，每天之后 db.commit() 并 db.expire_all()
    whole = run_period(subscription_config(CFG, sub), store, start, end)
    assert [r.equity for r in book_days] == pytest.approx(list(whole.equity), rel=0, abs=0)
    assert [(t.symbol, t.entry_day, t.exit_day, t.cost, t.proceeds, t.exit_reason) for t in trades] \
        == [(t.symbol, t.entry_day, t.exit_day, t.cost, t.proceeds, t.exit_reason.value)
            for t in whole.trades]
    assert {(e.day, e.kind) for e in risk_events} == {(e.day, e.kind.value) for e in whole.risk_events}

def test_advance_is_idempotent_for_same_day(db: Session) -> None:
    advance_system_book(db, sub, CFG, panel, cal, day); db.commit()
    counts = table_counts(db)
    assert advance_system_book(db, sub, CFG, panel, cal, day) is None
    assert table_counts(db) == counts

def test_param_change_takes_effect_from_next_advance_without_resetting_book(db: Session) -> None:
    # 推进到 D，update_params(stop_loss=0.10)，推进 D+1：持仓和现金延续，D+1 的卖出判断用 0.10

def test_signal_statuses_follow_fills_and_expiry(db: Session) -> None:
    # 买入信号次日成交 → executed；涨停买不进满 3 天 → voided 且 void_note 含"超过有效期"

def test_repeated_rejection_is_one_episode(db: Session) -> None:
    # 同一股票连续 5 晚因 max_positions 被拒 → rejections 一行，first_day/last_day 相差 4 个交易日
```

- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现。**
- [ ] **Step 4：运行确认通过**，`./scripts/check.sh` 全绿。
- [ ] **Step 5：提交** — `feat: 系统账逐日推进并落库，与回测逐项一致`

---

### Task 7：手动账的录入与每晚推进

**Files:**
- Create: `src/qt/service/manual_ops.py`、`src/qt/service/market.py`、`tests/service/test_manual_ops.py`
- Modify: `src/qt/db/repo.py`

**Interfaces:**
- Consumes: `replay`、`validate_entry`、`Entry`（任务 5）；`mark_signal`、`record_rejection`（任务 6）。
- Produces（`qt.service.market`）：`class StoreMarket`，用 `FeaturePanel` 和 `ParquetStore` 实现 `manual_book.Market`。
- Produces（`qt.service.manual_ops`）：

```python
def record_trade(s: Session, user_id: int, subscription_id: int, market: Market, calendar: TradingCalendar,
                 cfg: AppConfig, today: date, side: Side, symbol: str, trade_day: date,
                 price: float, shares: float, fees: float | None = None) -> LedgerEntry
def record_holding(s, user_id, subscription_id, market, calendar, cfg, today,
                   symbol: str, buy_day: date, cost_price: float, shares: float) -> LedgerEntry
def record_cash(s, user_id, subscription_id, market, calendar, cfg, today,
                kind: Literal["deposit", "withdraw"], day: date, amount: float) -> LedgerEntry
def void_entry(s, user_id, subscription_id, market, calendar, cfg, today, entry_id: int) -> LedgerEntry
def manual_state(s, user_id, subscription_id, market, calendar, cfg, through: date) -> ManualState
def advance_manual_book(s: Session, sub: Subscription, cfg: AppConfig, panel: FeaturePanel,
                        market: Market, calendar: TradingCalendar, day: date,
                        notify: bool) -> ManualDayResult
@dataclass(frozen=True)
class ManualDayResult:
    new_buy_signals: list[Signal]; new_sell_signals: list[Signal]
    reminders: list[Signal]            # 需要再提醒一次的卖出信号
    abandoned: list[Signal]            # 当晚到期被放弃的买入信号
    risk_events: list[RiskEventRow]    # 当晚新出现的
    state: ManualState
```

录入规则：
- `fees` 为 `None` 时用 `CostModel` 估算并置 `fees_estimated=True`。
- 价格在当日最高最低价之外、或卖出违反 T+1 时不拒绝，在返回的 `LedgerEntry.note` 里写提示。
- 自动关联：买入在同一本账里找 `symbol` 相同、状态为 `created`/`notified`、`signal_day < trade_day <= valid_until` 的买入信号，找到则 `origin="signal"`、信号置 `executed`；找不到则 `origin="self"`。卖出关联未结的卖出信号，同理，关联到的 `exit_reason` 写入流水。
- 每次录入后重放整本账，重写该账的 `book_days` 和 `trades`（派生表，先删后写）；`risk_events` 只插入不存在的，不删除。写 `audit_log`。

每晚推进（`books.processed_through >= day` 时直接返回空结果）：
1. `replay` 到 `day`。
2. 卖出：对重放后的持仓调用 `strategy.sell_signals(day, snapshot, positions)`。已退市（`market.delisted_before`）的持仓生成 `exit_reason=DELISTED` 的卖出信号。
   - 该持仓没有未结卖出信号 → 新建。
   - 已有未结卖出信号且今晚仍触发 → 不新建；`signal_day` 起满 `signal_valid_days` 个交易日且 `reminded_day` 为空 → 放入 `reminders` 并记 `reminded_day`。
   - 已有未结卖出信号但今晚不再触发 → 置 `voided`，`void_note="条件不再满足"`。
3. 买入信号到期：`valid_until < day` 仍未执行的 → `abandoned`。
4. 买入：未暂停时，`held = 持仓 ∪ 未结买入信号的股票`；`strategy.buy_candidates(day, snapshot, held)`；去掉最近 `abandon_resignal_days` 个交易日内在本账被放弃过的股票；`risk.size_buys(day, candidates, equity, cash, positions, closes, pending_buys=未结买入信号对应的 Order)`；通过的建信号（`valid_until = calendar.offset(day, signal_valid_days)`、`ref_price=当日收盘`、`stop_price`、`take_price` 按订阅参数算），被拒的 `record_rejection`。暂停期间不调用 `size_buys`。
5. 当晚触发 PAUSE 时，未结的买入信号置 `voided`，`void_note="暂停买入时取消"`。
6. 新信号状态：`notify` 为真 → `notified`，否则 `unnotified`。
7. 写 `book_days`、`trades`、`risk_events`、`processed_through`。

- [ ] **Step 1：写失败的测试**（数据用 `populate_trading_store` 或在其基础上扩展的小数据集）

```python
def test_buy_signal_sized_from_manual_book_cash(db) -> None:
    # 手动账现金 100000、max_position_pct 0.1、收盘 10.00 → 信号 1000 股（扣费后向下取整到一手）
    assert sig.status == "notified" and sig.valid_until == cal.offset(day, 3)
    assert sig.stop_price == pytest.approx(10.0 * 0.85) and sig.take_price == pytest.approx(12.0)

def test_recording_buy_within_validity_links_signal(db) -> None:
    e = record_trade(..., Side.BUY, "600001", next_day, 10.05, 1000)
    assert e.origin == "signal" and reload(sig).status == "executed"

def test_buy_without_signal_is_self_directed(db) -> None:
    assert record_trade(..., Side.BUY, "600004", day, 9.9, 500).origin == "self"

def test_unacted_buy_signal_reserves_cash_and_slot(db) -> None:
    # max_positions=1：第一晚出一个信号；第二晚即使有其他合格候选也不再出买入信号

def test_buy_signal_abandoned_after_three_trading_days_then_blocked_for_20(db) -> None:
    # 第 4 个交易日晚上 → abandoned，出现在 result.abandoned；之后 20 个交易日内该股票不再出信号；第 21 个交易日可以

def test_sell_signal_emitted_once_reminded_once_then_silent(db) -> None:
    # 止损触发：第一晚 new_sell_signals 有它；第 2、3 晚没有；满 3 个交易日那晚 reminders 有它；之后都没有

def test_sell_signal_voided_when_condition_no_longer_holds(db) -> None:
    # 止盈触发后价格回落到止盈线以下 → voided，void_note == "条件不再满足"；再次触发时新建

def test_recording_sell_links_signal_and_starts_cooldown(db) -> None:
    # 关联到 STOP_LOSS 信号的卖出 → trades.exit_reason == "stop_loss"；冷却期内该股票的候选被 cooldown 拒绝

def test_self_directed_holding_gets_sell_checks_and_counts_in_risk(db) -> None:
    # record_holding 成本 12.00，现价 10.00（亏 16.7%）→ 当晚出止损卖出信号；
    # 它所在行业的占比计入 industry_cap

def test_pause_voids_open_buy_signals_and_stops_new_ones(db) -> None:

def test_suspended_holding_valued_at_last_close(db) -> None:
    # 持仓股票当日无日线：equity 用最后收盘价，不报错，不出卖出信号以外的异常

def test_delisted_holding_emits_delisted_sell_signal_once(db) -> None:
    assert [s.exit_reason for s in result.new_sell_signals] == ["delisted"]
    # 次晚不重复

def test_backdated_trade_rewrites_derived_rows_but_not_signals(db) -> None:
    # 录入 3 个交易日前的买入：book_days 从那天起改变；此前已 notified 的信号行一字未改

def test_advance_manual_book_idempotent(db) -> None:
    # 同一天调用两次，第二次返回空结果，表行数不变

def test_not_notify_marks_signals_unnotified(db) -> None:
    assert all(s.status == "unnotified" for s in advance_manual_book(..., notify=False).new_buy_signals)

def test_other_user_cannot_record_or_void(db) -> None:
    with pytest.raises(NotFoundError):
        record_trade(db, other_user.id, sub.id, ...)
    with pytest.raises(NotFoundError):
        void_entry(db, other_user.id, sub.id, ..., entry.id)
```

- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现。**
- [ ] **Step 4：运行确认通过**，`./scripts/check.sh` 全绿。
- [ ] **Step 5：提交** — `feat: 手动账录入、信号生命周期与每晚推进`

---

### Task 8：交易日志的跟踪记录

**Files:**
- Create: `src/qt/service/journal.py`、`tests/service/test_journal.py`

**Interfaces:**
- Produces：

```python
def open_followups(s: Session, book: Book, day: date, market: Market) -> int
def fill_followups(s: Session, calendar: TradingCalendar, market: AdjMarket, day: date,
                   horizons: Sequence[int]) -> int     # 返回本次填上的格子数
class AdjMarket(Protocol):
    def adj_close(self, day: date, symbol: str) -> float | None: ...   # 复权收盘价，无数据为 None
```
  - `open_followups`：为该账在 `day` 发生的事件各建一条（唯一键保证不重复）：`trades` 里 `exit_day == day` 的（`exit`）、当晚变成 `abandoned` 的买入信号（`abandoned`）、`first_day == day` 的 `rejections`（`rejected`）。`base_adj_close` 取事件日的复权收盘价。
  - `fill_followups`：对 `ret_20`、`ret_60` 为空的记录，事件日之后第 20、60 个交易日已到达且有价格时填入 `adj_close / base − 1`；到期日停牌则用其后第一个有价格的交易日；已退市则留空。
  - `StoreMarket`（任务 7）增加 `adj_close` 实现。

- [ ] **Step 1：写失败的测试**

```python
def test_followup_created_for_exit_abandoned_and_first_rejection(db) -> None:
    assert {(f.event, f.symbol) for f in followups} == {
        ("exit", "600002"), ("abandoned", "600003"), ("rejected", "600004")}

def test_followup_not_duplicated_on_rerun_or_repeated_rejection(db) -> None:

def test_ret_20_and_60_filled_on_their_days_with_adjusted_prices(db) -> None:
    # 事件日复权价 10.0；第 20 个交易日 11.0；第 60 个交易日 9.0；期间有一次除权（未复权价跳变不影响）
    assert f.ret_20 == pytest.approx(0.10) and f.ret_60 == pytest.approx(-0.10)

def test_followup_waits_when_horizon_not_reached(db) -> None:
    # 第 19 个交易日运行：ret_20 仍为空

def test_followup_uses_next_available_price_when_suspended(db) -> None:
```

- [ ] **Step 2–4：确认失败、实现、确认通过，`./scripts/check.sh` 全绿。**
- [ ] **Step 5：提交** — `feat: 卖出、放弃、被否决之后的走势跟踪`

---

### Task 9：邮件——发件箱、模板、发送

**Files:**
- Create: `src/qt/mail/__init__.py`、`src/qt/mail/outbox.py`、`src/qt/mail/render.py`、`src/qt/mail/send.py`、`src/qt/mail/templates/{base,buy_signal,sell_signal,risk_alert,abandon_reminder,weekly,admin_alert}.html.j2`、`tests/mail/test_outbox.py`、`tests/mail/test_render.py`、`tests/mail/test_send.py`
- Modify: `pyproject.toml`（把模板打进包，参照第一阶段报告模板的做法）

**Interfaces:**
- Produces（`qt.mail.outbox`）：

```python
def enqueue(s: Session, user_id: int | None, to_addr: str, kind: str, dedupe_key: str,
            subject: str, body_html: str, body_text: str) -> bool   # 已存在同 dedupe_key 返回 False，不写入
def send_pending(s: Session, sender: Sender, max_attempts: int = 5) -> tuple[int, int]   # (成功数, 失败数)
```
- Produces（`qt.mail.send`）：`class Sender(Protocol): def send(self, to_addr: str, subject: str, html: str, text: str) -> None`；`SmtpSender`（标准库 `smtplib`，STARTTLS 或 SSL 按端口 465 判断）、`FileSender(directory: Path)`（每封写一个 `.eml`）；`make_sender() -> Sender` 按 `MAIL_BACKEND` 选择，缺少必要环境变量时抛中文 `RuntimeError`。
- Produces（`qt.mail.render`）：

```python
@dataclass(frozen=True)
class Mail: subject: str; html: str; text: str
def render_buy_signals(day: date, manual: Sequence[SignalView], system: Sequence[SignalView],
                       books: BooksSummary) -> Mail
def render_sell_signals(day: date, manual: Sequence[SignalView], system: Sequence[SignalView],
                        books: BooksSummary, reminder: bool) -> Mail
def render_risk_alert(day: date, event: RiskEventView, books: BooksSummary) -> Mail
def render_abandon_reminder(day: date, signals: Sequence[SignalView]) -> Mail
def render_weekly(week_end: date, report: WeeklyView) -> Mail
def render_admin_alert(day: date | None, title: str, lines: Sequence[str]) -> Mail
```
  `SignalView`、`BooksSummary`、`RiskEventView`、`WeeklyView` 是只含展示字段的冻结数据类，定义在 `render.py`，由任务 10 从数据库行组装。

固定文案：
- 买入信号标题：`【买入信号】{day} {n} 只`；卖出：`【卖出信号】{day} {n} 只`，再提醒时为 `【卖出信号·再次提醒】…`；风控：`【风控提醒】回撤预警` / `【风控提醒】暂停买入` / `【风控提醒】恢复买入`；放弃：`【信号已过期】{n} 只未录入`；周报：`【周报】{week_end}`；管理员：`【系统异常】{title}`。
- 正文先写"手动账（请据此操作）"，后写"系统账（自动执行，仅供对照）"。
- 买入信号每只股票包含：代码、名称、数量、参考价及"今日收盘价，实际成交在下一个交易日，价格会有出入"、各项指标、买入理由、止损位、止盈位、录入命令 `qt admin trade buy …`（2B 换成网页链接）。
- 信号类邮件（买入、卖出、放弃提醒、周报）底部带 Global Constraints 里的固定风险提示；管理员邮件不带。

- [ ] **Step 1：写失败的测试**

```python
def test_enqueue_same_dedupe_key_only_once(db) -> None:
    assert enqueue(db, 1, "a@x.com", "buy_signal", "buy:1:2026-11-16", "s", "h", "t") is True
    assert enqueue(db, 1, "a@x.com", "buy_signal", "buy:1:2026-11-16", "s2", "h2", "t2") is False
    assert count(db, Outbox) == 1

def test_send_pending_marks_sent_and_retries_failures_up_to_limit(db) -> None:
    # 发送器前两次抛异常：attempts 递增、status 仍为 pending、last_error 有内容；第三次成功 → sent、sent_at 非空
    # 一直失败到第 5 次 → failed，之后 send_pending 不再尝试

def test_send_pending_does_not_resend_sent_mail(db) -> None:

def test_buy_signal_mail_content() -> None:
    m = render_buy_signals(DAY, [view], [sys_view], books)
    assert m.subject == "【买入信号】2026-11-16 1 只"
    for needle in ("手动账（请据此操作）", "系统账（自动执行，仅供对照）", "600001", "1000 股",
                   "10.02", "实际成交在下一个交易日", "止损位", "止盈位", "不构成投资建议"):
        assert needle in m.html and needle in m.text
    assert m.html.index("手动账") < m.html.index("系统账")
    for banned in ("建议买入", "推荐"):
        assert banned not in m.html

def test_sell_reminder_subject_and_reason_label() -> None:
    assert render_sell_signals(DAY, [v], [], books, reminder=True).subject.startswith("【卖出信号·再次提醒】")
    # 正文含中文的退出原因（价格止损、固定止盈、基本面止损、时间止损、已退市）

def test_admin_alert_has_no_disclaimer() -> None:

def test_file_sender_writes_eml(tmp_path) -> None:

def test_make_sender_missing_smtp_settings_raises_chinese_error(monkeypatch) -> None:
```

- [ ] **Step 2–4：确认失败、实现、确认通过，`./scripts/check.sh` 全绿。**
- [ ] **Step 5：提交** — `feat: 邮件发件箱、模板与发送`

---

### Task 10：每日任务、补跑与定时进程

**Files:**
- Create: `src/qt/jobs/__init__.py`、`src/qt/jobs/daily.py`、`src/qt/jobs/worker.py`、`tests/jobs/test_daily.py`、`tests/jobs/test_worker.py`
- Modify: `src/qt/cli.py`（增加 `qt worker`、`qt run-day`）、`compose.yaml`（worker 命令改为 `qt worker`）

**Interfaces:**
- Consumes: 任务 1 的 `refresh_fundamentals`；任务 6、7、8、9 的全部产出；第一阶段的 `update_all`、`check_integrity`、`latest_closed_day`、`load_calendar`、`build_panel`。
- Produces（`qt.jobs.daily`）：

```python
@dataclass(frozen=True)
class RunContext:
    engine: Engine; store: ParquetStore; source: DataSource; cfg: AppConfig
    sender: Sender; admin_email: str; now: Callable[[], datetime]

def run_day(ctx: RunContext, day: date, notify: bool, update_data: bool) -> str   # 返回 daily_runs.status
def pending_days(s: Session, calendar: TradingCalendar, as_of: date, first_day: date | None) -> list[date]
def catch_up(ctx: RunContext) -> list[tuple[date, str]]
```
- Produces（`qt.jobs.worker`）：`def run_forever(ctx: RunContext, sleep: Callable[[float], None] = time.sleep) -> None`、`def is_due(now: datetime, run_time: str, last_attempt: datetime | None) -> bool`。

`run_day` 的阶段（`daily_runs.stage` 依次为 `data`、`check`、`panel`、`users`、`mail`、`done`；重跑时已完成的使用者靠 `books.processed_through` 跳过）：

1. `data`：`update_data` 为真时刷新交易日历并 `update_all(as_of=day)`。抛 `SharedTableError`、`NetworkOutageError` → 状态 `data_failed`，给管理员发告警，返回。
2. `check`：`check_integrity(store, calendar, day)` 有问题 → `data_failed`，告警，返回。**此后不得为任何使用者生成信号。**
3. `panel`：`build_panel(store, calendar, cfg, day, day)`。然后做基本面复核：收集所有生效订阅两本账的持仓，加上各订阅用当晚快照初筛出的买入候选（只调用 `strategy.buy_candidates`，不落库），取并集，调用 `refresh_fundamentals`；并集非空则重建面板。刷新失败的股票记入 `detail` 并告警，但不中止。
4. `users`：对每个 `active` 订阅，在各自独立的事务里依次 `advance_system_book`、`advance_manual_book(notify=notify)`、`open_followups`、组装邮件写入发件箱。抛异常则回滚该使用者的事务、记入 `detail["user_errors"]`、给管理员发告警，继续下一个。全部使用者处理完后调用一次 `fill_followups`。
5. `mail`：`notify` 为真且当日是本周最后一个交易日时，为每个开启周报的使用者写周报。`send_pending`。SMTP 失败不影响运行状态，留在发件箱下次重试。
6. 状态：有使用者出错为 `done_with_errors`，否则 `done`。

邮件去重键：`buy:{book_id}:{day}`、`sell:{book_id}:{day}`、`sell-remind:{book_id}:{day}`、`risk:{book_id}:{day}:{kind}`、`abandon:{book_id}:{day}`、`weekly:{user_id}:{week_end}`、`admin:{day}:{title}`。

`catch_up`：
- `as_of = latest_closed_day(now, calendar)`。待处理日 = 最早的生效订阅 `start_day`（没有订阅则为空）到 `as_of` 之间，`daily_runs` 里不是 `done`/`done_with_errors` 的交易日，按日期升序。
- 先做一次数据更新（`update_all(as_of=as_of)`），之后逐日 `run_day(day, notify=(day == as_of), update_data=False)`。
- 某一天返回 `data_failed` 时停止，不处理更晚的日子（保证系统账按顺序推进）。

`run_forever`：每 60 秒醒一次（不用一次性长睡眠，电脑休眠恢复后能立刻发现已过点）；启动时和每次 `is_due` 为真时调用 `catch_up`；`is_due` 在当前时间已过当日 `run_time`、当日是交易日、且今天还没尝试过时为真；`data_failed` 之后每 30 分钟重试一次，直到次日 `run_deadline`，届时仍未完成则给管理员发 `【系统异常】每日运行超时`（每个交易日只发一次）。

- [ ] **Step 1：写失败的测试**（数据用 `populate_trading_store` 写到临时目录；数据源用测试里的假 `DataSource`；发送器用收集到列表的假 `Sender`；`now` 可控）

```python
def test_run_day_advances_both_books_and_sends_one_buy_mail(ctx, db) -> None:
    assert run_day(ctx, DAY, notify=True, update_data=False) == "done"
    assert processed_through(db, "system") == processed_through(db, "manual") == DAY
    assert [m.subject for m in sent] == ["【买入信号】… 4 只"]   # 用实际日期

def test_rerun_same_day_creates_no_duplicates(ctx, db) -> None:
    run_day(ctx, DAY, True, False); before = table_counts(db); n = len(sent)
    assert run_day(ctx, DAY, True, False) == "done"
    assert table_counts(db) == before and len(sent) == n

def test_integrity_failure_emits_no_signals_and_alerts_admin(ctx, db) -> None:
    # 当日日线只写入 50% 的股票
    assert run_day(ctx, DAY, True, False) == "data_failed"
    assert count(db, Signal) == 0 and processed_through(db, "system") is None
    assert [m.to_addr for m in sent] == [ctx.admin_email]
    assert sent[0].subject.startswith("【系统异常】")

def test_catch_up_processes_missing_days_in_order_and_notifies_only_latest(ctx, db) -> None:
    # 上次成功是 5 个交易日前
    result = catch_up(ctx)
    assert [d for d, _ in result] == five_days and all(st == "done" for _, st in result)
    statuses = {s.signal_day: s.status for s in manual_signals(db)}
    assert all(v == "unnotified" for d, v in statuses.items() if d < five_days[-1])
    assert all(v == "notified" for d, v in statuses.items() if d == five_days[-1])
    # 系统账与一次性回测一致
    assert equity_series(db, "system") == pytest.approx(list(run_period(...).equity), abs=0)

def test_catch_up_stops_at_first_data_failure_then_resumes_next_time(ctx, db) -> None:
    # 第 2 天数据不完整：第一次 catch_up 只完成第 1 天；补齐数据后再次 catch_up 完成第 2、3 天

def test_one_user_error_does_not_block_others(ctx, db, monkeypatch) -> None:
    # 让使用者甲的 advance_manual_book 抛异常
    assert run_day(ctx, DAY, True, False) == "done_with_errors"
    assert processed_through_of(db, user_a, "system") is None        # 甲整体回滚
    assert processed_through_of(db, user_b, "manual") == DAY
    assert any(m.to_addr == ctx.admin_email for m in sent)
    # 修复后重跑：甲被补上，乙不重复

def test_killed_between_users_and_mail_then_rerun_sends_each_mail_once(ctx, db) -> None:
    # 让 send_pending 第一次抛异常模拟进程中断；重跑后每封邮件恰好发出一次

def test_smtp_failure_keeps_run_done_and_mail_pending(ctx, db) -> None:

def test_fundamentals_rechecked_for_holdings_and_candidates_before_signals(ctx, db) -> None:
    # 候选股票在数据源里有一条新的分红削减记录（本地文件是旧的）
    run_day(ctx, DAY, True, False)
    assert source.dividend_calls 包含该候选和全部持仓，且不包含其他股票
    assert 该候选没有出现在当晚的买入信号里

def test_weekly_mail_only_on_last_trading_day_of_week_and_respects_user_switch(ctx, db) -> None:

def test_paused_and_closed_subscriptions_are_skipped(ctx, db) -> None:

def test_is_due_and_deadline() -> None:
    sh = ZoneInfo("Asia/Shanghai")
    assert not is_due(datetime(2026, 11, 16, 17, 59, tzinfo=sh), "18:00", None)
    assert is_due(datetime(2026, 11, 16, 18, 0, tzinfo=sh), "18:00", None)
    assert not is_due(datetime(2026, 11, 16, 18, 5, tzinfo=sh), "18:00",
                      datetime(2026, 11, 16, 18, 0, tzinfo=sh))

def test_worker_alerts_once_when_deadline_passes_without_success(ctx, db) -> None:
    # 驱动 run_forever 若干个假的 60 秒节拍，数据一直失败，跨过次日 08:30
    assert [m.subject for m in sent].count("【系统异常】每日运行超时") == 1

def test_worker_runs_catch_up_immediately_after_wake_from_sleep(ctx, db) -> None:
    # 假时钟从周一 17:00 直接跳到周三 09:00：下一个节拍内补完周一、周二
```

- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现。** 周报内容：两本账的份额净值与本周涨跌、持仓盈亏、本周信号与成交、未处理的卖出信号、与 `cfg.backtest.benchmarks` 同期涨跌的对比。
- [ ] **Step 4：运行确认通过**，`./scripts/check.sh` 全绿。
- [ ] **Step 5：提交** — `feat: 每日任务、补跑与定时进程`

---

### Task 11：管理命令

**Files:**
- Create: `src/qt/admin_cli.py`、`tests/test_admin_cli.py`
- Modify: `src/qt/cli.py`（`app.add_typer(admin_app, name="admin")`）

**Interfaces:**
- Consumes: `accounts`、`manual_ops`、`repo`、`admin_repo`、`run_day`。
- Produces（全部命令的输出为中文；业务错误打印 `ValidationError`/`NotFoundError` 的消息并以 1 退出，不打印调用栈）：

| 命令 | 作用 |
|---|---|
| `qt admin user add --email E [--admin]` | 创建使用者 |
| `qt admin user list` | 列出使用者（邮箱、状态、是否有生效订阅） |
| `qt admin sub create --email E --cash N [--set path=value …]` | 创建订阅，`start_day` 为下一个交易日 |
| `qt admin sub set --email E --set path=value …` | 改参数，下一次运行生效 |
| `qt admin sub show --email E` | 当前参数，与默认值不同的标出 |
| `qt admin sub pause / resume / close --email E` | |
| `qt admin trade buy / sell --email E SYMBOL --price P --shares N [--day D] [--fees F]` | 录入成交，`--day` 默认今天之前最近的交易日 |
| `qt admin holding add --email E SYMBOL --cost P --shares N --bought D` | 录入已有持仓 |
| `qt admin cash deposit / withdraw --email E --amount A [--day D]` | 入金出金 |
| `qt admin entry void --email E ID` | 作废一条流水 |
| `qt admin book show --email E [--book manual\|system]` | 净值、现金、持仓（成本、现价、收益、来源）、最近 20 条流水（含推算出的分红送转） |
| `qt admin signals --email E [--book …] [--status …] [--days 30]` | 信号列表 |
| `qt admin journal --email E` | 已完成的交易与跟踪记录 |
| `qt admin runs [--last 10]` | 每日运行记录与失败原因 |
| `qt run-day [--day D] [--no-notify] [--no-update]` | 手动触发某日运行 |

- [ ] **Step 1：写失败的测试**（用 Typer 的 `CliRunner`，数据库用测试库）

```python
def test_user_and_subscription_lifecycle_via_cli(runner, db) -> None:
    assert invoke("admin", "user", "add", "--email", "a@x.com").exit_code == 0
    r = invoke("admin", "sub", "create", "--email", "a@x.com", "--cash", "150000",
               "--set", "strategy.stop_loss=0.10")
    assert r.exit_code == 0 and "止损" in invoke("admin", "sub", "show", "--email", "a@x.com").stdout

def test_trade_commands_record_and_show_in_book(runner, db) -> None:
    # buy 之后 book show 的持仓里有该股票和"自主买入"或"按信号买入"

def test_business_errors_print_chinese_message_and_exit_1_without_traceback(runner, db) -> None:
    r = invoke("admin", "trade", "sell", "--email", "a@x.com", "600001", "--price", "10", "--shares", "999999")
    assert r.exit_code == 1 and "超过持仓" in r.stdout and "Traceback" not in r.stdout
    r = invoke("admin", "sub", "set", "--email", "a@x.com", "--set", "strategy.lookback_days=100")
    assert r.exit_code == 1 and "全站统一" in r.stdout
    assert invoke("admin", "sub", "show", "--email", "nobody@x.com").exit_code == 1

def test_void_command_and_derived_dividend_rows_in_book_show(runner, db) -> None:

def test_runs_command_lists_failure_reason(runner, db) -> None:
```

- [ ] **Step 2–4：确认失败、实现、确认通过，`./scripts/check.sh` 全绿。**
- [ ] **Step 5：提交** — `feat: 管理命令`

---

### Task 12：部署、备份、文档与真实环境试运行

**Files:**
- Create: `scripts/backup_db.sh`、`scripts/mac/com.qt.keepawake.plist`、`docs/migrate-to-server.md`、`docs/live-vs-backtest.md`、`tests/jobs/test_end_to_end.py`
- Modify: `compose.yaml`、`docs/deploy-mac.md`、`README.md`

**Interfaces:**
- Consumes: 全部前序任务。

- [ ] **Step 1：写端到端测试**（`tests/jobs/test_end_to_end.py`）

```python
def test_two_users_thirty_days_end_to_end(ctx, db, tmp_path) -> None:
    # 使用者甲默认参数；使用者乙 stop_loss=0.10、max_positions=2
    # 30 个交易日逐日 catch_up；甲在收到买入信号的次日用 record_trade 照做，乙从不录入
    # 断言：
    #  - 甲、乙的系统账分别等于各自参数下的 run_period 结果（equity 逐日相同）
    #  - 甲的手动账有持仓且信号为 executed；乙的买入信号全部 abandoned 且各有一条 abandoned 跟踪记录
    #  - 发给甲的邮件里没有乙的任何股票数量或金额，反之亦然（按 to_addr 分组检查）
    #  - 发件箱里没有重复的 dedupe_key，没有 pending 之外的异常状态
```

- [ ] **Step 2：运行确认通过**（前序任务正确时应直接通过；不通过说明前序任务之间接口不一致，修到通过）。
- [ ] **Step 3：完善 `compose.yaml`**：`worker` 设 `restart: unless-stopped`、依赖 `db` 健康；新增一次性服务 `migrate`（`alembic upgrade head`），`worker` 启动前先执行。
- [ ] **Step 4：写 `scripts/backup_db.sh`**：对 `db` 容器执行 `pg_dump`，输出到 `backups/qt-YYYYMMDD.sql.gz`，删除 14 天前的备份；`backups/` 加入 `.gitignore`。`worker` 在每日运行状态为 `done` 或 `done_with_errors` 之后调用一次等价的备份逻辑（在容器内用 `pg_dump` 连接 `db`，输出到挂载的 `backups` 卷）。
- [ ] **Step 5：写文档**
  - `docs/deploy-mac.md`：安装容器运行环境；复制 `.env.example` 为 `.env` 并填写（邮箱的 SMTP 授权码怎么取、代理地址、管理员邮箱）；`docker compose up -d`；创建第一个使用者和订阅的命令；如何查看运行记录和日志；**防休眠**：说明容器无法阻止 Mac 休眠，给出两种由用户自己操作的办法（接电并在系统设置里关闭自动休眠；或安装 `scripts/mac/com.qt.keepawake.plist`，它在每个工作日 17:55 运行 `caffeinate -i -t 14400`），并说明错过运行会在恢复后自动补跑；每日更新耗时实测（任务 2 已写）。
  - `docs/migrate-to-server.md`：在新服务器上安装 Docker、拷贝代码与 `.env`、用 `backup_db.sh` 的备份恢复数据库、拷贝或重新下载 `data/`、去掉代理设置、启动并核对 `qt admin runs`。
  - `docs/live-vs-backtest.md`（给使用者看）：每日运行与回测的已知差异——参考价是当日收盘价而成交在次日；非持仓非候选股票的财务与分红最多迟 7 天；手动账的分红按税前计；免费数据的局限（链接到 `docs/data-gaps.md`）；系统账与手动账的区别。
  - `README.md`：增加"第二阶段：每日运行"一节，列出常用命令。
- [ ] **Step 6：真实环境试运行（由主控执行，不派给子代理）**
  1. `cp .env.example .env`，`MAIL_BACKEND=file`，其余按本机填写。
  2. `docker compose up -d`，确认 `migrate` 成功、`worker` 在运行。
  3. `qt admin user add`、`qt admin sub create`（起始资金 150000，默认参数）。
  4. `qt run-day`，对真实数据跑最近一个交易日。记录总耗时、各阶段耗时、产生的信号数、写出的 `.eml` 文件。
  5. 再执行一次 `qt run-day`，确认没有任何新增记录和邮件。
  6. 把耗时和结果写进 `docs/deploy-mac.md` 的"首次试运行记录"。
- [ ] **Step 7：交付检查点**
  - 向用户报告：试运行结果、每晚实际耗时、还需要用户亲自做的事（填写真实 SMTP 授权码并把 `MAIL_BACKEND` 改为 `smtp`、防休眠设置）。
  - **SMTP 授权码由用户自己填进 `.env`，执行者不得索要、不得代填。**
  - 用户确认收到第一封真实邮件之后，模拟期开始计时。
- [ ] **Step 8：提交** — `feat: 部署、备份与文档，完成子阶段 2A`

---

## 自查记录

**设计覆盖：**

| 设计章节 | 任务 |
|---|---|
| 7.1 数据存放、隔离 | 2、3 |
| 7.2 代码划分 | 文件结构 |
| 7.3 每日流程、可重跑、补跑、单人出错、耗时、基本面新鲜度 | 1、2、10 |
| 7.4 策略订阅 | 3、6（改参数后延续） |
| 7.5 系统账与硬性验收 | 4、6、10、12 |
| 7.6 手动账 | 5、7 |
| 7.7 信号生命周期 | 6、7 |
| 7.8 日志与跟踪 | 7（trades）、8 |
| 7.9 邮件 | 9、10 |
| 7.10 网页、7.11 账号安全 | 不在 2A（见 2B）；隔离测试在 3、7、12 |
| 7.12 商业化预留 | 3（`plan`、条款字段、`audit_log`）；注册开关属于 2B |
| 7.13 部署运维 | 2、12 |
| 9.1 遗留事项 | 1（分红报错）、开工前（分支） |

**本计划做出、设计没有写明的决定**（交付时向用户说明）：

1. 分红送转在手动账里不存成流水，而是每次由分红表推算出来展示。好处是晚到的分红记录和回溯录入都自动正确；代价是使用者不能单独作废某一笔分红，只能用入金出金修正。
2. 手动账的卖出信号在条件不再满足时置为"已作废"并注明原因，再次满足时新发一个。
3. 手动账允许部分卖出；系统账始终整笔卖出。
4. 手动账里的退市股票不自动清算，只发一次卖出信号。
5. 财务与分红的 7 天一轮改为按股票代码错开的滚动刷新，避免每 7 天有一晚要跑 6 个多小时。
6. 定时进程不用第三方调度库，自己每 60 秒检查一次时间。
7. 邮件里的录入方式在 2A 是命令行，2B 换成网页链接。
8. 备份保留 14 天。
