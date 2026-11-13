# pyright: basic
"""每日增量更新耗时实测：对抽样股票执行一次 update_all，按阶段计时并外推整晚耗时。

运行（容器内）：
    docker compose run --rm worker uv run python scripts/probe_daily_update.py --symbols 60

不会改动原数据目录：先把 --data-dir 整个复制到临时目录，再在副本上运行。
通过 qt.data.update.update_all 调用，不直接访问 AKShare。
"""

import argparse
import shutil
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pandas as pd

from qt.config import AppConfig, load_config
from qt.data.akshare_source import AkshareSource
from qt.data.schema import Table
from qt.data.source import DataSource
from qt.data.store import ParquetStore
from qt.data.update import (
    latest_closed_day,
    load_calendar,
    refresh_fundamentals,
    update_all,
)

# 全市场证券数与财务分红每晚刷新的比例（滚动刷新 7 天一轮）
TOTAL_SYMBOLS = 3487
FUNDAMENTALS_ROUND_DAYS = 7


class TimedSource:
    """包装数据源，累计各接口的调用次数与耗时。"""

    def __init__(self, inner: AkshareSource) -> None:
        self._inner = inner
        self.seconds: dict[str, float] = defaultdict(float)
        self.calls: dict[str, int] = defaultdict(int)

    def _timed[T](self, name: str, value_fn: Callable[[], T]) -> T:
        start = time.perf_counter()
        try:
            return value_fn()
        finally:
            self.seconds[name] += time.perf_counter() - start
            self.calls[name] += 1

    def reset(self) -> None:
        self.seconds.clear()
        self.calls.clear()

    def set_as_of(self, as_of: date) -> None:
        self._inner.set_as_of(as_of)

    def industry(self, symbol: str) -> tuple[str, bool] | None:
        return self._timed("industry", lambda: self._inner.industry(symbol))

    def trade_days(self) -> pd.DataFrame:
        return self._timed("trade_days", self._inner.trade_days)

    def securities(self) -> pd.DataFrame:
        return self._timed("securities", self._inner.securities)

    def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        return self._timed("bars", lambda: self._inner.bars(symbol, start, end))

    def dividends(self, symbol: str) -> pd.DataFrame:
        return self._timed("dividends", lambda: self._inner.dividends(symbol))

    def financials(self, symbol: str) -> pd.DataFrame:
        return self._timed("financials", lambda: self._inner.financials(symbol))

    def industries(self) -> pd.DataFrame:
        return self._timed("industries", self._inner.industries)

    def st_history(self) -> pd.DataFrame:
        return self._timed("st_history", self._inner.st_history)

    def benchmark(self, code: str, start: date, end: date) -> pd.DataFrame:
        return self._timed("benchmark", lambda: self._inner.benchmark(code, start, end))


def sample_symbols(store: ParquetStore, count: int) -> list[str]:
    """从在市证券里按代码排序均匀抽样 count 只。"""
    secs = store.read(Table.SECURITIES)
    listed = sorted(str(s) for s in secs[secs["delist_date"].isna()]["symbol"])
    if count >= len(listed):
        return listed
    step = len(listed) / count
    return [listed[int(i * step)] for i in range(count)]


def report(label: str, timed: TimedSource, names: list[str]) -> None:
    for name in names:
        print(f"  {label}{name}: {timed.calls[name]} 次，合计 {timed.seconds[name]:.1f}s")


