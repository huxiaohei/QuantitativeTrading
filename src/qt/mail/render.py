"""邮件渲染：由展示用数据类生成主题、HTML 与纯文本。

文案用"信号""提示""按你设定的规则计算"；信号类邮件底部带固定风险提示。
HTML 模板开启自动转义；纯文本与 HTML 共用同一份字段列表，保证内容一致。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from functools import cache
from shlex import quote

from jinja2 import Environment, PackageLoader

DISCLAIMER = (
    "以上内容是按你设定的规则计算的结果，不构成投资建议。历史回测不代表未来收益，盈亏自负。"
)
MANUAL_HEADING = "手动账（请据此操作）"
SYSTEM_HEADING = "系统账（自动执行，仅供对照）"
_PRICE_NOTE = "今日收盘价，实际成交在下一个交易日，价格会有出入"
_NONE_TEXT = "（无）"
_DASH = "—"

EXIT_REASON_LABELS: Mapping[str, str] = {
    "stop_loss": "价格止损",
    "take_profit_fixed": "固定止盈",
    "take_profit_trailing": "移动止盈",
    "fundamental": "基本面止损",
    "time_stop": "时间止损",
    "delisted": "已退市",
}
_RISK_TITLES: Mapping[str, str] = {
    "drawdown_warning": "回撤预警",
    "buy_paused": "暂停买入",
    "buy_resumed": "恢复买入",
}


@dataclass(frozen=True)
class Mail:
    subject: str
    html: str
    text: str


@dataclass(frozen=True)
class SignalView:
    """一条信号的展示字段。side 为 buy 或 sell；exit_reason 取 ExitReason 的值（卖出时用）。"""

    side: str
    symbol: str
    name: str
    shares: int
    ref_price: float
    reason: str
    exit_reason: str | None
    metrics: Mapping[str, float]
    stop_price: float | None
    take_price: float | None
    valid_until: date | None


@dataclass(frozen=True)
class BooksSummary:
    """两本账的概况。回撤为小数（0.032 表示 3.2%）。"""

    manual_equity: float
    manual_cash: float
    manual_positions: int
    manual_drawdown: float
    system_equity: float
    system_cash: float
    system_positions: int


@dataclass(frozen=True)
class RiskEventView:
    """风控事件。kind：drawdown_warning、buy_paused、buy_resumed；比例为小数。"""

    kind: str
    drawdown: float
    threshold: float | None
    message: str


@dataclass(frozen=True)
class HoldingView:
    """周报里的一只持仓；pnl_pct 为小数。成本为 0（无法计算）时 cost、pnl_pct 为 None，显示"—"。"""

    symbol: str
    name: str
    shares: int
    cost: float | None
    last_price: float
    pnl_pct: float | None


@dataclass(frozen=True)
class WeeklyView:
    """周报内容；收益率为小数；信号与成交行由调用方预先格式化。"""

    week_start: date
    manual_equity: float
    system_equity: float
    manual_week_return: float
    system_week_return: float
    benchmark_week_return: float | None
    holdings: Sequence[HoldingView]
    signal_lines: Sequence[str]
    trade_lines: Sequence[str]


@dataclass(frozen=True)
class _Card:
    """一只股票的信号卡片：标题加若干（标签，内容）。"""

    title: str
    fields: Sequence[tuple[str, str]]


@dataclass(frozen=True)
class _Section:
    heading: str
    cards: Sequence[_Card]
    empty_text: str = _NONE_TEXT


@cache
def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("qt.mail", "templates"),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _price(value: float | None) -> str:
    return _DASH if value is None else f"{value:.2f}"


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _signed_pct(value: float | None) -> str:
    return _DASH if value is None else f"{value * 100:+.2f}%"


def _command(side: str, v: SignalView, to_email: str) -> str:
    head = f"qt admin trade {side} --email {quote(to_email)} {quote(v.symbol)}"
    return f"{head} --price <实际成交价> --shares {v.shares}"


def _buy_card(v: SignalView, to_email: str | None) -> _Card:
    metrics = "；".join(f"{k} {val:.2f}" for k, val in v.metrics.items()) or _DASH
    fields = [
        ("数量", f"{v.shares} 股"),
        ("参考价", f"{v.ref_price:.2f}（{_PRICE_NOTE}）"),
        ("指标", metrics),
        ("买入理由", v.reason),
        ("止损位", _price(v.stop_price)),
        ("止盈位", _price(v.take_price)),
    ]
    if v.valid_until is not None:
        fields.append(("有效至", v.valid_until.isoformat()))
    if to_email is not None:
        fields.append(("录入命令", _command("buy", v, to_email)))
    return _Card(f"{v.symbol} {v.name}", fields)


def _exit_label(v: SignalView) -> str:
    if v.exit_reason is None:
        return _DASH
    return EXIT_REASON_LABELS.get(v.exit_reason, v.exit_reason)


def _sell_card(v: SignalView, to_email: str | None) -> _Card:
    fields = [
        ("数量", f"{v.shares} 股"),
        ("参考价", f"{v.ref_price:.2f}（{_PRICE_NOTE}）"),
        ("卖出原因", _exit_label(v)),
        ("触发说明", v.reason),
    ]
    if v.valid_until is not None:
        fields.append(("有效至", v.valid_until.isoformat()))
    if to_email is not None:
        fields.append(("录入命令", _command("sell", v, to_email)))
    return _Card(f"{v.symbol} {v.name}", fields)


def _expired_card(v: SignalView) -> _Card:
    valid = v.valid_until.isoformat() if v.valid_until is not None else _DASH
    return _Card(
        f"{v.symbol} {v.name}",
        [
            ("数量", f"{v.shares} 股"),
            ("参考价", f"{v.ref_price:.2f}"),
            ("有效至", f"{valid}（已过期，未录入）"),
        ],
    )


def _books_lines(b: BooksSummary) -> list[tuple[str, str]]:
    return [
        (
            "手动账",
            f"净值 {_money(b.manual_equity)}，现金 {_money(b.manual_cash)}，"
            f"持仓 {b.manual_positions} 只，当前回撤 {_pct(b.manual_drawdown)}",
        ),
        (
            "系统账",
            f"净值 {_money(b.system_equity)}，现金 {_money(b.system_cash)}，"
            f"持仓 {b.system_positions} 只",
        ),
    ]


def _text_cards(cards: Sequence[_Card], empty_text: str) -> list[str]:
    if not cards:
        return [empty_text]
    lines: list[str] = []
    for card in cards:
        lines.append(card.title)
        lines.extend(f"  {label}：{value}" for label, value in card.fields)
        lines.append("")
    if lines[-1] == "":
        lines.pop()
    return lines


def _compose(
    subject: str,
    template: str,
    intro: str,
    sections: Sequence[_Section],
    facts: Sequence[tuple[str, str]],
    disclaimer: bool,
    extra: Mapping[str, object] | None = None,
    extra_text: Sequence[str] = (),
) -> Mail:
    """统一拼装：HTML 走模板，纯文本在此生成。"""
    html = (
        _environment()
        .get_template(template)
        .render(
            subject=subject,
            intro=intro,
            sections=sections,
            facts=facts,
            disclaimer=DISCLAIMER if disclaimer else None,
            **(extra or {}),
        )
    )
    lines = [subject, "", intro, ""]
    for sec in sections:
        lines.append(f"== {sec.heading} ==")
        lines.extend(_text_cards(sec.cards, sec.empty_text))
        lines.append("")
    lines.extend(extra_text)
    if facts:
        lines.extend(f"{label}：{value}" for label, value in facts)
        lines.append("")
    if disclaimer:
        lines.append(DISCLAIMER)
    return Mail(subject, html, "\n".join(lines).rstrip("\n") + "\n")


def render_buy_signals(
    day: date,
    manual: Sequence[SignalView],
    system: Sequence[SignalView],
    books: BooksSummary,
    to_email: str,
) -> Mail:
    subject = f"【买入信号】{day} {len(manual)} 只"
    sections = [
        _Section(MANUAL_HEADING, [_buy_card(v, to_email) for v in manual]),
        _Section(SYSTEM_HEADING, [_buy_card(v, None) for v in system]),
    ]
    intro = f"按你设定的规则计算，{day} 收盘后产生以下买入信号。"
    return _compose(subject, "buy_signal.html.j2", intro, sections, _books_lines(books), True)


def render_sell_signals(
    day: date,
    manual: Sequence[SignalView],
    system: Sequence[SignalView],
    books: BooksSummary,
    reminder: bool,
    to_email: str,
) -> Mail:
    prefix = "【卖出信号·再次提醒】" if reminder else "【卖出信号】"
    subject = f"{prefix}{day} {len(manual)} 只"
    sections = [
        _Section(MANUAL_HEADING, [_sell_card(v, to_email) for v in manual]),
        _Section(SYSTEM_HEADING, [_sell_card(v, None) for v in system]),
    ]
    intro = f"按你设定的规则计算，{day} 收盘后以下持仓触发卖出条件。"
    if reminder:
        intro += "这些信号此前已通知过，目前仍未录入卖出。"
    return _compose_sell(subject, intro, sections, books)


def _compose_sell(
    subject: str, intro: str, sections: Sequence[_Section], books: BooksSummary
) -> Mail:
    return _compose(subject, "sell_signal.html.j2", intro, sections, _books_lines(books), True)


def render_risk_alert(day: date, event: RiskEventView, books: BooksSummary) -> Mail:
    title = _RISK_TITLES.get(event.kind)
    if title is None:
        raise ValueError(f"未知的风控事件类型：{event.kind}")
    subject = f"【风控提醒】{title}"
    facts = [
        ("日期", day.isoformat()),
        ("当前回撤", _pct(event.drawdown)),
        ("触发线", _DASH if event.threshold is None else _pct(event.threshold)),
        ("说明", event.message),
        *_books_lines(books),
    ]
    intro = "按你设定的规则计算，手动账触发了风控提示。"
    return _compose(subject, "risk_alert.html.j2", intro, [], facts, True)


def render_abandon_reminder(day: date, signals: Sequence[SignalView]) -> Mail:
    subject = f"【信号已过期】{len(signals)} 只未录入"
    sections = [_Section("已过期未录入的买入信号", [_expired_card(v) for v in signals])]
    intro = f"截至 {day}，以下买入信号已过有效期且没有录入成交，此提示只发一次。"
    return _compose(subject, "abandon_reminder.html.j2", intro, sections, [], True)


def render_weekly(week_end: date, report: WeeklyView) -> Mail:
    subject = f"【周报】{week_end}"
    facts = [
        (
            "手动账",
            f"净值 {_money(report.manual_equity)}，本周 {_signed_pct(report.manual_week_return)}",
        ),
        (
            "系统账",
            f"净值 {_money(report.system_equity)}，本周 {_signed_pct(report.system_week_return)}",
        ),
        ("基准本周", _signed_pct(report.benchmark_week_return)),
    ]
    holdings = [
        _Card(
            f"{h.symbol} {h.name}",
            [
                ("数量", f"{h.shares} 股"),
                ("成本", _price(h.cost)),
                ("现价", f"{h.last_price:.2f}"),
                ("浮动盈亏", _signed_pct(h.pnl_pct)),
            ],
        )
        for h in report.holdings
    ]
    sections = [_Section("手动账持仓", holdings)]
    extra_text = ["== 本周信号 =="]
    extra_text += list(report.signal_lines) or [_NONE_TEXT]
    extra_text += ["", "== 本周成交 =="]
    extra_text += list(report.trade_lines) or [_NONE_TEXT]
    extra_text.append("")
    intro = f"本周（{report.week_start} 至 {week_end}）按你设定的规则计算的汇总。"
    return _compose(
        subject,
        "weekly.html.j2",
        intro,
        sections,
        facts,
        True,
        extra={
            "signal_lines": list(report.signal_lines),
            "trade_lines": list(report.trade_lines),
            "none_text": _NONE_TEXT,
        },
        extra_text=extra_text,
    )


def render_admin_alert(day: date | None, title: str, lines: Sequence[str]) -> Mail:
    subject = f"【系统异常】{title}"
    parts = [subject, ""]
    if day is not None:
        parts += [f"交易日：{day}"]
    parts += list(lines)
    text = "\n".join(parts) + "\n"
    html = (
        _environment()
        .get_template("admin_alert.html.j2")
        .render(subject=subject, day=day, lines=list(lines), disclaimer=None)
    )
    return Mail(subject, html, text)
