"""核对批量行情与逐只日线（qt data check-snapshot）：只读，不写数据目录。"""

import random
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from qt.data.schema import Table
from qt.data.source import DataSource
from qt.data.store import ParquetStore
from qt.data.update import (
    SNAPSHOT_MIN_MATCH,
    AsOfAware,
    Quote,
    latest_closed_day,
    load_calendar,
    prev_close_agrees,
    prev_close_matches,
    snapshot_quotes,
    snapshot_window_problem,
    traded,
    zeroed,
)

# 逐项比较的列：批量行情与逐只日线都有
_FIELDS = ("open", "high", "low", "close", "volume", "amount")
# 另外抽查的“批量行情显示停牌”的股票数上限
SUSPENDED_SAMPLE = 5


@dataclass(frozen=True)
class SnapshotCheck:
    """核对结果：lines 逐行打印；ok 为假表示有不一致或无法核对的股票（命令以 1 退出）。"""

    as_of: date
    lines: list[str]
    ok: bool


def _stored_prev(store: ParquetStore, prev: date) -> dict[str, tuple[float, float]]:
    """库里 prev 当天每只股票的 (收盘价, 复权因子)。"""
    bars = store.read(Table.BARS, start=prev, end=prev)
    return {
        str(s): (float(c), float(a))
        for s, c, a in zip(bars["symbol"], bars["close"], bars["adj_factor"], strict=True)
    }


def _compare(
    quote: Quote, bar: pd.Series, stored: tuple[float, float] | None
) -> tuple[list[str], bool]:
    """返回 (不一致的说明, 是否当天除权)。"""
    diffs = [
        f"{col} 批量 {getattr(quote, col)!r} / 逐只 {float(bar[col])!r}"
        for col in _FIELDS
        if getattr(quote, col) != float(bar[col])
    ]
    if stored is None:
        return diffs, False
    close, adj_factor = stored
    if not prev_close_agrees(quote, close):
        return diffs, True  # 批量路径不会用它，复权因子变化是预期的
    if float(bar["adj_factor"]) != adj_factor:
        diffs.append(f"复权因子 逐只 {float(bar['adj_factor'])!r} / 沿用值 {adj_factor!r}")
    return diffs, False


def check_snapshot(
    source: DataSource, store: ParquetStore, now: datetime, sample: int, rng: random.Random
) -> SnapshotCheck:
    """取一次批量行情，对抽样股票逐只请求同一天的日线，逐项比较；不写任何数据。

    as_of 按已存储的交易日历取 now 时最近一个已收盘的交易日。抽样范围：库里在市的证券中批量行情
    显示当天有成交的 sample 只，另加批量行情显示停牌（成交量、开盘价、最新价都是 0）的至多
    SUSPENDED_SAMPLE 只。有成交的：比较开高低收、成交量、成交额是否完全相同；库里有前一交易日日线
    且昨收对得上的，再看逐只接口给出的复权因子是否等于沿用值；昨收对不上的（当天除权）且价格一致的
    单独计数，不算不一致。停牌的：逐只接口不应有 as_of 的日线，有则不一致。
    每只股票恰好计入一类（一致、不一致、当天除权、无法核对），各类之和等于核对的只数。
    """
    calendar = load_calendar(store)
    as_of = latest_closed_day(now, calendar)
    prev = calendar.offset(as_of, -1)
    lines = [f"核对日（最近一个已收盘交易日）：{as_of}；只读，不写入任何数据"]
    problem = snapshot_window_problem(now, as_of, calendar)
    if problem is not None:
        lines.append(f"注意：{problem}；批量行情可能不是 {as_of} 的数据")
    if isinstance(source, AsOfAware):
        source.set_as_of(as_of)
    try:
        quotes = snapshot_quotes(source.daily_snapshot())
    except Exception as exc:  # 只请求一次，不重试
        lines.append(f"批量行情获取失败：{type(exc).__name__}: {exc}")
        return SnapshotCheck(as_of, lines, ok=False)
    stored = _stored_prev(store, prev)
    secs = store.read(Table.SECURITIES)
    listed = {str(s) for s in secs.loc[secs["delist_date"].isna(), "symbol"]}
    closes = {s: c for s, (c, _) in stored.items() if s in listed}
    matched, total = prev_close_matches(quotes, closes)
    share = f"{matched / total:.1%}" if total else "—"
    lines.append(
        f"批量行情 {len(quotes)} 只；昨收与库里 {prev} 的收盘价相符 {matched}/{total}（{share}），"
        f"整晚使用批量行情要求不低于 {SNAPSHOT_MIN_MATCH:.0%}"
    )
    population = sorted(s for s in listed if s in quotes and traded(quotes[s]))
    chosen = sorted(rng.sample(population, min(sample, len(population))))
    idle = sorted(s for s in listed if s in quotes and zeroed(quotes[s]))
    idle_chosen = sorted(rng.sample(idle, min(SUSPENDED_SAMPLE, len(idle))))
    same = differ = ex_rights = unable = 0
    for symbol in [*chosen, *idle_chosen]:
        try:
            bars = source.bars(symbol, as_of, as_of)
        except Exception as exc:  # 单只失败不影响其他股票
            unable += 1
            lines.append(f"  {symbol}：无法核对：逐只请求失败：{type(exc).__name__}: {exc}")
            continue
        today = bars[bars["day"] == pd.Timestamp(as_of)]
        if symbol in idle_chosen:
            if len(today) == 0:
                same += 1
            else:
                differ += 1
                lines.append(f"  {symbol}：不一致：批量行情显示停牌，逐只接口却有 {as_of} 的日线")
            continue
        if len(today) == 0:
            differ += 1
            lines.append(f"  {symbol}：不一致：逐只接口没有 {as_of} 的日线，批量行情却有成交")
            continue
        diffs, shifted = _compare(quotes[symbol], today.iloc[0], stored.get(symbol))
        if diffs:
            differ += 1
            lines.append(f"  {symbol}：不一致：{'；'.join(diffs)}")
        elif shifted:
            ex_rights += 1
            lines.append(f"  {symbol}：当天除权（昨收对不上），批量路径会改走逐只请求")
        else:
            same += 1
    total_checked = len(chosen) + len(idle_chosen)
    lines.append(
        f"核对 {total_checked} 只（有成交 {len(chosen)} 只、停牌 {len(idle_chosen)} 只）："
        f"一致 {same} 只，不一致 {differ} 只，"
        f"当天除权（昨收对不上，批量路径会改走逐只请求）{ex_rights} 只，无法核对 {unable} 只"
    )
    return SnapshotCheck(as_of, lines, ok=differ == 0 and unable == 0)