def run(data_dir: Path, count: int, config: Path) -> int:
    """返回退出码：有任何失败时为 1（耗时估计无效）。"""
    cfg: AppConfig = load_config(config)
    with tempfile.TemporaryDirectory(prefix="qt-probe-") as tmp:
        work = Path(tmp) / "data"
        t0 = time.perf_counter()
        shutil.copytree(data_dir, work)  # 保留修改时间，滚动刷新依赖文件年龄
        print(f"已复制数据目录到 {work}（{time.perf_counter() - t0:.1f}s），原目录不会被改动")
        store = ParquetStore(work)
        timed = TimedSource(AkshareSource.from_config(cfg))
        source = cast(DataSource, timed)
        symbols = sample_symbols(store, count)
        print(f"抽样 {len(symbols)} 只（在市证券中均匀抽取）：{symbols[0]} … {symbols[-1]}")

        # 阶段 0：交易日历，并确定 as_of
        t = time.perf_counter()
        store.write(Table.TRADE_DAYS, timed.trade_days())
        as_of = latest_closed_day(datetime.now(ZoneInfo("Asia/Shanghai")), load_calendar(store))
        calendar_s = time.perf_counter() - t
        print(f"as_of（最近已收盘交易日）= {as_of}；交易日历 {calendar_s:.1f}s")

        # 阶段 1：update_all（共享表 + 抽样股票日线；财务分红按滚动刷新只轮到一部分）
        timed.reset()
        t = time.perf_counter()
        result = update_all(source, store, cfg, as_of, symbols, refresh_trade_days=False)
        total_all = time.perf_counter() - t
        print(
            f"update_all 完成 {total_all:.1f}s：更新 {result.updated}，跳过 {result.skipped}，"
            f"失败 {len(result.failed)}"
        )
        for symbol, message in list(result.failed.items())[:5]:
            print(f"  失败 {symbol}: {message[:120]}")
        report("", timed, ["securities", "st_history", "benchmark", "industry", "bars"])
        bars_s = timed.seconds["bars"]
        bars_n = max(timed.calls["bars"], 1)
        shared_s = sum(
            timed.seconds[k] for k in ("securities", "st_history", "benchmark", "industry")
        )
        roll_s = timed.seconds["dividends"] + timed.seconds["financials"]
        print(
            f"  update_all 内顺带的滚动财务分红：分红 {timed.calls['dividends']} 次、"
            f"财务 {timed.calls['financials']} 次，合计 {roll_s:.1f}s"
        )

        # 阶段 2：强制对全部抽样股票下载财务与分红，得到每只的完整耗时
        timed.reset()
        t = time.perf_counter()
        failed = refresh_fundamentals(source, store, cfg, symbols)
        fund_total = time.perf_counter() - t
        fund_per = fund_total / len(symbols)
        print(f"财务分红（强制全部 {len(symbols)} 只）：{fund_total:.1f}s，失败 {len(failed)}")
        for symbol, message in list(failed.items())[:5]:
            print(f"  财务分红失败 {symbol}: {message[:120]}")
        report("", timed, ["dividends", "financials"])

        bars_per = bars_s / bars_n
        print("\n=== 汇总 ===")
        print(f"日线阶段：{bars_s:.1f}s（有请求 {bars_n} 只），平均每只 {bars_per:.2f}s")
        print(f"财务分红阶段：{fund_total:.1f}s，平均每只 {fund_per:.2f}s")
        print(f"共享表（证券/ST/基准）：{shared_s + calendar_s:.1f}s")
        night_bars = bars_per * TOTAL_SYMBOLS
        night_fund = fund_per * TOTAL_SYMBOLS / FUNDAMENTALS_ROUND_DAYS
        night = shared_s + calendar_s + night_bars + night_fund
        print(
            f"外推整晚（{TOTAL_SYMBOLS} 只；财务分红 1/{FUNDAMENTALS_ROUND_DAYS}）："
            f"日线 {night_bars / 60:.1f} 分钟 + 财务分红 {night_fund / 60:.1f} 分钟 "
            f"+ 共享表 {(shared_s + calendar_s) / 60:.1f} 分钟 = {night / 60:.1f} 分钟 "
            f"（{night / 3600:.2f} 小时）"
        )
        print("阈值：4 小时" + ("；超过，需上报 BLOCKED" if night > 4 * 3600 else "；未超过"))
        if result.failed or failed:
            print(
                f"\n!!! 警告：本次有失败（日线等阶段 {len(result.failed)} 只，"
                f"财务分红 {len(failed)} 只），"
                "失败的请求耗时不具代表性，上面的外推估计无效，请排查后重跑 !!!"
            )
            return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="每日增量更新耗时实测")
    parser.add_argument("--symbols", type=int, default=60, help="抽样股票数")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="原数据目录（只读）")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    args = parser.parse_args()
    raise SystemExit(run(args.data_dir, args.symbols, args.config))


if __name__ == "__main__":
    main()
