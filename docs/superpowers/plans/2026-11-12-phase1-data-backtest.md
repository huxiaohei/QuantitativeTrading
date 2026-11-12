# 阶段一：数据层 + 策略 + 风控 + 回测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把用户的"高股息低位"做法实现为可回测的策略，在 2015 年至今的 A股 数据上跑出一份可信的 HTML 回测报告，供用户决定参数和是否进入阶段二。

**Architecture:** 数据源藏在 `DataSource` 协议后面，落地为 Parquet 文件。`FeaturePanel` 把行情和财务数据加工成"某一天能看到的全市场指标快照"，时点正确性只在这一层保证。策略和风控只读快照和持仓，不直接碰数据，因此阶段二的每日实盘流程可以原样复用它们。回测引擎是一个按日循环，负责 A股 成交规则。

**Tech Stack:** Python 3.12、uv、pandas（配 pandas-stubs）、DuckDB、pyarrow、AKShare、pydantic v2、PyYAML、Typer、Jinja2、matplotlib、pytest、ruff、pyright。

**Spec:** `docs/superpowers/specs/2026-11-12-quant-system-design.md`

**本计划不包含：** 每日定时运行、邮件、成交录入、交易日志、SQLite 状态库、月度复盘（均属阶段二）。

## Global Constraints

- Python 3.12，用 uv 管理依赖。
- 所有函数的参数和返回值都带类型标注。
- 零告警：`uv run ruff check .`、`uv run ruff format --check .`、`uv run pyright` 均无任何输出问题；pytest 配置 `filterwarnings = ["error"]`。
- pyright 用 `typeCheckingMode = "strict"`。只有 `src/qt/data/akshare_source.py` 允许用文件级 pyright 注释放宽"未知类型"类规则，其他文件不允许出现 `# type: ignore`、`# pyright: ignore`、`# noqa`。
- 只用免费数据。数据文件（`data/`）、`.env`、报告输出（`reports/`）不进入 git。
- 所有数值参数放在 `config/default.yaml`，默认值取自设计文档，代码里不写死。
- 标识符用英文；注释、文档字符串、报告文字、命令行提示用中文。
- 回测与将来的实盘共用同一份策略代码和风控代码，这两个模块不得依赖回测引擎。
- 每个任务结束前运行 `./scripts/check.sh`，必须全部通过才能提交。
- 提交信息末尾加一行 `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`。

## Review Focus

设计文档隐含、但容易被漏掉的五种情况。每条都已在对应任务里加了测试。

1. **长期停牌的股票**：持仓按最后一个收盘价计值，挂着的订单顺延，停牌期间不产生该股票的买卖信号。（任务 6、9）
2. **除权除息日**：未复权价格的跳空不能触发止损、不能被数据完整性检查当成异常、不能扭曲一年低位的计算。（任务 5、6、9）
3. **数据下载中途失败或被限流**：重新运行能从断点继续，不留下写了一半的文件。（任务 4、5）
4. **数据不足或缺失**：上市不满 250 个交易日、财务指标为空的股票被排除在候选之外，程序不报错，也不会因为空值比较而误选。（任务 6、7）
5. **持仓股票被 ST 或退市**：被 ST 后仍可卖出，涨跌停幅度按 5% 计；退市的持仓按最后一个收盘价清算并记为一笔交易。（任务 9）

## 文件结构

```
pyproject.toml
scripts/check.sh               质量门禁：ruff、pyright、pytest
scripts/probe_akshare.py       数据探查脚本（任务 2）
config/default.yaml            全部参数
docs/data-gaps.md              免费数据的缺口清单（任务 2 产出，报告引用）
docs/concepts.md               概念说明，给用户科普（任务 11）
src/qt/
  config.py                    配置模型与加载
  models.py                    Position、Order、Trade 等领域对象
  calendar.py                  交易日历
  costs.py                     交易成本
  data/schema.py               各数据表的列定义与校验
  data/source.py               DataSource 协议
  data/akshare_source.py       AKShare 实现
  data/store.py                Parquet 读写
  data/update.py               增量更新与完整性检查
  features.py                  FeaturePanel：时点正确的每日指标快照
  strategy/base.py             Strategy 协议
  strategy/high_dividend_low.py  第一版策略
  risk.py                      风控层
  backtest/engine.py           按日回测循环
  backtest/metrics.py          绩效指标
  backtest/report.py           HTML 报告
  backtest/sweep.py            参数对比
  cli.py                       命令行入口 `qt`
tests/                         与 src/qt 同构，另有 tests/fakes.py
```

---

### Task 1: 项目脚手架、质量门禁、配置

**Files:**
- Create: `pyproject.toml`、`.gitignore`、`scripts/check.sh`、`config/default.yaml`、`src/qt/__init__.py`、`src/qt/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `load_config(path: Path) -> AppConfig`
  - `AppConfig` 含五个子模型，字段名和默认值如下（pydantic `BaseModel`，`frozen=True`，`extra="forbid"`）：

```yaml
universe:
  boards: [sh_main, sz_main]      # 可选值另有 chinext；科创板、北交所不支持
  min_listed_years: 3
  min_avg_amount_60d: 50000000
strategy:
  min_dividend_yield: 0.04
  max_price_position: 0.2
  lookback_days: 250
  min_dividend_years: 5
  min_roe_avg3: 0.08
  max_payout_ratio: 0.80
  min_profit_yoy: -0.20
  max_debt_ratio: 0.70
  take_profit_mode: fixed          # fixed | trailing
  take_profit_fixed: 0.12
  trailing_activate: 0.10
  trailing_drawdown: 0.08
  stop_loss: 0.15
  time_stop_days: 250
  fundamental_profit_yoy: -0.30
risk:
  max_position_pct: 0.10
  max_positions: 10
  max_industry_pct: 0.30
  warn_drawdown: 0.15
  pause_drawdown: 0.20
  pause_days: 60
  cooldown_days: 60
costs:
  commission_rate: 0.00025
  min_commission: 5.0
  stamp_tax_rate: 0.0005
  transfer_fee_rate: 0.00001
  slippage: 0.001
backtest:
  initial_cash: 150000
  data_start: 2009-01-01           # 财务和分红数据起点，满足"连续分红 5 年"
  in_sample_start: 2015-01-01
  in_sample_end: 2021-12-31
  out_sample_start: 2022-01-01
  buy_order_valid_days: 3
  benchmarks: [H00300, "510880"]
