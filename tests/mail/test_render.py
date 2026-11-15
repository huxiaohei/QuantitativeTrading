"""邮件渲染：标题、固定文案、转义、免责声明。"""

from collections.abc import Callable
from datetime import date

import pytest

from qt.mail.render import (
    BooksSummary,
    HoldingView,
    Mail,
    RiskEventView,
    SignalView,
    WeeklyView,
    render_abandon_reminder,
    render_admin_alert,
    render_buy_signals,
    render_risk_alert,
    render_sell_signals,
    render_weekly,
)

DAY = date(2026, 11, 16)
EMAIL = "u@example.com"
DISCLAIMER = (
    "以上内容是按你设定的规则计算的结果，不构成投资建议。历史回测不代表未来收益，盈亏自负。"
)


def _buy(
    symbol: str = "600001",
    name: str = "示例股份",
    reason: str = "近 20 日动量排名靠前，趋势向上",
    stop_price: float | None = 9.5,
    take_price: float | None = 12.0,
) -> SignalView:
    return SignalView(
        side="buy",
        symbol=symbol,
        name=name,
        shares=1000,
        ref_price=10.02,
        reason=reason,
        exit_reason=None,
        metrics={"动量": 0.35, "估值分位": 0.21},
        stop_price=stop_price,
        take_price=take_price,
        valid_until=date(2026, 11, 17),
    )


def _sell(exit_reason: str = "stop_loss", symbol: str = "600001") -> SignalView:
    return SignalView(
        side="sell",
        symbol=symbol,
        name="示例股份",
        shares=1000,
        ref_price=9.4,
        reason="收盘价跌破止损位",
        exit_reason=exit_reason,
        metrics={},
        stop_price=None,
        take_price=None,
        valid_until=date(2026, 11, 17),
    )


BOOKS = BooksSummary(
    manual_equity=102_345.6,
    manual_cash=50_000.0,
    manual_positions=3,
    manual_drawdown=0.032,
    system_equity=101_000.0,
    system_cash=40_000.0,
    system_positions=4,
)


def _both(m: Mail, *needles: str) -> None:
    for n in needles:
        assert n in m.html, n
        assert n in m.text, n


def _no_banned(m: Mail) -> None:
    for banned in ("建议买入", "推荐"):
        assert banned not in m.html
        assert banned not in m.text
        assert banned not in m.subject


def test_buy_signal_mail_content() -> None:
    m = render_buy_signals(DAY, [_buy()], [_buy("600002", "系统股份")], BOOKS, to_email=EMAIL)
    assert m.subject == "【买入信号】2026-11-16 1 只"
    _both(
        m,
        "手动账（请据此操作）",
        "系统账（自动执行，仅供对照）",
        "600001",
        "示例股份",
        "1000 股",
        "10.02",
        "今日收盘价，实际成交在下一个交易日，价格会有出入",
        "动量",
        "0.35",
        "近 20 日动量排名靠前",
        "止损位",
        "9.50",
        "止盈位",
        "12.00",
        "qt admin trade buy",
        "不构成投资建议",
    )
    assert m.html.index("手动账") < m.html.index("系统账")
    assert m.text.index("手动账") < m.text.index("系统账")
    assert "600002" in m.html and "600002" in m.text
    _no_banned(m)


def test_buy_signal_without_stop_or_take_price_shows_dash() -> None:
    m = render_buy_signals(DAY, [_buy(stop_price=None, take_price=None)], [], BOOKS, to_email=EMAIL)
    assert "止损位：—" in m.text
    assert "止盈位：—" in m.text


