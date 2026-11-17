# 持仓表显示盘中价 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交易时段内，`/manual` 和 `/system` 的持仓表每 15 秒自动更新，现价、市值、盈亏、收益按盘中价计算。

**Architecture:** 新增只读报价模块（腾讯接口 + 进程内共用缓存 + 交易时段判断）；视图层用一个纯函数把报价覆盖到已有的 `HoldingRow` 上；
网页层新增两个只返回持仓片段的路由，页面用一个随站点提供的小脚本定时替换该片段。信号、止损止盈、净值的计算一律不动。

**Tech Stack:** 标准库 `urllib.request`、FastAPI、Jinja2、原生 JavaScript（不用 HTMX 的条件触发：它依赖 `eval`，被站点安全策略禁止）。

**Spec:** `docs/superpowers/specs/2026-11-17-holdings-live-quotes-design.md`

## Global Constraints

- 函数参数都带类型标注；ruff、pyright 零告警；不得使用 `# type: ignore`、`noqa`、`pyright: ignore`、`fmt: skip`。
- 用语：只说"信号/提示/按你设定的规则计算"，不出现"建议买入""推荐"。
- 安全策略保持 `default-src 'self'`：页面不连接外部地址，不加内联脚本或内联样式。
- 测试不访问真实接口：用假的取报价函数和注入的时钟。
- 报价请求超时 3 秒，不重试；缓存有效期 10 秒；页面刷新间隔 15 秒。
- 交易时段：交易日的 09:30–11:30、13:00–15:00（上海时间，含两端）。
- 提交信息末尾带 `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`；不推送。

## Review Focus

1. 报价接口超时或返回乱码 → 持仓页仍返回 200，显示收盘价。
2. 登录过期后片段请求被重定向到登录页 → 脚本不得把登录页塞进表格。
3. 甲请求 `/system/holdings?sub=<乙的订阅>` → 404。
4. 手动账流水不一致（`page.error`）时请求片段 → 不报 500，返回空片段且不再刷新。
5. 两个请求同时发现缓存过期 → 只有一个去取，另一个用旧值，不阻塞。

---

### Task 1: 报价模块

**Files:**
- Create: `src/qt/data/quotes.py`
- Test: `tests/data/test_quotes.py`

**Interfaces (Produces):**

```python
@dataclass(frozen=True)
class LiveQuote:
    price: float        # 现价；0 表示停牌或尚未成交
    prev_close: float
    at: datetime        # 报价时间（上海时间，无时区）

QuoteFetcher = Callable[[Collection[str]], dict[str, LiveQuote]]

def exchange_prefix(symbol: str) -> str | None      # 6/9 开头 sh；0/2/3 开头 sz；4/8 开头 bj；其余 None
def parse_tencent(text: str) -> dict[str, LiveQuote]  # 每行 v_sh600519="1~名称~600519~现价~昨收~…~[30]=yyyymmddHHMMSS~…"；坏行丢弃
def fetch_tencent(symbols: Collection[str]) -> dict[str, LiveQuote]  # https://qt.gtimg.cn/q=…，超时 3 秒，GBK 解码，每次最多 60 只，分批
def in_session(now: datetime, calendar: TradingCalendar) -> bool

class QuoteCache:
    def __init__(self, fetch: QuoteFetcher, *, ttl_seconds: float = 10.0, max_size: int = 2000) -> None
    def get(self, symbols: Collection[str], now: datetime) -> dict[str, LiveQuote]
```

`QuoteCache.get`：只取已过期或没有的股票；取失败（任何异常）时保留旧值并返回旧值，记 warning（同一分钟最多一条）；
用非阻塞锁保证同一时刻最多一个在途请求，拿不到锁的调用直接返回现有缓存；超过 `max_size` 时丢掉最旧的。

- [ ] 测试：`exchange_prefix`、`parse_tencent`（正常两行、字段不足、现价非数字、时间格式错）、`in_session`（09:29 否、09:30 是、11:30 是、11:31 否、12:59 否、13:00 是、15:00 是、15:01 否、周末否）、
  缓存（10 秒内第二次不再取、过期后只取过期的、失败保留旧值、失败且无旧值返回空、拿不到锁时不调用 fetch、条数上限）。