```

- [ ] **Step 1:** `git init`；用 `uv init --package` 建项目，包名 `qt`，命令行入口 `qt = "qt.cli:app"`。加入 Tech Stack 列出的依赖，以及开发依赖 `pandas-stubs`、`types-PyYAML`、`pytest`、`ruff`、`pyright`。
- [ ] **Step 2:** 在 `pyproject.toml` 配置：pyright `typeCheckingMode = "strict"`、`include = ["src", "tests", "scripts"]`；ruff `select = ["E","F","W","I","B","UP","ANN","SIM","RUF"]`、`line-length = 100`；pytest `filterwarnings = ["error"]`。
- [ ] **Step 3:** 写 `scripts/check.sh`：依次执行 `uv run ruff check .`、`uv run ruff format --check .`、`uv run pyright`、`uv run pytest -q`，任一失败即退出非零。`.gitignore` 排除 `data/`、`reports/`、`.env`、`.venv/`。
- [ ] **Step 4: 写失败的测试**

```python
def test_default_config_matches_spec() -> None:
    cfg = load_config(Path("config/default.yaml"))
    assert cfg.strategy.min_dividend_yield == 0.04
    assert cfg.strategy.take_profit_fixed == 0.12
    assert cfg.risk.pause_drawdown == 0.20
    assert cfg.risk.cooldown_days == 60
    assert cfg.costs.min_commission == 5.0
    assert cfg.universe.boards == ["sh_main", "sz_main"]

def test_unknown_key_rejected(tmp_path: Path) -> None:
    # 在默认配置基础上给 strategy 加一个不存在的键
    with pytest.raises(ValidationError): ...

def test_unsupported_board_rejected(tmp_path: Path) -> None:
    # boards: [star]  科创板不支持
    with pytest.raises(ValidationError): ...
```

- [ ] **Step 5:** 运行 `uv run pytest tests/test_config.py -q`，确认因 `qt.config` 不存在而失败。
- [ ] **Step 6:** 实现 `config/default.yaml` 和 `src/qt/config.py`。
- [ ] **Step 7:** 运行 `./scripts/check.sh`，预期全部通过且无任何告警输出。
- [ ] **Step 8:** 提交 `chore: 项目脚手架、质量门禁与配置`。

---

### Task 2: 数据探查，产出缺口清单

这是调研任务，不走测试驱动。它的结论决定任务 5 用哪些接口，以及报告里要披露什么。

**Files:**
- Create: `scripts/probe_akshare.py`、`docs/data-gaps.md`

**Interfaces:**
- Produces: `docs/data-gaps.md`，任务 5 和任务 10 都要读它。

- [ ] **Step 1:** 写 `scripts/probe_akshare.py`，对下表每一项实际调用安装版本的 AKShare，打印返回的列名、行数、最早和最晚日期。表中接口名是候选，AKShare 的接口经常变动，以实际安装版本的文档和返回结果为准。

| 需要的数据 | 候选接口 | 要回答的问题 |
|---|---|---|
| 交易日历 | `tool_trade_date_hist_sina` | 是否覆盖 2009 年至今 |
| 在市股票列表与上市日期 | `stock_info_a_code_name`、`stock_info_sh_name_code`、`stock_info_sz_name_code` | 能否区分板块 |
| 已退市股票列表与退市日期 | `stock_info_sh_delist`、`stock_info_sz_delist` | 已退市股票的历史日线能否取到 |
| 日线（未复权 + 后复权） | `stock_zh_a_hist` | 用 600000、000001、一只已退市股票各试一次；连续请求的限流情况 |
| 分红记录 | `stock_history_dividend_detail`、`stock_fhps_detail_em` | 是否有预案公告日、除权除息日、每股派息 |
| 财务指标 | `stock_financial_analysis_indicator`、`stock_financial_abstract_ths` | 是否有 ROE、净利润同比、资产负债率；**是否有实际公告日** |
| 行业分类 | `stock_board_industry_name_em` 及其成分接口 | 只有当前分类还是有历史分类 |
| 历史 ST 状态 | 无明确接口，可尝试从历史名称变更推断 | 能否还原某一天是否为 ST |
| 沪深300 全收益 | `stock_zh_index_hist_csindex(symbol="H00300")` | 取不到时退而用 510300 后复权 |
| 红利 ETF | `fund_etf_hist_em(symbol="510880", adjust="hfq")` | 起始日期 |

- [ ] **Step 2:** 运行 `uv run python scripts/probe_akshare.py`，把结论写进 `docs/data-gaps.md`。每个数据项一节，写明：选用的接口、覆盖范围、缺口、采用的保守近似、对回测结果的偏差方向（偏乐观或偏悲观）。固定的近似规则：
  - 财报没有实际公告日时，按法定披露截止日使用：年报次年 4 月 30 日，一季报 4 月 30 日，半年报 8 月 31 日，三季报 10 月 31 日。
  - 没有历史行业分类时，用当前分类回填全部历史。
  - 历史 ST 状态无法还原时，ST 过滤只对能确认的时段生效。
  - 已退市股票的日线取不到时，记录缺失的股票数量。
- [ ] **Step 3:** 运行 `./scripts/check.sh`。脚本文件同样受 ruff 和 pyright 约束，可使用与 `akshare_source.py` 相同的文件级放宽。
- [ ] **Step 4:** 提交 `docs: AKShare 数据探查与缺口清单`。
- [ ] **Step 5: 检查点。** 如果出现以下任一情况，停下来向用户报告后再继续：日线无法取得未复权价格或复权因子；分红数据没有任何日期字段；批量下载约 3000 只股票的日线预计超过 12 小时。

---

### Task 3: 领域对象、交易日历、交易成本

**Files:**
- Create: `src/qt/models.py`、`src/qt/calendar.py`、`src/qt/costs.py`
- Test: `tests/test_models.py`、`tests/test_calendar.py`、`tests/test_costs.py`

**Interfaces:**
- Consumes: `CostsConfig`（任务 1）
- Produces:

```python
# models.py
class Side(StrEnum): BUY = "buy"; SELL = "sell"
class ExitReason(StrEnum):
    TAKE_PROFIT_FIXED = "take_profit_fixed"; TAKE_PROFIT_TRAILING = "take_profit_trailing"
    STOP_LOSS = "stop_loss"; TIME_STOP = "time_stop"; FUNDAMENTAL = "fundamental"
    DELISTED = "delisted"

@dataclass(frozen=True)
class BuyCandidate:
    symbol: str; day: date; close: float; industry: str
    metrics: Mapping[str, float]; reason: str        # reason 是给人看的中文说明

@dataclass(frozen=True)
class SellSignal:
    symbol: str; day: date; reason: ExitReason; detail: str

@dataclass
class Position:
    symbol: str; shares: float; cost: float          # cost 为含费用的总买入金额
    entry_day: date; industry: str; entry_metrics: Mapping[str, float]
    days_held: int = 0; peak_return: float = 0.0
    def ret(self, close: float) -> float: ...        # shares * close / cost - 1
    def scale_shares(self, factor_ratio: float) -> None: ...

@dataclass(frozen=True)
class Order:
    symbol: str; side: Side; shares: float; signal_day: date; reason: str
    exit_reason: ExitReason | None = None

