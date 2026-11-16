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
- 每处理 20 只股票打印一行带时间的进度。每次网络请求最多等 `data.request_timeout_seconds`（默认 90 秒），超时算一次失败并重试；连续 `data.max_consecutive_failures`（默认 20）只股票因网络错误失败，会判断为断网并中止，网络恢复后重新运行即可续传。
- 更新结束后会自动对最新一个交易日做一次数据完整性检查，有问题会逐条打印，并以非零状态退出。数据源这次返回的证券明显少于已存储的（少 2% 以上）也算问题；已存储的证券不会因此被删除。
- 其他选项：`--config` 指定配置文件，`--data-dir` 指定数据目录（默认 `data`）。
- 收盘后（16:00 之后、下一个交易日 09:00 之前）运行时，当天的日线先用一次批量行情写入，对不上的股票再逐只请求，比全部逐只请求快得多；运行结束会打印一行"日线批量行情：……"。规则见 `docs/data-gaps.md` 第 4 节；配置 `data.bulk_bars: false` 可关闭。
- 核对批量行情与逐只接口是否一致（只读，不写数据；默认抽 30 只，有不一致时以非零状态退出）：`uv run qt data check-snapshot --sample 30`

全部历史日线的完整性扫描（价格或复权因子缺失或非有限、价格不为正、最高价低于最低价、复权因子不为正、复权收盘价单日涨跌超过 21%）单独运行，只读不写原始数据，结果供参考，回测报告会显示各类异常的数量：

```bash
uv run qt data check
```

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

## 第二阶段：每日运行

在回测之外，项目还能作为多人使用的每日信号服务运行：每个交易日 18:00 起更新数据，按每位使用者订阅的参数计算信号，发邮件，并记录“系统账”（照信号严格执行的模拟账）和“手动账”（你实际做了的买卖）。系统只出信号，不下单，不构成投资建议。

用 Docker Compose 部署（详见 [Mac 部署说明](docs/deploy-mac.md)，迁移见 [迁移到服务器](docs/migrate-to-server.md)，使用者须知见 [每日信号与回测的差异](docs/live-vs-backtest.md)）：

```bash
cp .env.example .env                  # 填写邮箱、管理员邮箱等；授权码只放在 .env 里
docker compose up -d                  # db、migrate（升级数据库）、worker（定时进程）
docker compose ps
docker compose logs -f worker         # 查看运行日志
./scripts/backup_db.sh                # 手动备份数据库（worker 每日运行后也会自动备份，保留 14 天）
```

常用命令（容器内执行，前缀 `docker compose run --rm worker`；也可在本机用 `uv run --env-file .env qt ...`）：

```bash
qt admin user add --email you@example.com --admin
qt admin sub create --email you@example.com --cash 150000 [--start 2026-11-13] [--set strategy.stop_loss=0.10]
                                                      # --start：起始日（缺省下一个交易日；选最近已收盘的交易日可立即试运行）
qt admin sub show --email you@example.com
qt admin book show --email you@example.com            # 手动账；加 --book system 看系统账
qt admin trade buy 600001 --email you@example.com --price 10.5 --shares 1000
qt admin signals --email you@example.com --status 已通知
qt admin runs --last 10                               # 每日运行记录
qt run-day [--day 2026-11-13]                         # 手动跑一天
```

Mac 必须在 18:00 起的约 3.5 小时里保持唤醒并接电，防休眠设置见部署说明。

## 输出放在哪里

| 位置 | 内容 |
|---|---|
| `data/` | 下载的数据（Parquet 文件），不进入 git |
| `data/update-failures.json` | 最近一次全量更新中失败的股票及原因（回测报告据此统计取不到日线的退市股票） |
| `data/update-failures-partial.json` | 最近一次 `--symbols` 部分更新的失败明细（不覆盖全量记录） |
| `data/history-anomalies.csv` | `qt data check` 发现的历史行情异常明细 |
| `reports/` | 回测 HTML 报告与参数对比 CSV，不进入 git |

## 如何修改参数

策略、风控、成本、回测区间和数据下载的数值参数都在 `config/default.yaml` 里。直接编辑它，或者复制一份、改完后用 `--config` 指定：

```bash
cp config/default.yaml config/my.yaml
uv run qt backtest run --period in --config config/my.yaml
```

配置分六个区段：

- `universe`：股票池，包含的板块（只支持 `sh_main` 沪市主板、`sz_main` 深市主板；创业板、科创板、北交所的涨跌幅规则不同，不支持）、最短上市年限、近 60 日平均成交额下限。
- `strategy`：选股与卖出规则，股息率、价格位置、ROE、分红率等买入条件，是否排除已判定削减分红的股票（`exclude_dividend_cut`，默认 true），止盈止损与时间止损。
- `risk`：风控，单只仓位上限、最大持仓数、行业集中度上限、回撤预警与暂停线、冷却期。
- `costs`：交易成本，佣金、最低佣金、印花税、过户费、滑点。印花税按历史税率分段（`stamp_tax_schedule`，每段写起始日和税率，按起始日升序）：默认 2008-09-19 起 0.1%，2023-08-28 起 0.05%，每笔卖出按成交当天有效的税率计算。
- `backtest`：回测本金、数据起点、样本内外的日期分界、买单有效天数、对比基准。
- `data`：数据下载，刷新间隔、请求间隔、重试次数、单次请求时限、断网熔断的连续失败只数、行业覆盖率下限、证券表缩水的报警比例。

另有少量属于市场规则或数据校验口径的数字写在代码里，不在配置中：一手 100 股（`src/qt/costs.py`）；主板涨跌停 ±10%、ST ±5%（`src/qt/backtest/engine.py`）；财年 Y 的分红在次年 8 月 31 日后才算完整（`src/qt/features.py`）；数据检查的单日涨跌阈值 21%、新股上市前 5 个交易日不查、当日日线覆盖率下限 80%、16:00 视为收盘（`src/qt/data/update.py`）；夏普比率按一年 244 个交易日年化（`src/qt/backtest/metrics.py`）；报告里最大回撤达 40% 的红色提示（`src/qt/backtest/report.py`）。

每个概念的含义与例子见 [docs/concepts.md](docs/concepts.md)。配置文件不允许出现未知的键，拼错会报错而不是被悄悄忽略；每个数值也有取值范围（比例在 0 到 1 之间、天数和个数为正、不接受 NaN 与无穷大，`in_sample_start < in_sample_end < out_sample_start`），超出范围同样报错，`--param` 的取值也按同样的规则检查。

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