@pytest.mark.parametrize(
    "mail_factory",
    [
        lambda: render_buy_signals(DAY, [_buy()], [], BOOKS, to_email=EMAIL),
        lambda: render_sell_signals(DAY, [_sell()], [], BOOKS, reminder=False, to_email=EMAIL),
        lambda: render_sell_signals(DAY, [_sell()], [], BOOKS, reminder=True, to_email=EMAIL),
        lambda: render_abandon_reminder(DAY, [_buy()]),
        lambda: render_weekly(DAY, _weekly()),
        lambda: render_risk_alert(DAY, _risk("drawdown_warning"), BOOKS),
    ],
)
def test_signal_type_mails_end_with_disclaimer(mail_factory: Callable[[], Mail]) -> None:
    m = mail_factory()
    assert m.text.rstrip().endswith(DISCLAIMER)
    assert DISCLAIMER in m.html
    _no_banned(m)


def test_sell_reminder_subject_and_reason_label() -> None:
    first = render_sell_signals(DAY, [_sell()], [], BOOKS, reminder=False, to_email=EMAIL)
    assert first.subject == "【卖出信号】2026-11-16 1 只"
    again = render_sell_signals(DAY, [_sell()], [], BOOKS, reminder=True, to_email=EMAIL)
    assert again.subject.startswith("【卖出信号·再次提醒】")
    assert again.subject == "【卖出信号·再次提醒】2026-11-16 1 只"
    labels = {
        "stop_loss": "价格止损",
        "take_profit_fixed": "固定止盈",
        "take_profit_trailing": "移动止盈",
        "fundamental": "基本面止损",
        "time_stop": "时间止损",
        "delisted": "已退市",
    }
    for reason, label in labels.items():
        m = render_sell_signals(DAY, [_sell(reason)], [], BOOKS, reminder=False, to_email=EMAIL)
        _both(m, label)
    m = render_sell_signals(
        DAY, [_sell()], [_sell(symbol="600009")], BOOKS, reminder=False, to_email=EMAIL
    )
    _both(m, "手动账（请据此操作）", "系统账（自动执行，仅供对照）", "qt admin trade sell")
    assert m.html.index("手动账") < m.html.index("系统账")


def test_all_exit_reasons_have_labels() -> None:
    from qt.models import ExitReason

    for reason in ExitReason:
        m = render_sell_signals(
            DAY, [_sell(reason.value)], [], BOOKS, reminder=False, to_email=EMAIL
        )
        assert reason.value not in m.text


def _risk(kind: str) -> RiskEventView:
    return RiskEventView(kind=kind, drawdown=0.12, threshold=0.10, message="手动账回撤达到预警线")


def test_risk_alert_subjects() -> None:
    assert (
        render_risk_alert(DAY, _risk("drawdown_warning"), BOOKS).subject == "【风控提醒】回撤预警"
    )
    assert render_risk_alert(DAY, _risk("buy_paused"), BOOKS).subject == "【风控提醒】暂停买入"
    assert render_risk_alert(DAY, _risk("buy_resumed"), BOOKS).subject == "【风控提醒】恢复买入"
    m = render_risk_alert(DAY, _risk("drawdown_warning"), BOOKS)
    _both(m, "12.00%", "10.00%", "手动账回撤达到预警线")


def test_risk_alert_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="风控事件类型"):
        render_risk_alert(DAY, _risk("nonsense"), BOOKS)


def test_abandon_reminder() -> None:
    m = render_abandon_reminder(DAY, [_buy(), _buy("600002", "乙")])
    assert m.subject == "【信号已过期】2 只未录入"
    _both(m, "600001", "600002", "已过期", "2026-11-17")


def _weekly() -> WeeklyView:
    return WeeklyView(
        week_start=date(2026, 11, 9),
        manual_equity=102_345.6,
        system_equity=101_000.0,
        manual_week_return=0.012,
        system_week_return=-0.004,
        benchmark_week_return=0.008,
        holdings=[
            HoldingView(
                symbol="600001",
                name="示例股份",
                shares=1000,
                cost=9.8,
                last_price=10.5,
                pnl_pct=0.0714,
            )
        ],
        signal_lines=["11-10 买入信号 600001 示例股份：已录入"],
        trade_lines=["11-11 买入 600001 1000 股 @ 9.80"],
    )