@dataclass(frozen=True)
class Trade:
    symbol: str; entry_day: date; exit_day: date; cost: float; proceeds: float
    exit_reason: ExitReason; holding_days: int; entry_metrics: Mapping[str, float]
    @property
    def pnl(self) -> float: ...
    @property
    def ret(self) -> float: ...

# calendar.py
class TradingCalendar:
    def __init__(self, days: Sequence[date]) -> None: ...
    def is_trading_day(self, day: date) -> bool: ...
    def offset(self, day: date, n: int) -> date: ...        # n 可为负；越界抛 ValueError
    def between(self, start: date, end: date) -> list[date]: ...   # 闭区间
    def count_between(self, start: date, end: date) -> int: ...    # (start, end] 内的交易日数

# costs.py
class CostModel:
    def __init__(self, cfg: CostsConfig) -> None: ...
    def buy_cost(self, price: float, shares: float) -> float: ...       # 现金流出总额
    def sell_proceeds(self, price: float, shares: float) -> float: ...  # 现金流入净额
    def max_lots(self, price: float, cash: float) -> int: ...           # 现金够买的最大整手数
```

分红的处理方式（全计划统一）：持仓股数在复权因子变化的当天乘以 `当日因子 / 前一交易日因子`，等同于把分红按除权价再投资。这样 `Position.ret` 天然包含分红，除权跳空也不会触发止损。这是近似处理（忽略红利税，允许出现非整数股数），任务 10 的报告里要写明。

- [ ] **Step 1: 写失败的测试**

```python
def test_buy_cost_with_min_commission() -> None:
    m = CostModel(default_costs())
    # 成交价 10 * 1.001 = 10.01，金额 10010；佣金 max(5, 2.5025) = 5；过户费 0.1001
    assert m.buy_cost(10.0, 1000) == pytest.approx(10015.1001)

def test_sell_proceeds_includes_stamp_tax() -> None:
    m = CostModel(default_costs())
    # 成交价 9.99，金额 9990；佣金 5；印花税 4.995；过户费 0.0999
    assert m.sell_proceeds(10.0, 1000) == pytest.approx(9979.9051)

def test_max_lots_rounds_down_to_board_lot() -> None:
    m = CostModel(default_costs())
    assert m.max_lots(10.0, 15000.0) == 14       # 15 手含费用超出 15000
    assert m.max_lots(200.0, 15000.0) == 0       # 一手 20000 买不起

def test_position_return_includes_reinvested_dividend() -> None:
    p = Position("600000", 1000, 10000.0, date(2020, 1, 2), "银行", {})
    p.scale_shares(1.05)                          # 除权日因子上升 5%
    assert p.ret(9.6) == pytest.approx(0.008)     # 1050 * 9.6 / 10000 - 1

def test_calendar_offset_and_count() -> None:
    cal = TradingCalendar([date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 8)])
    assert cal.offset(date(2024, 1, 4), 1) == date(2024, 1, 8)
    assert cal.count_between(date(2024, 1, 2), date(2024, 1, 8)) == 3
    assert not cal.is_trading_day(date(2024, 1, 6))
    with pytest.raises(ValueError):
        cal.offset(date(2024, 1, 8), 1)
```

- [ ] **Step 2:** 运行这三个测试文件，确认失败。
- [ ] **Step 3:** 实现三个模块。
- [ ] **Step 4:** 运行 `./scripts/check.sh`，预期通过。
- [ ] **Step 5:** 提交 `feat: 领域对象、交易日历与成本模型`。

---

### Task 4: 数据表定义、数据源协议、Parquet 存储

**Files:**
- Create: `src/qt/data/__init__.py`、`src/qt/data/schema.py`、`src/qt/data/source.py`、`src/qt/data/store.py`、`tests/fakes.py`
- Test: `tests/data/test_store.py`、`tests/data/test_schema.py`

**Interfaces:**
- Produces:

```python
# schema.py —— 表名与列（日期列统一为 datetime64[ns]，symbol 为 6 位字符串）
class Table(StrEnum):
    TRADE_DAYS = "trade_days"    # day
    SECURITIES = "securities"    # symbol, name, board, list_date, delist_date(可空)
    BARS = "bars"                # symbol, day, open, high, low, close, volume, amount, adj_factor
    DIVIDENDS = "dividends"      # symbol, fiscal_year, announce_date, ex_date, cash_per_share
    FINANCIALS = "financials"    # symbol, report_period, announce_date, roe, net_profit,
                                 #   net_profit_yoy, debt_ratio, eps
    INDUSTRIES = "industries"    # symbol, industry, is_financial
    ST_HISTORY = "st_history"    # symbol, start, end(可空)
    BENCHMARKS = "benchmarks"    # code, day, close
def validate(table: Table, df: pd.DataFrame) -> None: ...   # 缺列、多列、类型不符抛 SchemaError

# source.py
class DataSource(Protocol):
    def trade_days(self) -> pd.DataFrame: ...
    def securities(self) -> pd.DataFrame: ...
    def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...
    def dividends(self, symbol: str) -> pd.DataFrame: ...
    def financials(self, symbol: str) -> pd.DataFrame: ...
    def industries(self) -> pd.DataFrame: ...
    def st_history(self) -> pd.DataFrame: ...
    def benchmark(self, code: str, start: date, end: date) -> pd.DataFrame: ...

# store.py
class ParquetStore:
    def __init__(self, root: Path) -> None: ...
    def write(self, table: Table, df: pd.DataFrame, symbol: str | None = None) -> None: ...
    def append(self, table: Table, df: pd.DataFrame, symbol: str) -> None: ...   # 按主键去重
    def read(self, table: Table, symbols: Sequence[str] | None = None,
             start: date | None = None, end: date | None = None) -> pd.DataFrame: ...
    def last_day(self, table: Table, symbol: str) -> date | None: ...
    def has(self, table: Table, symbol: str | None = None) -> bool: ...

# tests/fakes.py
class FakeSource:                       # 实现 DataSource，数据由测试传入
    def __init__(self, **tables: pd.DataFrame) -> None: ...
def make_bars(symbol: str, days: Sequence[date], closes: Sequence[float],
              adj_factors: Sequence[float] | None = None) -> pd.DataFrame: ...
```

存储布局：按股票分文件的表（BARS、DIVIDENDS、FINANCIALS）放在 `{root}/{table}/{symbol}.parquet`，其余表放在 `{root}/{table}.parquet`。`read` 用 DuckDB 对 glob 路径查询。`write` 和 `append` 先写到同目录的临时文件再用 `os.replace` 换名。

- [ ] **Step 1: 写失败的测试**

```python
def test_roundtrip_and_filter(tmp_path: Path) -> None:
    # 写入两只股票各 3 天的 BARS，read(symbols=["600000"], start=第2天) 返回 2 行

def test_append_dedupes_on_primary_key(tmp_path: Path) -> None:
    # 先写第 1~3 天，再 append 第 3~4 天，结果 4 行，第 3 天取后写入的值

