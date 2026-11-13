"""风控层测试。"""

from datetime import date, timedelta

import pytest

from qt.calendar import TradingCalendar
from qt.config import RiskConfig
from qt.costs import CostModel
from qt.models import ExitReason, Order, Side
from qt.risk import RiskEventKind, RiskManager
from tests.fakes import default_costs, default_risk, make_candidate, make_position


def _weekdays(n: int = 400) -> list[date]:
    out: list[date] = []
    d = date(2024, 1, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays()
CAL = TradingCalendar(DAYS)


def _mgr(cfg: RiskConfig | None = None) -> RiskManager:
    return RiskManager(cfg or default_risk(), CAL, CostModel(default_costs()))


def test_sizes_to_ten_percent_in_board_lots() -> None:
    orders, rej = _mgr().size_buys(DAYS[0], [make_candidate("A")], 150_000, 150_000, [], {})
    assert rej == []
    assert len(orders) == 1
    o = orders[0]
    assert (o.symbol, o.side, o.shares, o.signal_day) == ("A", Side.BUY, 1400, DAYS[0])
    assert o.reason == "测试买入" and o.exit_reason is None


def test_lot_too_expensive() -> None:
    orders, rej = _mgr().size_buys(DAYS[0], [make_candidate("A", 200)], 150_000, 150_000, [], {})
    assert orders == []
    assert [r.rule for r in rej] == ["lot_too_expensive"]
    assert "200" in rej[0].message


def test_no_cash() -> None:
    orders, rej = _mgr().size_buys(DAYS[0], [make_candidate("A")], 150_000, 800, [], {})
    assert orders == []
    assert [r.rule for r in rej] == ["no_cash"]


def test_max_positions_counts_pending_buys() -> None:
    pos = [make_position(f"P{i}", 100, 1000, industry=f"I{i}") for i in range(8)]
    pend = [Order(f"Q{i}", Side.BUY, 100, DAYS[0], "x") for i in range(2)]
    closes = {p.symbol: 10.0 for p in pos}
    orders, rej = _mgr().size_buys(
        DAYS[1], [make_candidate("A", industry="Z")], 150_000, 100_000, pos, closes, pend
    )
    assert orders == []
    assert [r.rule for r in rej] == ["max_positions"]


def test_pending_buys_reduce_cash() -> None:
    pend = [Order("Q", Side.BUY, 1000, DAYS[0], "x")]
    mgr = _mgr()
    cost = CostModel(default_costs()).buy_cost(10.0, 1000)
    orders, rej = mgr.size_buys(
        DAYS[1], [make_candidate("A")], 150_000, cost + 500, [], {"Q": 10.0}, pend
    )
    assert orders == []
    assert [r.rule for r in rej] == ["no_cash"]


def test_industry_cap_trims_then_rejects() -> None:
    held = [make_position("H", 4000, 40_000)]
    orders, rej = _mgr().size_buys(
        DAYS[0], [make_candidate("A")], 150_000, 100_000, held, {"H": 10.0}
    )
    assert rej == [] and [o.shares for o in orders] == [500]
    held = [make_position("H", 4500, 45_000)]
    orders, rej = _mgr().size_buys(
        DAYS[0], [make_candidate("A")], 150_000, 100_000, held, {"H": 10.0}
    )
    assert orders == [] and [r.rule for r in rej] == ["industry_cap"]


def test_industry_cap_exact_limit_not_exceeded() -> None:
    # 已持 30000，再买 15000 恰好等于 45000 上限，不算超限
    held = [make_position("H", 3000, 30_000)]
    orders, _ = _mgr().size_buys(
        DAYS[0], [make_candidate("A")], 150_000, 100_000, held, {"H": 10.0}
    )
    assert [o.shares for o in orders] == [1400]
    held = [make_position("H", 3050, 30_500)]
    orders, _ = _mgr().size_buys(
        DAYS[0], [make_candidate("A")], 150_000, 100_000, held, {"H": 10.0}
    )
    assert [o.shares for o in orders] == [1400]  # 30500+14000=44500 未超


def test_earlier_candidate_consumes_cash_for_later() -> None:
    cands = [make_candidate("A", industry="X"), make_candidate("B", industry="Y")]
    orders, rej = _mgr().size_buys(DAYS[0], cands, 150_000, 15_000, [], {})
    assert [o.symbol for o in orders] == ["A"]
    assert [(r.candidate.symbol, r.rule) for r in rej] == [("B", "no_cash")]


def test_earlier_candidate_counts_toward_industry_and_positions() -> None:
    cands = [make_candidate(s) for s in "ABCDE"]  # 同属银行，上限 45000
    orders, rej = _mgr().size_buys(DAYS[0], cands, 150_000, 150_000, [], {})
    assert [(o.symbol, o.shares) for o in orders] == [
        ("A", 1400),
        ("B", 1400),
        ("C", 1400),
        ("D", 300),  # 剩余额度 3000 元，被裁剪
    ]
    assert [(r.candidate.symbol, r.rule) for r in rej] == [("E", "industry_cap")]


def test_missing_close_uses_candidate_close() -> None:
    orders, _ = _mgr().size_buys(DAYS[0], [make_candidate("A", 20)], 150_000, 150_000, [], {})
    assert [o.shares for o in orders] == [700]


def test_warn_once_at_15_percent() -> None:
    mgr = _mgr()
    kinds = [
        [e.kind for e in mgr.on_day_close(DAYS[i], nav)] for i, nav in enumerate([1.0, 0.85, 0.84])
    ]
    assert kinds == [[], [RiskEventKind.WARN], []]
    assert mgr.buying_allowed()


def test_warn_rearms_after_new_high() -> None:
    mgr = _mgr()
    navs = [1.0, 0.85, 1.05, 0.89]
    kinds = [[e.kind for e in mgr.on_day_close(DAYS[i], n)] for i, n in enumerate(navs)]
    assert kinds == [[], [RiskEventKind.WARN], [], [RiskEventKind.WARN]]


def test_pause_at_20_percent_blocks_buys() -> None:
    mgr = _mgr()
    mgr.on_day_close(DAYS[0], 1.0)
    ev = mgr.on_day_close(DAYS[1], 0.80)
    assert [e.kind for e in ev] == [RiskEventKind.PAUSE]
    assert ev[0].drawdown == pytest.approx(-0.20)
    assert "20" in ev[0].message
    assert not mgr.buying_allowed()
    orders, rej = mgr.size_buys(DAYS[2], [make_candidate("A")], 150_000, 150_000, [], {})
    assert orders == [] and [r.rule for r in rej] == ["paused"]


def test_pause_emits_only_pause_when_crossing_both() -> None:
    mgr = _mgr()
    mgr.on_day_close(DAYS[0], 1.0)
    assert [e.kind for e in mgr.on_day_close(DAYS[1], 0.7)] == [RiskEventKind.PAUSE]


def test_auto_resume_after_60_trading_days_resets_peak() -> None:
    mgr = _mgr()
    mgr.on_day_close(DAYS[0], 1.0)
    mgr.on_day_close(DAYS[1], 0.80)
    assert mgr.on_day_close(DAYS[60], 0.78) == []  # 距离 59
    assert not mgr.buying_allowed()
    ev = mgr.on_day_close(DAYS[61], 0.78)  # 距离 60
    assert [e.kind for e in ev] == [RiskEventKind.RESUME]
    assert mgr.buying_allowed()
    assert mgr.on_day_close(DAYS[62], 0.77) == []  # 相对新峰值 0.78 回撤约 1.3%


def test_resume_rearms_warn() -> None:
    mgr = _mgr()
    mgr.on_day_close(DAYS[0], 1.0)
    mgr.on_day_close(DAYS[1], 0.85)  # WARN
    mgr.on_day_close(DAYS[2], 0.80)  # PAUSE
    mgr.on_day_close(DAYS[62], 0.80)  # RESUME
    # 恢复后峰值为 0.80，0.67 回撤 -16.25%：只发 WARN（已重新武装），不是 PAUSE
    assert [e.kind for e in mgr.on_day_close(DAYS[63], 0.67)] == [RiskEventKind.WARN]


def test_manual_resume_and_extend() -> None:
    mgr = _mgr()
    mgr.on_day_close(DAYS[0], 1.0)
    mgr.on_day_close(DAYS[1], 0.80)
    mgr.extend_pause(10)
    assert mgr.on_day_close(DAYS[61], 0.80) == []
    assert [e.kind for e in mgr.on_day_close(DAYS[71], 0.80)] == [RiskEventKind.RESUME]

    mgr = _mgr()
    mgr.on_day_close(DAYS[0], 1.0)
    mgr.on_day_close(DAYS[1], 0.80)
    ev = mgr.resume(DAYS[5], 0.79)
    assert ev.kind is RiskEventKind.RESUME and mgr.buying_allowed()
    assert mgr.on_day_close(DAYS[6], 0.78) == []
    with pytest.raises(ValueError):
        mgr.resume(DAYS[7], 0.78)


def test_cooldown_after_stop_loss_but_not_take_profit() -> None:
    mgr = _mgr()
    d0 = DAYS[0]
    mgr.note_exit("A", d0, ExitReason.STOP_LOSS)
    mgr.note_exit("B", d0, ExitReason.TAKE_PROFIT_FIXED)
    cands = [make_candidate("A", industry="X"), make_candidate("B", industry="Y")]
    orders, rej = mgr.size_buys(DAYS[59], cands, 150_000, 150_000, [], {})
    assert [o.symbol for o in orders] == ["B"]
    assert [(r.candidate.symbol, r.rule) for r in rej] == [("A", "cooldown")]
    orders, rej = mgr.size_buys(DAYS[60], cands[:1], 150_000, 150_000, [], {})
    assert [o.symbol for o in orders] == ["A"] and rej == []
    orders, _ = mgr.size_buys(DAYS[1], cands[1:], 150_000, 150_000, [], {})
    assert [o.symbol for o in orders] == ["B"]


@pytest.mark.parametrize("reason", [ExitReason.TIME_STOP, ExitReason.FUNDAMENTAL])
def test_cooldown_reasons(reason: ExitReason) -> None:
    mgr = _mgr()
    mgr.note_exit("A", DAYS[0], reason)
    _, rej = mgr.size_buys(DAYS[1], [make_candidate("A")], 150_000, 150_000, [], {})
    assert [r.rule for r in rej] == ["cooldown"]


def test_delisted_no_cooldown_and_zero_days_disables() -> None:
    mgr = _mgr()
    mgr.note_exit("A", DAYS[0], ExitReason.DELISTED)
    orders, _ = mgr.size_buys(DAYS[1], [make_candidate("A")], 150_000, 150_000, [], {})
    assert len(orders) == 1
    mgr = _mgr(default_risk().model_copy(update={"cooldown_days": 0}))
    mgr.note_exit("A", DAYS[0], ExitReason.STOP_LOSS)
    orders, _ = mgr.size_buys(DAYS[1], [make_candidate("A")], 150_000, 150_000, [], {})
    assert len(orders) == 1


def test_already_held_in_positions() -> None:
    held = [make_position("A", 100, 1000, industry="X")]
    orders, rej = _mgr().size_buys(
        DAYS[0], [make_candidate("A", industry="X")], 150_000, 150_000, held, {"A": 10.0}
    )
    assert orders == [] and [r.rule for r in rej] == ["already_held"]
    assert "A" in rej[0].message and "持仓" in rej[0].message


def test_already_held_in_pending_buys() -> None:
    pend = [Order("A", Side.BUY, 100, DAYS[0], "x")]
    orders, rej = _mgr().size_buys(DAYS[1], [make_candidate("A")], 150_000, 150_000, [], {}, pend)
    assert orders == [] and [r.rule for r in rej] == ["already_held"]
    assert "待成交" in rej[0].message


def test_duplicate_candidate_in_one_call() -> None:
    cands = [make_candidate("A"), make_candidate("A")]
    orders, rej = _mgr().size_buys(DAYS[0], cands, 150_000, 150_000, [], {})
    assert [o.symbol for o in orders] == ["A"]
    assert [r.rule for r in rej] == ["already_held"]
    assert "本次" in rej[0].message


def test_already_held_checked_before_cooldown_after_paused() -> None:
    mgr = _mgr()
    mgr.note_exit("A", DAYS[0], ExitReason.STOP_LOSS)
    held = [make_position("A", 100, 1000)]
    _, rej = mgr.size_buys(DAYS[1], [make_candidate("A")], 150_000, 150_000, held, {"A": 10.0})
    assert [r.rule for r in rej] == ["already_held"]
    mgr.on_day_close(DAYS[1], 1.0)
    mgr.on_day_close(DAYS[2], 0.8)
    _, rej = mgr.size_buys(DAYS[3], [make_candidate("A")], 150_000, 150_000, held, {"A": 10.0})
    assert [r.rule for r in rej] == ["paused"]


def test_max_positions_limits_orders_in_one_call() -> None:
    cands = [make_candidate(f"S{i}", industry=f"I{i}") for i in range(12)]
    orders, rej = _mgr().size_buys(DAYS[0], cands, 1_000_000, 1_000_000, [], {})
    assert len(orders) == 10
    assert [r.rule for r in rej] == ["max_positions"] * 2


def test_extend_pause_validation() -> None:
    mgr = _mgr()
    with pytest.raises(ValueError):
        mgr.extend_pause(5)  # 未暂停
    mgr.on_day_close(DAYS[0], 1.0)
    mgr.on_day_close(DAYS[1], 0.8)
    with pytest.raises(ValueError):
        mgr.extend_pause(0)
    with pytest.raises(ValueError):
        mgr.extend_pause(-3)


def test_paused_message_has_pause_day_and_drawdown() -> None:
    mgr = _mgr()
    mgr.on_day_close(DAYS[0], 1.0)
    mgr.on_day_close(DAYS[1], 0.8)
    _, rej = mgr.size_buys(DAYS[2], [make_candidate("A")], 150_000, 150_000, [], {})
    assert str(DAYS[1]) in rej[0].message
    assert "20.00%" in rej[0].message


def test_pending_buys_exceeding_cash_never_produce_negative_shares() -> None:
    # 评审场景：待成交买单按今日收盘价重估后超过现金，可用现金为负
    pending = [Order("600009", Side.BUY, 500, DAYS[0], "x")]
    orders, rej = _mgr().size_buys(
        DAYS[1],
        [make_candidate("600001", close=10.0)],
        equity=100_000,
        cash=1000.0,
        positions=[],
        closes={"600009": 20.0, "600001": 10.0},
        pending_buys=pending,
    )
    assert orders == []
    assert [r.rule for r in rej] == ["no_cash"]
