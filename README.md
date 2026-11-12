# A 股量化回测

这是一个个人用的 A 股量化回测项目：用免费数据（AKShare）下载沪深主板的日线、分红、财务数据，在历史上回测一个"高股息、低位"的选股策略，生成回测报告，并支持对参数做对比、用样本外数据检验。项目只产生信号和报告，不连接任何券商，也不会下单；是否买卖、买卖多少，完全由你自己决定。

回测结果不代表未来收益，本项目不构成投资建议。

## 安装

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。在项目根目录执行：

```bash
uv sync
```

## 三条命令

所有命令都在项目根目录运行，路径默认相对于项目根目录。

### 1. 下载和更新数据

```bash
uv run qt data update
```

首次完整运行需要几个小时（预计 3 到 6 小时，要逐只股票请求数据，并有请求间隔以免被限流），中途中断后再运行会从断点继续，已经下载好的不会重复下载。以后每次运行只下载新增的交易日和有变化的数据。

- 想试一下、只更新几只股票：`uv run qt data update --symbols 600000,600036`
- 想让当天的数据也进来，请在交易日 16:00 之后运行；16:00 之前运行，数据只更新到上一个交易日。
- 更新结束后会自动做一次数据完整性检查，有问题会逐条打印，并以非零状态退出。
- 其他选项：`--config` 指定配置文件，`--data-dir` 指定数据目录（默认 `data`）。

### 2. 回测并生成报告

```bash
uv run qt backtest run --period in     # 样本内：2015-01-01 至 2021-12-31
uv run qt backtest run --period out    # 样本外：2022-01-01 至最新数据日
uv run qt backtest run --period full   # 全区间：样本内起点至最新数据日
```

在 `reports/` 下生成一个自包含的 HTML 报告，如 `reports/backtest-in-20261112-153000.html`，直接用浏览器打开。报告包含与沪深300 全收益、红利 ETF 的对比、净值与回撤图、逐笔交易、风控事件、本次使用的全部参数，以及数据局限。其他选项：`--config`、`--data-dir`、`--out`（报告目录，默认 `reports`）、`--data-gaps`（数据局限文档，默认 `docs/data-gaps.md`）。

### 3. 参数对比

```bash
uv run qt backtest sweep --param strategy.take_profit_mode=fixed,trailing
uv run qt backtest sweep \
  --param strategy.min_dividend_yield=0.03,0.04,0.05 \
  --param strategy.max_price_position=0.1,0.2,0.3
```

`--param 路径=值1,值2,...` 可以重复多次，命令会对所有参数取值的组合（笛卡尔积）逐一回测，上面第二条是 3 × 3 = 9 组。路径写成 `区段.字段`，就是 `config/default.yaml` 里的写法；值按字段类型自动转换，写错了会给出中文提示。取值是列表的字段（如 `universe.boards`）无法用命令行扫描，请直接改配置文件。结果在屏幕上打印成表格，同时保存为 `reports/sweep-{时间戳}.csv`。其他选项：`--config`、`--data-dir`、`--out`。

参数对比只在样本内区间运行，并且不允许把 `backtest.in_sample_start`、`backtest.in_sample_end`、`backtest.out_sample_start` 当作参数来扫。这是有意的：设计要求 2022 年之后的数据只用来检验，不参与调参。挑好参数后，再用 `--period out` 看它在没见过的数据上表现如何。

## 输出放在哪里

| 位置 | 内容 |
|---|---|
| `data/` | 下载的数据（Parquet 文件），不进入 git |
| `data/update-failures.json` | 最近一次更新中失败的股票及原因 |
| `reports/` | 回测 HTML 报告与参数对比 CSV，不进入 git |

## 如何修改参数

所有数值参数都在 `config/default.yaml` 里，代码里没有写死的数字。直接编辑它，或者复制一份、改完后用 `--config` 指定：

```bash
cp config/default.yaml config/my.yaml
uv run qt backtest run --period in --config config/my.yaml
```

配置分六个区段：

- `universe`：股票池，包含的板块、最短上市年限、近 60 日平均成交额下限。
- `strategy`：选股与卖出规则，股息率、价格位置、ROE、分红率等买入条件，止盈止损与时间止损。
- `risk`：风控，单只仓位上限、最大持仓数、行业集中度上限、回撤预警与暂停线、冷却期。
- `costs`：交易成本，佣金、最低佣金、印花税、过户费、滑点。
- `backtest`：回测本金、数据起点、样本内外的日期分界、买单有效天数、对比基准。
- `data`：数据下载，刷新间隔、请求间隔、重试次数、行业覆盖率下限。

每个概念的含义与例子见 [docs/concepts.md](docs/concepts.md)。配置文件不允许出现未知的键，拼错会报错而不是被悄悄忽略。

## 如何添加一个新策略

策略是实现了 `Strategy` 协议（`src/qt/strategy/base.py`）的类：有一个 `name` 属性，和两个方法 `buy_candidates(day, snapshot, held)`、`sell_signals(day, snapshot, positions)`，只读每日指标快照，产出买入候选和卖出信号。现有的实现 `src/qt/strategy/high_dividend_low.py` 可以作为参考。

1. 在 `src/qt/strategy/` 下新建一个文件，写这个类。策略和风控模块不得依赖回测引擎，这样将来实盘可以复用同一份代码。
2. 在 `src/qt/backtest/report.py` 的 `run_period` 函数里，找到带有注释"想换策略，在这里替换"的那一行 `strategy = HighDividendLow(cfg.strategy, cfg.universe)`，把它换成你的类。
3. 新策略需要的参数，加到 `src/qt/config.py` 的配置模型和 `config/default.yaml` 里（配置禁止未知键，所以必须两处同时加）。需要新的每日指标时，在 `src/qt/features.py` 里扩展。

## 质量检查

```bash
./scripts/check.sh
```

依次运行 ruff 检查、ruff 格式检查、pyright 严格类型检查和 pytest，全部通过且没有任何告警才算合格。

## 已知局限

免费数据有缺口，回测里做了近似处理，这些会影响结果的可信度。完整清单和每一项偏差的方向见 [docs/data-gaps.md](docs/data-gaps.md)，每份回测报告的"数据局限与近似处理"一节也会逐条列出。看结果之前请先读一遍。

回测结果不代表未来收益，本项目不构成投资建议。