def test_last_day(tmp_path: Path) -> None:
    # 没有文件时返回 None；有文件时返回最大 day

def test_interrupted_write_leaves_old_file_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 让 os.replace 抛 OSError；原文件内容不变，目录下没有残留的临时文件

def test_validate_rejects_missing_column() -> None:
    with pytest.raises(SchemaError): validate(Table.BARS, bars_without_adj_factor)
```

- [ ] **Step 2:** 运行确认失败。
- [ ] **Step 3:** 实现 `schema.py`、`source.py`、`store.py`、`tests/fakes.py`。`write` 和 `append` 在落盘前调用 `validate`。
- [ ] **Step 4:** 运行 `./scripts/check.sh`，预期通过。
- [ ] **Step 5:** 提交 `feat: 数据表定义、数据源协议与 Parquet 存储`。

---

### Task 5: AKShare 数据源、增量更新、完整性检查

**Files:**
- Create: `src/qt/data/akshare_source.py`、`src/qt/data/update.py`、`src/qt/cli.py`
- Test: `tests/data/test_update.py`、`tests/data/test_akshare_source.py`

**Interfaces:**
- Consumes: `DataSource`、`ParquetStore`、`Table`、`validate`（任务 4）；`docs/data-gaps.md`（任务 2）
- Produces:

```python
# akshare_source.py
class AkshareSource:                    # 实现 DataSource；接口选择以 docs/data-gaps.md 为准
    def __init__(self, retries: int = 3, pause_seconds: float = 0.5) -> None: ...

# update.py
class DataIntegrityError(Exception): ...
@dataclass(frozen=True)
class UpdateReport:
    updated: int; skipped: int; failed: Mapping[str, str]      # symbol -> 错误信息
def update_all(source: DataSource, store: ParquetStore, cfg: AppConfig, today: date) -> UpdateReport: ...
def check_integrity(store: ParquetStore, calendar: TradingCalendar, day: date) -> list[str]: ...
def load_calendar(store: ParquetStore) -> TradingCalendar: ...

# cli.py
app = typer.Typer()
# qt data update [--config PATH]     调用 update_all，随后 check_integrity；有问题则打印并以非零退出
```

`update_all` 的行为：股票池取配置板块内的在市和已退市股票；每只股票的日线从 `store.last_day` 的下一天续传，首次从 `backtest.in_sample_start` 往前 2 年开始（供 250 日回看使用）；财务和分红数据从 `backtest.data_start` 开始；单只股票失败记入 `failed` 后继续下一只；`adj_factor = 后复权收盘价 / 未复权收盘价`。

`check_integrity` 返回问题描述列表，空列表表示正常。检查项：`day` 是交易日但当日有日线的股票少于在市股票的 80%；某只股票复权后收盘价的单日涨跌幅绝对值超过 21%（上市后前 5 个交易日除外，新股上市初期涨跌幅限制不同）。

`akshare_source.py` 是唯一做列名映射和类型清洗的地方，每个方法返回前都调用 `validate`。把"AKShare 原始表 → 标准表"的转换写成不联网的纯函数，方便测试。

- [ ] **Step 1: 写失败的测试**

```python
def test_update_resumes_from_last_day(tmp_path: Path) -> None:
    # store 里 600000 已有到 2024-01-03 的日线；FakeSource 记录每次 bars() 的 start 参数
    # update_all 后断言该次调用的 start == date(2024, 1, 4)

def test_one_symbol_failure_does_not_stop_others(tmp_path: Path) -> None:
    # FakeSource 对 000001 抛异常；report.failed 含 "000001"，600000 仍被写入

def test_integrity_flags_missing_day(tmp_path: Path) -> None:
    # 10 只在市股票中只有 5 只有当日日线；check_integrity 返回 1 条问题

def test_integrity_ignores_ex_dividend_gap(tmp_path: Path) -> None:
    # 未复权收盘价 10 -> 7，同日 adj_factor 1.0 -> 1.43；返回空列表

def test_integrity_flags_unexplained_jump(tmp_path: Path) -> None:
    # 未复权收盘价 10 -> 7，adj_factor 不变；返回 1 条问题

def test_normalize_bars_computes_adj_factor() -> None:
    # 给定一小段未复权表和后复权表（列名取自任务 2 的探查结果），断言 adj_factor 与标准列齐全
```

- [ ] **Step 2:** 运行确认失败。
- [ ] **Step 3:** 实现三个文件。`akshare_source.py` 顶部可加文件级 pyright 放宽注释，仅限"未知类型"相关规则。
- [ ] **Step 4:** 运行 `./scripts/check.sh`，预期通过。
- [ ] **Step 5:** 实际运行 `uv run qt data update`。耗时可能达数小时，放后台执行。完成后确认 `data/bars/` 下文件数与股票池规模相符，`failed` 为空或已逐只查明原因并补记到 `docs/data-gaps.md`。
- [ ] **Step 6:** 提交 `feat: AKShare 数据源、增量更新与完整性检查`。

---

### Task 6: FeaturePanel，时点正确的每日指标快照

**Files:**
- Create: `src/qt/features.py`
- Test: `tests/test_features.py`

**Interfaces:**
- Consumes: `ParquetStore`、`Table`（任务 4）；`TradingCalendar`（任务 3）；`AppConfig`（任务 1）
- Produces:

```python
@dataclass(frozen=True)
class Bar:
    open: float; close: float; prev_close: float; factor_ratio: float; is_st: bool
    # prev_close 已按 factor_ratio 调整到当日价格口径；factor_ratio = 当日因子 / 前一交易日因子

class FeaturePanel:
    def snapshot(self, day: date) -> pd.DataFrame: ...
    def bar(self, day: date, symbol: str) -> Bar | None: ...        # 当日停牌或无数据返回 None
    def last_close(self, day: date, symbol: str) -> float: ...      # 截至 day 的最后一个收盘价
    def delisted_on(self, day: date) -> list[str]: ...

def build_panel(store: ParquetStore, calendar: TradingCalendar, cfg: AppConfig,
                start: date, end: date) -> FeaturePanel: ...