- [ ] 实现，跑测试，提交。

### Task 2: 视图覆盖

**Files:**
- Modify: `src/qt/service/views/books.py`（`HoldingRow` 加 `cash_received: float`、`price_source: str = "close"`；新增 `LiveHoldings`、`apply_quotes`）
- Modify: `src/qt/service/views/__init__.py`（导出）
- Test: `tests/service/` 下现有 views 测试文件旁新增 `test_views_live.py`

```python
@dataclass(frozen=True)
class LiveHoldings:
    rows: list[HoldingRow]
    quote_time: datetime | None   # 用到的报价里最新的时间；没有用到报价为 None
    live_count: int               # 用盘中价的只数
    suspended_count: int          # 标为停牌的只数
    close_count: int              # 仍用收盘价的只数

def apply_quotes(rows: Sequence[HoldingRow], quotes: Mapping[str, LiveQuote]) -> LiveHoldings
```

规则：有报价且 `price > 0` → `close=price`、`price_source="live"`；有报价且 `price <= 0` 且 `prev_close > 0` → `close=prev_close`、`price_source="suspended"`；
否则原样、`"close"`。改了 `close` 的行重算 `value = shares*close`、`pnl = value + cash_received - cost`、`ret = (value + cash_received)/cost - 1`。

- [ ] 测试上述三种情形及数值，提交。

### Task 3: 路由、模板、脚本

**Files:**
- Modify: `src/qt/web/deps.py`（`WebDeps` 加必填字段 `quotes: Callable[[Collection[str], datetime], dict[str, LiveQuote]]`）、`src/qt/cli.py`（用 `QuoteCache(fetch_tencent).get`）、`tests/web/conftest.py`（假报价，默认返回空）
- Modify: `src/qt/web/routes/common.py`（`live_holdings(deps, rows) -> tuple[LiveHoldings, bool]`：不在交易时段时不调用 `deps.quotes`，返回原样和 `False`）
- Modify: `src/qt/web/routes/manual.py`、`system.py`：页面带上 `live`、`polling`、`live_url`；新增 `GET /manual/holdings`、`GET /system/holdings`（权限 `user`，后者收 `sub`）
- Create: `src/qt/web/templates/_holdings_live.html`（片段：`<div id="holdings" data-live-holdings [data-live-url=… data-live-seconds="15"]>` + 表格 + 说明行）
- Modify: `src/qt/web/templates/_tables.html`（现价格子：停牌时加"停牌"字样）、`manual.html`、`system.html`、`base.html`（加载 `/static/live.js`）
- Create: `src/qt/web/static/live.js`
- Test: `tests/web/test_live.py`；更新 `tests/web/test_access.py` 的路由总表

`live.js`：找到带 `data-live-url` 的元素，每 `data-live-seconds` 秒、且 `document.hidden` 为假时 `fetch(url, {credentials: "same-origin"})`；
仅当 `response.ok && !response.redirected` 且返回文本含 `data-live-holdings` 时替换该元素；替换后的元素没有 `data-live-url` 就停止；页面重新可见时立即取一次。

说明行：盘中 → "盘中价，报价时间 HH:MM:SS；仅供查看，信号仍按每晚收盘价计算。"；有仍用收盘价的 → 追加 "N 只暂时取不到盘中价，显示的是收盘价。"；非交易时段不显示说明行。

手动账 `page.error` 不为空时：片段返回不带 `data-live-url` 的空容器。

- [ ] 测试：盘中页面含 `data-live-url` 与盘中价；盘后不含、且假报价未被调用；片段路由返回片段不含 `<html`；停牌字样；报价函数抛异常时 200；甲取乙的 `sub` → 404；未登录 → 跳登录；`page.error` 情形。
- [ ] 实现，跑 `./scripts/check.sh`，提交。

### Task 4: 文档与终审

- [ ] `docs/deploy-server.md` 加一句：网页进程需要能访问 `qt.gtimg.cn`（取不到时持仓表显示收盘价）。
- [ ] 一次独立的整分支审阅，修完后再跑全量检查。