def test_weekly_mail() -> None:
    m = render_weekly(date(2026, 11, 13), _weekly())
    assert m.subject == "【周报】2026-11-13"
    _both(m, "600001", "示例股份", "11-10 买入信号", "11-11 买入 600001")
    for line in (
        "手动账：净值 102,345.60，本周 +1.20%",
        "系统账：净值 101,000.00，本周 -0.40%",
        "基准本周：+0.80%",
        "浮动盈亏：+7.14%",
        "成本：9.80",
        "现价：10.50",
    ):
        assert line in m.text, line
    for fragment in ("+1.20%", "-0.40%", "+0.80%", "+7.14%", "102,345.60", "101,000.00"):
        assert fragment in m.html, fragment
    assert m.text.rstrip().endswith(DISCLAIMER)


def test_weekly_without_benchmark() -> None:
    from dataclasses import replace

    m = render_weekly(DAY, replace(_weekly(), benchmark_week_return=None))
    assert "基准" in m.text and "—" in m.text


def test_admin_alert_has_no_disclaimer() -> None:
    m = render_admin_alert(DAY, "数据更新失败", ["原因：超时", "重试 3 次"])
    assert m.subject == "【系统异常】数据更新失败"
    assert "不构成投资建议" not in m.html and "不构成投资建议" not in m.text
    _both(m, "原因：超时", "重试 3 次", "2026-11-16")
    m2 = render_admin_alert(None, "任务超时", ["x"])
    assert m2.subject == "【系统异常】任务超时"


def test_html_escapes_user_derived_strings() -> None:
    evil = '<script>alert("x")</script>'
    m = render_buy_signals(DAY, [_buy(name=evil, reason=evil)], [], BOOKS, to_email=EMAIL)
    assert evil not in m.html
    assert "&lt;script&gt;" in m.html
    assert evil in m.text  # 纯文本不转义
    a = render_admin_alert(DAY, evil, [evil])
    assert evil not in a.html
    w = render_weekly(DAY, _weekly())
    assert w.html  # smoke
    risk = render_risk_alert(DAY, RiskEventView("buy_paused", 0.2, None, evil), BOOKS)
    assert evil not in risk.html
    ab = render_abandon_reminder(DAY, [_buy(name=evil)])
    assert evil not in ab.html


def test_empty_manual_section_still_renders() -> None:
    m = render_buy_signals(DAY, [], [_buy()], BOOKS, to_email=EMAIL)
    assert "手动账（请据此操作）" in m.text
    assert "（无）" in m.text


def test_entry_command_is_exact_cli_shape() -> None:
    buy = render_buy_signals(DAY, [_buy()], [_buy("600002")], BOOKS, to_email=EMAIL)
    cmd = "qt admin trade buy --email u@example.com 600001 --price <实际成交价> --shares 1000"
    assert cmd in buy.text
    assert cmd.replace("<", "&lt;").replace(">", "&gt;") in buy.html
    assert buy.text.count("qt admin trade") == 1  # 系统账不带录入命令
    sell = render_sell_signals(DAY, [_sell()], [], BOOKS, reminder=False, to_email=EMAIL)
    sell_cmd = "qt admin trade sell --email u@example.com 600001 --price <实际成交价> --shares 1000"
    assert sell_cmd in sell.text


def test_entry_command_quotes_shell_metacharacters() -> None:
    evil = "a b;rm -rf x@example.com"
    m = render_buy_signals(DAY, [_buy()], [], BOOKS, to_email=evil)
    cmd = (
        "qt admin trade buy --email 'a b;rm -rf x@example.com' 600001 "
        "--price <实际成交价> --shares 1000"
    )
    assert cmd in m.text
    assert cmd.replace("<", "&lt;").replace(">", "&gt;").replace("'", "&#39;") in m.html