```

`snapshot(day)` 以 symbol 为索引，只包含当日有成交的股票，列如下：

| 列 | 口径 |
|---|---|
| `close` | 当日未复权收盘价 |
| `price_position` | 用后复权收盘价算 `(收盘 − 250日最低) ÷ (250日最高 − 250日最低)`；有效交易日不足 250 个或最高等于最低时为空 |
| `dividend_yield` | 公告日在 `day` 之前、且在过去 365 天内的每股派息合计 ÷ `close` |
| `dividend_years` | 截至 `day` 已公告的、连续有现金分红的财年数 |
| `dividend_cut` | 最近一个已公告财年的每股派息合计低于上一财年则为真 |
| `roe_avg3` | 最近 3 份已公告年报的 ROE 平均值，不足 3 份为空 |
| `payout_ratio` | 最近一个已公告财年的每股派息 ÷ 该年每股收益；每股收益不为正时为空 |
| `profit_yoy` | 最近一期已公告报告的净利润同比 |
| `debt_ratio` | 最近一期已公告报告的资产负债率 |
| `industry`、`is_financial` | 行业分类 |
| `is_st` | 当日是否 ST |
| `listed_years` | 上市至今的年数 |
| `avg_amount_60d` | 近 60 个交易日的日均成交额 |

"已公告"一律指 `announce_date < day`，严格小于。比率类字段用小数（4% 记为 0.04），数据源给的是百分数时在任务 5 的清洗函数里换算。

- [ ] **Step 1: 写失败的测试**（全部用 `tests/fakes.py` 构造的小数据）

```python
def test_financials_invisible_until_day_after_announcement() -> None:
    # 2020 年报 announce_date = 2021-04-28
    # snapshot(2021-04-28) 的 profit_yoy 仍是上一期的值；snapshot(2021-04-29) 才是新值

def test_dividend_yield_uses_only_announced_dividends() -> None:
    # 每股派 0.5 元，announce_date = 2021-03-30，收盘价 10
    # snapshot(2021-03-30).dividend_yield == 0；snapshot(2021-03-31).dividend_yield == 0.05

def test_price_position_unaffected_by_ex_dividend_gap() -> None:
    # 后复权价平稳，未复权价在除权日从 10 跳到 7 且因子同步上调；price_position 在除权日前后一致

def test_price_position_nan_when_history_short() -> None:
    # 只有 100 个交易日的数据；price_position 为空

def test_suspended_symbol_absent_from_snapshot_and_bar_is_none() -> None:
    # 某股票 2021-05-10 无日线；不在 snapshot 索引里，bar() 返回 None，last_close() 返回前一日收盘

def test_bar_prev_close_adjusted_on_ex_date() -> None:
    # 前收 10，当日因子比 1.25；bar.prev_close == 8.0，factor_ratio == 1.25

def test_dividend_cut_and_consecutive_years() -> None:
    # 2016~2020 每年派息 0.5, 0.5, 0.5, 0.5, 0.3 且均已公告；dividend_years == 5，dividend_cut 为真
```

- [ ] **Step 2:** 运行确认失败。
- [ ] **Step 3:** 实现 `features.py`。财务和分红数据与日线的对齐用 `pd.merge_asof`，`allow_exact_matches=False`。面板一次性算好全区间，`snapshot` 只做切片。
- [ ] **Step 4:** 运行 `./scripts/check.sh`，预期通过。
- [ ] **Step 5:** 提交 `feat: 时点正确的每日指标快照`。

---

### Task 7: 策略协议与"高股息低位"策略

**Files:**
- Create: `src/qt/strategy/__init__.py`、`src/qt/strategy/base.py`、`src/qt/strategy/high_dividend_low.py`
- Test: `tests/strategy/test_high_dividend_low.py`

**Interfaces:**
- Consumes: `snapshot` 的列（任务 6）；`BuyCandidate`、`SellSignal`、`Position`、`ExitReason`（任务 3）；`StrategyConfig`、`UniverseConfig`（任务 1）
- Produces:

```python
# base.py
class Strategy(Protocol):
    name: str
    def buy_candidates(self, day: date, snapshot: pd.DataFrame,
                       held: AbstractSet[str]) -> list[BuyCandidate]: ...
    def sell_signals(self, day: date, snapshot: pd.DataFrame,
                     positions: Sequence[Position]) -> list[SellSignal]: ...

# high_dividend_low.py
class HighDividendLow:                 # 实现 Strategy，name = "high_dividend_low"
    def __init__(self, strategy_cfg: StrategyConfig, universe_cfg: UniverseConfig) -> None: ...
```

规则（数值全部读配置）：
- 股票池：非 ST、`listed_years >= 3`、`avg_amount_60d >= 5000 万`。"一手金额超过单笔预算"与资金量有关，由风控层处理（任务 8）。
- 买入：`dividend_yield >= 0.04`、`price_position <= 0.2`、`dividend_years >= 5`、`roe_avg3 >= 0.08`、`payout_ratio <= 0.80`、`profit_yoy >= -0.20`、`debt_ratio <= 0.70`（`is_financial` 为真时跳过此项）。任何一项为空值即不通过。已持有的不再入选。按 `dividend_yield` 从高到低排序，相同则按代码升序。
- `BuyCandidate.metrics` 记录上述全部指标值；`reason` 用中文写明关键数字，例如"股息率 5.2%，处于一年价格区间底部 12%"。
- 卖出，每个持仓按下列顺序检查，只返回命中的第一个：
  1. 基本面：`dividend_cut` 为真，或 `profit_yoy < -0.30`
  2. 价格止损：`ret <= -0.15`
  3. 止盈：`fixed` 模式 `ret >= 0.12`；`trailing` 模式 `peak_return >= 0.10` 且 `(1 + ret) / (1 + peak_return) - 1 <= -0.08`
  4. 时间止损：`days_held >= 250` 且 `ret <= 0`
- 持仓股票当日不在快照里（停牌）时不产生卖出信号。
- `ret` 用 `position.ret(snapshot.close)` 计算；`peak_return` 和 `days_held` 由调用方维护，策略只读。

- [ ] **Step 1: 写失败的测试**

```python
def test_selects_stock_meeting_all_conditions() -> None: ...
@pytest.mark.parametrize("column,value", [
    ("dividend_yield", 0.039), ("price_position", 0.21), ("dividend_years", 4),
    ("roe_avg3", 0.079), ("payout_ratio", 0.81), ("profit_yoy", -0.21),
    ("debt_ratio", 0.71), ("is_st", True), ("listed_years", 2.9), ("avg_amount_60d", 4.9e7),
])
def test_rejects_when_one_condition_fails(column: str, value: object) -> None: ...
def test_nan_metric_is_rejected_not_error() -> None:          # roe_avg3 为空
def test_financial_stock_exempt_from_debt_ratio() -> None:    # is_financial 且 debt_ratio 0.92 仍入选
def test_held_symbols_excluded() -> None: ...
def test_sorted_by_dividend_yield_desc() -> None: ...
def test_candidate_reason_mentions_key_numbers() -> None:     # reason 含 "5.2%" 和 "12%"

def test_fixed_take_profit_at_12_percent() -> None:           # ret 0.12 -> TAKE_PROFIT_FIXED；0.119 -> 无信号
def test_trailing_take_profit() -> None:
    # trailing 模式：peak_return 0.20、当前 ret 0.10 -> 回落 8.3% -> TAKE_PROFIT_TRAILING
    # peak_return 0.09、当前 ret 0.0 -> 无信号（未达启动线）
def test_stop_loss_at_15_percent() -> None: ...
def test_time_stop_only_when_not_profitable() -> None:        # days_held 250：ret -0.01 触发，ret 0.01 不触发
def test_fundamental_stop_on_dividend_cut_or_profit_drop() -> None: ...
def test_fundamental_takes_priority_over_stop_loss() -> None: ...
def test_suspended_position_yields_no_signal() -> None: ...
```

- [ ] **Step 2:** 运行确认失败。
- [ ] **Step 3:** 实现两个文件。
- [ ] **Step 4:** 运行 `./scripts/check.sh`，预期通过。
- [ ] **Step 5:** 提交 `feat: 策略协议与高股息低位策略`。

---

### Task 8: 风控层

**Files:**
- Create: `src/qt/risk.py`
- Test: `tests/test_risk.py`

**Interfaces:**
- Consumes: `BuyCandidate`、`Position`、`Order`、`Side`、`ExitReason`（任务 3）；`TradingCalendar`、`CostModel`（任务 3）；`RiskConfig`（任务 1）
- Produces:

```python
class RiskEventKind(StrEnum): WARN = "warn"; PAUSE = "pause"; RESUME = "resume"
@dataclass(frozen=True)
class RiskEvent:
    day: date; kind: RiskEventKind; drawdown: float; message: str
@dataclass(frozen=True)
class Rejection:
    candidate: BuyCandidate; rule: str; message: str
    # rule 取值：paused, cooldown, max_positions, industry_cap, lot_too_expensive, no_cash

class RiskManager:
    def __init__(self, cfg: RiskConfig, calendar: TradingCalendar, costs: CostModel) -> None: ...
    def on_day_close(self, day: date, nav: float) -> list[RiskEvent]: ...
    def buying_allowed(self) -> bool: ...
    def note_exit(self, symbol: str, day: date, reason: ExitReason) -> None: ...
    def size_buys(self, day: date, candidates: Sequence[BuyCandidate], equity: float, cash: float,
                  positions: Sequence[Position], closes: Mapping[str, float],
                  pending_buys: Sequence[Order] = ()) -> tuple[list[Order], list[Rejection]]: ...
    def resume(self, day: date, nav: float) -> RiskEvent: ...          # 手动提前恢复
    def extend_pause(self, days: int) -> None: ...                     # 手动延长
```

行为：
- `nav` 是单位净值。阶段一没有出入金，回测引擎直接传 `总资产 / 初始资金`。
- 回撤 = `nav / 历史最高 nav - 1`。达到 −15% 发一次 `WARN`，净值创新高后才会再次发。达到 −20% 发 `PAUSE` 并停止买入。
- 暂停满 60 个交易日后的第一个 `on_day_close` 发 `RESUME`，并把历史最高 nav 重置为当日 nav。
- `note_exit`：原因是 `STOP_LOSS`、`TIME_STOP`、`FUNDAMENTAL` 时，该股票 60 个交易日内被拒，规则名 `cooldown`。两种止盈和 `DELISTED` 不触发冷却。
- `size_buys` 按候选顺序逐个处理，已通过的候选连同 `pending_buys` 一起计入后续候选的持仓数、行业占比和可用现金：
  - 单只预算 = `equity × 10%`；股数 = `costs.max_lots(close, min(预算, 剩余现金)) × 100`
  - 持仓数加待成交买单数已达 10 → `max_positions`
  - 加上这笔后该行业市值超过 `equity × 30%` → 先尝试减到不超限的最大整手数，减到 0 则 `industry_cap`。行业市值按 `股数 × close` 计，不含交易费用，恰好等于上限不算超限
  - 一手金额超过单只预算 → `lot_too_expensive`；预算够但现金不够一手 → `no_cash`
  - 暂停期间所有候选 → `paused`
- 每条 `Rejection.message` 用中文写明原因和数字。

- [ ] **Step 1: 写失败的测试**

```python
def test_sizes_to_ten_percent_in_board_lots() -> None:
    # equity 150000，cash 150000，close 10 -> 预算 15000 -> 1400 股

def test_lot_too_expensive() -> None:               # close 200，预算 15000 -> rule == "lot_too_expensive"
def test_no_cash() -> None:                         # cash 800，close 10 -> rule == "no_cash"
def test_max_positions_counts_pending_buys() -> None:   # 8 个持仓 + 2 个待成交买单 -> 新候选被拒
def test_industry_cap_trims_then_rejects() -> None:
    # equity 150000，银行已持有 40000；新银行候选 close 10 -> 只能再买 5000 元 -> 500 股
    # 银行已持有 45000 -> rule == "industry_cap"
def test_earlier_candidate_consumes_cash_for_later() -> None: ...

def test_warn_once_at_15_percent() -> None:
    # nav 1.0, 0.85, 0.84 -> 只有第二天发 WARN
def test_warn_rearms_after_new_high() -> None: ...
def test_pause_at_20_percent_blocks_buys() -> None:
    # nav 1.0 -> 0.80：PAUSE；buying_allowed() 为假；size_buys 全部 rule == "paused"
def test_auto_resume_after_60_trading_days_resets_peak() -> None:
    # 暂停后第 60 个交易日 nav 0.78：发 RESUME；次日 nav 0.77 不再触发 PAUSE
def test_manual_resume_and_extend() -> None: ...

def test_cooldown_after_stop_loss_but_not_take_profit() -> None:
    # note_exit(STOP_LOSS) 后第 59 个交易日被拒、第 60 个交易日放行
    # note_exit(TAKE_PROFIT_FIXED) 后次日即可买入
```

- [ ] **Step 2:** 运行确认失败。
- [ ] **Step 3:** 实现 `risk.py`。
- [ ] **Step 4:** 运行 `./scripts/check.sh`，预期通过。
- [ ] **Step 5:** 提交 `feat: 风控层`。

---

### Task 9: 回测引擎

**Files:**
- Create: `src/qt/backtest/__init__.py`、`src/qt/backtest/engine.py`
- Test: `tests/backtest/test_engine.py`

**Interfaces:**
- Consumes: `FeaturePanel`、`Bar`（任务 6）；`Strategy`（任务 7）；`RiskManager`、`RiskEvent`、`Rejection`（任务 8）；`CostModel`、`TradingCalendar`、`Position`、`Order`、`Trade`（任务 3）
- Produces:

```python
@dataclass(frozen=True)
class BacktestResult:
    equity: pd.Series                    # 索引为交易日，值为总资产
    cash: pd.Series
    trades: list[Trade]
    open_positions: list[Position]
    rejections: list[Rejection]
    risk_events: list[RiskEvent]
    expired_buys: list[Order]            # 超过有效期仍未成交的买单

def run_backtest(panel: FeaturePanel, strategy: Strategy, risk: RiskManager, costs: CostModel,
                 calendar: TradingCalendar, start: date, end: date, initial_cash: float,
                 buy_order_valid_days: int = 3) -> BacktestResult: ...
```

每个交易日按此顺序执行：

1. **退市清算**：`panel.delisted_on(day)` 中的持仓按 `panel.last_close` 卖出，记为 `ExitReason.DELISTED` 的交易。
2. **除权调整**：对每个当日有 `Bar` 的持仓调用 `scale_shares(bar.factor_ratio)`。
3. **撮合昨日及更早的挂单**，先卖后买，成交价为当日开盘价：
   - 当日无 `Bar`（停牌）→ 顺延。
   - 涨跌停幅度 `pct = 0.05 if bar.is_st else 0.10`；涨停价 = `prev_close × (1 + pct)` 四舍五入到分，跌停价同理。
   - 买单：`open >= 涨停价` → 顺延。卖单：`open <= 跌停价` → 顺延。
   - 买单成交时若现金不足，按 `costs.max_lots` 减到买得起的整手数，减到 0 则丢弃。
   - 买单自 `signal_day` 起超过 `buy_order_valid_days` 个交易日未成交 → 移入 `expired_buys`。卖单一直顺延到成交。
   - 卖出成交后生成 `Trade`，并调用 `risk.note_exit`。当日买入的股票当日不会被卖出（T+1），这由"信号在收盘后产生、次日才撮合"的流程天然保证。
4. **收盘计值**：每个持仓 `days_held += 1`，用 `panel.last_close` 计值（停牌用最后收盘价），更新 `peak_return`。记录当日 `equity` 和 `cash`。
5. `risk.on_day_close(day, equity / initial_cash)`。
6. `strategy.sell_signals` → 为每个信号生成全部股数的卖单；已有未成交卖单的持仓不重复下单。
7. 若 `risk.buying_allowed()`：`strategy.buy_candidates` → `risk.size_buys` → 买单。`held` 参数包含持仓和待成交买单的股票。`cash` 参数传当前现金减去待成交买单的预估金额。

- [ ] **Step 1: 写失败的测试**（用 `tests/fakes.py` 构造面板，用桩策略精确控制信号）

```python
def test_signal_on_t_fills_at_t_plus_1_open() -> None:
    # 桩策略在第 1 天给出买入候选；第 2 天开盘价 10.2 成交；第 1 天收盘时无持仓

def test_buy_blocked_at_limit_up_then_filled_next_day() -> None:
    # 第 2 天 open == round(prev_close * 1.1, 2)：不成交；第 3 天正常开盘：成交

def test_buy_order_expires_after_3_days() -> None:
    # 连续 3 个交易日开盘涨停；订单出现在 expired_buys，没有持仓

def test_sell_blocked_at_limit_down_carries_over() -> None:
    # 卖出信号后连续 2 天开盘跌停，第 3 天成交；Trade.exit_day 是第 3 天

def test_st_position_uses_5_percent_limit() -> None:
    # 持仓变为 ST；open == round(prev_close * 0.95, 2) 时卖单顺延

def test_suspended_position_valued_at_last_close_and_order_deferred() -> None:
    # 停牌 5 天：equity 在停牌期间不变；复牌首日撮合卖单

def test_ex_dividend_gap_does_not_trigger_stop_loss() -> None:
    # 使用 HighDividendLow 真实卖出规则；未复权价 10 -> 7.5 且 factor_ratio 1.3333
    # 持仓 ret 约为 0；没有任何卖出信号

def test_delisted_position_liquidated_at_last_close() -> None:
    # trades 里有一笔 exit_reason == DELISTED，proceeds 按最后收盘价计算

def test_costs_deducted_from_cash() -> None:
    # 买 1000 股 @10 后 cash == 初始现金 - costs.buy_cost(10.0, 1000)

def test_stop_loss_exit_triggers_cooldown() -> None:
    # 止损卖出后桩策略次日再次给出同一股票；rejections 里 rule == "cooldown"

def test_paused_risk_blocks_new_buys_but_allows_sells() -> None: ...

def test_same_inputs_give_identical_results() -> None:
    # 连续运行两次，equity 和 trades 完全相等
```

- [ ] **Step 2:** 运行确认失败。
- [ ] **Step 3:** 实现 `engine.py`。把上面 7 步各写成一个私有函数，主循环只负责按顺序调用。
- [ ] **Step 4:** 运行 `./scripts/check.sh`，预期通过。
- [ ] **Step 5:** 提交 `feat: 按日回测引擎`。

---

### Task 10: 绩效指标、基准对比、HTML 报告

**Files:**
- Create: `src/qt/backtest/metrics.py`、`src/qt/backtest/report.py`、`src/qt/backtest/templates/report.html.j2`
- Modify: `src/qt/cli.py`（增加 `qt backtest run`）
- Test: `tests/backtest/test_metrics.py`、`tests/backtest/test_report.py`

**Interfaces:**
- Consumes: `BacktestResult`（任务 9）；`ParquetStore`、`Table.BENCHMARKS`（任务 4）；`build_panel`（任务 6）；`docs/data-gaps.md`（任务 2）
- Produces:

```python
# metrics.py
@dataclass(frozen=True)
class Metrics:
    annual_return: float; max_drawdown: float; sharpe: float
    win_rate: float; profit_loss_ratio: float
    avg_holding_days: float; annual_turnover: float; trade_count: int
def compute_metrics(equity: pd.Series, trades: Sequence[Trade]) -> Metrics: ...
def benchmark_metrics(close: pd.Series) -> Metrics: ...          # 交易类字段填 0
def exit_breakdown(trades: Sequence[Trade]) -> pd.DataFrame: ... # 列：exit_reason, count, avg_ret, total_pnl
def rejection_breakdown(rejections: Sequence[Rejection]) -> pd.DataFrame: ...   # 列：rule, count

# report.py
def render_report(result: BacktestResult, metrics: Metrics, benchmarks: Mapping[str, pd.Series],
                  cfg: AppConfig, period_label: str, data_gaps: Path, out: Path) -> Path: ...
def run_period(cfg: AppConfig, store: ParquetStore, start: date, end: date) -> BacktestResult: ...
    # 组装 panel、HighDividendLow、RiskManager、CostModel 并调用 run_backtest；任务 11 复用

# cli.py
# qt backtest run [--period in|out|full] [--config PATH] [--out reports/]
```

口径：年化按每年 244 个交易日；夏普 = 日收益均值 ÷ 日收益标准差 × √244，无风险利率取 0；最大回撤为负数；盈亏比 = 盈利交易平均盈利 ÷ 亏损交易平均亏损的绝对值；年换手率 = 卖出成交总额 ÷ 平均总资产 ÷ 年数。没有交易或没有亏损交易时，对应字段为 0，不得出现除零告警。

报告内容（单个自包含 HTML 文件，图用 matplotlib 生成后以内嵌 SVG 放入）：
1. 概要表：策略与两个基准的年化收益、最大回撤、夏普并排对比，并用一句话回答"是否优于直接持有红利 ETF"。
2. 净值曲线：策略与两个基准，起点归一；下方配回撤曲线。
3. 交易统计：胜率、盈亏比、平均持有天数、年换手率、交易笔数。
4. 按退出原因的统计表。
5. 风控事件列表，以及被风控拒绝的信号按规则统计。
6. 逐笔交易清单，含买入时的指标快照。
7. 本次使用的全部参数。
8. **数据局限与近似处理**：原文嵌入 `docs/data-gaps.md`，并固定写明两条——分红按除权价再投资且未扣红利税；回测结果不代表未来收益。
9. **验收提示**：`max_drawdown <= -0.40` 时在报告顶部显示醒目提示"最大回撤接近 50% 上限，按设计文档策略需要重做"。40% 这个"接近"的界线是本计划定的。

`--period`：`in` 为 `in_sample_start`~`in_sample_end`，`out` 为 `out_sample_start`~最后一个有数据的交易日，`full` 为两者合并。输出文件名 `reports/backtest-{period}-{YYYYMMDD-HHMMSS}.html`。

- [ ] **Step 1: 写失败的测试**

```python
def test_max_drawdown() -> None:
    # equity [100, 110, 99, 121] -> max_drawdown == pytest.approx(-0.10)
def test_annual_return_over_244_days() -> None:
    # 245 个点，从 100 到 110 -> annual_return == pytest.approx(0.10)
def test_win_rate_and_profit_loss_ratio() -> None:
    # 三笔交易 pnl +200, +100, -100 -> win_rate 2/3，profit_loss_ratio 1.5
def test_no_trades_gives_zeros_without_warning() -> None: ...
def test_exit_breakdown_groups_by_reason() -> None: ...

def test_report_contains_required_sections(tmp_path: Path) -> None:
    # 生成报告后断言 HTML 含 "数据局限"、"红利 ETF"、"未扣红利税"、逐笔清单里的股票代码
def test_report_flags_drawdown_near_limit(tmp_path: Path) -> None:
    # max_drawdown -0.41 -> 含 "策略需要重做"；-0.20 -> 不含
def test_report_is_self_contained(tmp_path: Path) -> None:
    # HTML 中没有 http:// 或 https:// 的外部资源引用
```

- [ ] **Step 2:** 运行确认失败。
- [ ] **Step 3:** 实现 `metrics.py`、`report.py`、模板和 CLI 命令。matplotlib 用 `Agg` 后端并指定 macOS 自带的中文字体（如 `PingFang SC`、`Heiti TC`），字体缺失不得产生告警。
- [ ] **Step 4:** 运行 `./scripts/check.sh`，预期通过。
- [ ] **Step 5:** 提交 `feat: 绩效指标与 HTML 回测报告`。

---

### Task 11: 参数对比、样本内外检验、阶段一交付

**Files:**
- Create: `src/qt/backtest/sweep.py`、`docs/concepts.md`、`README.md`
- Modify: `src/qt/cli.py`（增加 `qt backtest sweep`）
- Test: `tests/backtest/test_sweep.py`

**Interfaces:**
- Consumes: `run_period`、`compute_metrics`（任务 10）；`AppConfig`（任务 1）
- Produces:

```python
def override(cfg: AppConfig, path: str, value: object) -> AppConfig: ...
    # path 形如 "strategy.min_dividend_yield"；未知路径或类型不符抛 ValueError
def run_sweep(cfg: AppConfig, store: ParquetStore,
              grid: Mapping[str, Sequence[object]]) -> pd.DataFrame: ...
    # 对 grid 的笛卡尔积逐一回测，只在样本内区间运行
    # 返回每组参数一行：参数列 + annual_return, max_drawdown, sharpe, win_rate, trade_count

# qt backtest sweep --param strategy.min_dividend_yield=0.03,0.04,0.05 [--param ...] [--out reports/]
#   结果打印为表格，并存为 reports/sweep-{时间戳}.csv
```

`run_sweep` 没有区间参数，固定使用 `in_sample_start`~`in_sample_end`。这是设计文档"2022 年之后的数据只用于检验，不参与调参"的落实方式。

- [ ] **Step 1: 写失败的测试**

```python
def test_override_returns_new_config_and_leaves_original() -> None:
    new = override(cfg, "strategy.min_dividend_yield", 0.05)
    assert new.strategy.min_dividend_yield == 0.05 and cfg.strategy.min_dividend_yield == 0.04

def test_override_unknown_path_raises() -> None:
    with pytest.raises(ValueError): override(cfg, "strategy.no_such", 1)

def test_sweep_runs_cartesian_product(tmp_path: Path) -> None:
    # grid 两个参数各 2 个取值 -> 结果 4 行，含两个参数列

def test_sweep_never_touches_out_of_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 记录 run_period 收到的 start/end；全部落在样本内区间之内
```

- [ ] **Step 2:** 运行确认失败。
- [ ] **Step 3:** 实现 `sweep.py` 和 CLI 命令。
- [ ] **Step 4:** 写 `docs/concepts.md`，用通俗中文解释报告里出现的每个概念：股息率、一年价格位置、ROE、分红率、复权、回撤、夏普比率、胜率与盈亏比、换手率、止盈止损、样本内与样本外、未来函数、幸存者偏差。每个概念两三句话，配一个带数字的例子。
- [ ] **Step 5:** 写 `README.md`：安装步骤、三条命令的用法、如何改参数（改 `config/default.yaml`）、如何加一个新策略（在 `src/qt/strategy/` 下新建一个实现 `Strategy` 协议的类，并指出 `run_period` 里装配策略的那一行）。
- [ ] **Step 6:** 运行 `./scripts/check.sh`，预期通过。提交 `feat: 参数对比与文档`。
- [ ] **Step 7: 跑出阶段一的实际结果。** 依次执行并保留输出文件：

```bash
uv run qt backtest run --period in
uv run qt backtest sweep --param strategy.take_profit_mode=fixed,trailing
uv run qt backtest sweep --param strategy.min_dividend_yield=0.03,0.04,0.05 --param strategy.max_price_position=0.1,0.2,0.3
uv run qt backtest sweep --param strategy.stop_loss=0.10,0.15,0.20 --param risk.cooldown_days=0,60
uv run qt backtest run --period out
uv run qt backtest run --period full
```

- [ ] **Step 8: 交付检查点。** 把三份报告和三份对比表交给用户，并如实汇报：默认参数下策略与红利 ETF、沪深300 全收益的对比；固定止盈与移动止盈哪个更好；结果对参数是否敏感；样本外表现是否与样本内接近；数据缺口可能造成的偏差。**不要自行修改默认参数**，由用户看过结果后决定。若策略扣除成本后不优于红利 ETF，按设计文档第 8 节与用户商量是改规则还是换思路，不进入阶段二。

---

## 阶段一完成的标志

- `./scripts/check.sh` 全部通过，无任何告警。
- `uv run qt data update` 可重复运行，第二次运行只做增量。
- 用户拿到样本内、样本外、全区间三份 HTML 报告和参数对比表。
- 报告里写明了数据局限和近似处理。
