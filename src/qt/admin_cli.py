"""管理命令（qt admin ...）：Web 管理台上线之前，由管理员在命令行管理使用者、订阅与手动账。

本模块只是服务层之上的一层薄壳：按邮箱找到使用者，再带着 user_id 调用 service / repo 函数；
跨使用者的查询（按邮箱找人、列出使用者与每日运行）一律走 qt.db.admin_repo，这里不写 SQL。
每条命令在一个事务里完成：成功提交，出错回滚。业务错误（ValidationError、NotFoundError）打印中文
消息并以 1 退出；账本不一致（LedgerInconsistentError）以 2 退出；其他意外异常原样抛出。
"""

import json
import unicodedata
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Literal, cast
from zoneinfo import ZoneInfo

import typer
from sqlalchemy.orm import Session

from qt.backtest.state import load_state
from qt.calendar import TradingCalendar
from qt.data.schema import SchemaError
from qt.data.update import DataIntegrityError, format_bulk
from qt.db import admin_repo, repo
from qt.db.engine import session_scope
from qt.db.models import Book, LedgerEntry, Signal, Subscription, User
from qt.features import build_panel
from qt.mail.outbox import requeue_failed
from qt.models import Position, Side
from qt.runtime import (
    DEFAULT_CONFIG,
    DEFAULT_DATA_DIR,
    ConfigOption,
    DataDirOption,
    Runtime,
    SetupError,
)
from qt.runtime import now as runtime_now
from qt.service import accounts, manual_ops, rules
from qt.service.actor import CLI
from qt.service.errors import LedgerInconsistentError, NotFoundError, ValidationError
from qt.service.manual_book import CorporateAction
from qt.service.market import StoreMarket
from qt.service.signals import SignalStatus

admin_app = typer.Typer(help="管理命令：使用者、订阅、手动账与运行记录", no_args_is_help=True)
user_app = typer.Typer(help="使用者", no_args_is_help=True)
sub_app = typer.Typer(help="订阅", no_args_is_help=True)
trade_app = typer.Typer(help="录入成交", no_args_is_help=True)
holding_app = typer.Typer(help="录入已有持仓", no_args_is_help=True)
cash_app = typer.Typer(help="入金与出金", no_args_is_help=True)
entry_app = typer.Typer(help="流水更正", no_args_is_help=True)
book_app = typer.Typer(help="账本", no_args_is_help=True)
outbox_app = typer.Typer(help="发件箱", no_args_is_help=True)
admin_app.add_typer(user_app, name="user")
admin_app.add_typer(sub_app, name="sub")
admin_app.add_typer(trade_app, name="trade")
admin_app.add_typer(holding_app, name="holding")
admin_app.add_typer(cash_app, name="cash")
admin_app.add_typer(entry_app, name="entry")
admin_app.add_typer(book_app, name="book")
admin_app.add_typer(outbox_app, name="outbox")

_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = time(15, 0)
_DAY_DEFAULT_HELP = (
    "默认最近一个已收盘的交易日（今天是交易日且已过 15:00 为今天，否则为上一个交易日）"
)
_EARLIEST = date(1990, 1, 1)
_RECENT_ENTRIES = 20  # book show 显示的最近流水条数
_FAR_FUTURE = date.max

Email = Annotated[str, typer.Option("--email", help="使用者邮箱")]
Symbol = Annotated[str, typer.Argument(help="股票代码，如 600001")]
SetOption = Annotated[
    list[str] | None,
    typer.Option("--set", help="参数覆盖，形如 strategy.stop_loss=0.10，可重复"),
]
BookOption = Annotated[
    str, typer.Option("--book", help="账本：manual（手动账）或 system（系统账）")
]

_PARAM_LABELS: dict[str, str] = {p.key: p.label for p in rules.PARAMS}
_SUB_STATUS = {"active": "生效中", "paused": "已暂停", "closed": "已关闭"}
_USER_STATUS = {"active": "正常", "disabled": "停用"}
_KIND_LABELS = {
    "buy": "买入",
    "sell": "卖出",
    "transfer_in": "转入持仓",
    "deposit": "入金",
    "withdraw": "出金",
    "void": "作废流水",
}
_SIGNAL_STATUS_LABELS = {
    "created": "已生成",
    "notified": "已通知",
    "executed": "已执行",
    "abandoned": "已放弃",
    "voided": "已作废",
    "unnotified": "未通知（补跑）",
}
_SIDE_LABELS = {"buy": "买入信号", "sell": "卖出信号"}
_EXIT_LABELS = {
    "take_profit_fixed": "固定止盈",
    "take_profit_trailing": "移动止盈",
    "stop_loss": "止损",
    "time_stop": "持有期满",
    "fundamental": "基本面",
    "delisted": "退市",
    "manual": "自主卖出",
}
_FOLLOWUP_LABELS = {"exit": "离场后", "abandoned": "放弃买入后", "rejected": "被规则否决后"}
_OUTBOX_STATUS_LABELS = {"pending": "待发", "sent": "已发", "failed": "发送失败"}
_OUTBOX_ERROR_CHARS = 80  # outbox list 里错误信息最多显示的字数
_RUN_STATUS_LABELS = {
    "running": "运行中（或中途中断）",
    "done": "完成",
    "done_with_errors": "完成，但有使用者处理出错",
    "data_failed": "数据异常，本日未出信号",
}


def now() -> datetime:
    """当前时间（上海时区）；测试里替换。"""
    return runtime_now()


def money(value: float) -> str:
    """金额：两位小数，千分位。"""
    return f"{value:,.2f}"


def pct(value: float) -> str:
    """百分比：两位小数。"""
    return f"{value * 100:.2f}%"


# ---------- 运行环境 ----------


def _market(rt: Runtime, calendar: TradingCalendar, today: date) -> StoreMarket:
    """行情：以不晚于今天的最近一个交易日为指标面板的日期。"""
    days = calendar.between(_EARLIEST, today)
    if not days:
        raise ValidationError(f"交易日历里没有 {today} 及以前的交易日")
    panel = build_panel(rt.store(), calendar, rt.cfg(), days[-1], days[-1])
    return StoreMarket(rt.store(), panel, rt.cfg())


# 命令的输出先攒在这里，事务提交成功之后才打印（出错回滚时丢弃）
_output: ContextVar[list[str] | None] = ContextVar("admin_output", default=None)


def _emit(text: str = "") -> None:
    lines = _output.get()
    if lines is None:
        typer.echo(text)
    else:
        lines.append(text)


@admin_app.callback()
def _setup(
    ctx: typer.Context,
    config: ConfigOption = DEFAULT_CONFIG,
    data_dir: DataDirOption = DEFAULT_DATA_DIR,
) -> None:
    """管理命令。数据库地址取自环境变量 DATABASE_URL。"""
    runtime = Runtime(config, data_dir)
    ctx.obj = runtime
    ctx.call_on_close(runtime.close)


@contextmanager
def _unit(ctx: typer.Context) -> Generator[tuple[Runtime, Session]]:
    """一个命令的工作单元：一个事务，成功提交后才打印输出，出错回滚；错误按约定转成退出码。"""
    runtime = ctx.find_object(Runtime)
    if runtime is None:
        raise RuntimeError("管理命令的运行环境没有初始化")
    lines: list[str] = []
    token = _output.set(lines)
    try:
        with session_scope(runtime.engine()) as s:
            yield runtime, s
        for line in lines:  # 事务已提交
            typer.echo(line)
    except (ValidationError, NotFoundError, SetupError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    except (DataIntegrityError, SchemaError) as exc:
        typer.echo(f"数据目录有问题：{exc}")
        raise typer.Exit(code=1) from exc
    except LedgerInconsistentError as exc:
        typer.echo(str(exc))
        typer.echo("该使用者的账本需要修复，本次操作没有保存；请先检查并更正流水，再继续录入。")
        raise typer.Exit(code=2) from exc
    finally:
        _output.reset(token)


# ---------- 输入解析 ----------


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"日期格式应为 YYYY-MM-DD，收到 {text!r}") from exc


def _parse_overrides(items: Sequence[str] | None) -> dict[str, object]:
    """把 路径=值 的列表解析成覆盖字典；值按 JSON 解析（数字、true/false），否则当作字符串。"""
    result: dict[str, object] = {}
    for item in items or []:
        path, eq, raw = item.partition("=")
        path = path.strip()
        if not eq or not path or not raw.strip():
            raise ValidationError(
                f"参数格式应为 路径=值，如 strategy.stop_loss=0.10，收到 {item!r}"
            )
        if path in result:
            raise ValidationError(f"参数 {path} 重复出现")
        try:
            value = cast(object, json.loads(raw))
        except ValueError:
            value = raw.strip()
        result[path] = value
    return result


def _book_kind(book: str) -> Literal["manual", "system"]:
    if book == "manual":
        return "manual"
    if book == "system":
        return "system"
    raise ValidationError(f"账本只能是 manual（手动账）或 system（系统账），收到 {book!r}")


def _today() -> date:
    return now().date()


def _closed_day(moment: datetime, calendar: TradingCalendar) -> date:
    """最近一个已收盘的交易日：今天是交易日且上海时间已到 15:00 为今天，否则为上一个交易日。"""
    local = moment.astimezone(_MARKET_TZ)
    today = local.date()
    if calendar.is_trading_day(today) and local.time() >= _MARKET_CLOSE:
        return today
    earlier = calendar.between(_EARLIEST, today - timedelta(days=1))
    if not earlier:
        raise ValidationError("交易日历里没有已收盘的交易日，请用 --day 指定日期")
    return earlier[-1]


def _day_or_default(day: str | None, calendar: TradingCalendar) -> date:
    return _closed_day(now(), calendar) if day is None else _parse_day(day)


# ---------- 查找使用者与订阅 ----------


def _user(s: Session, email: str) -> User:
    user = admin_repo.find_user_by_email(s, email)
    if user is None:
        raise NotFoundError(f"没有这个使用者：{email}")
    return user


def _open_sub(s: Session, user: User) -> Subscription:
    sub = repo.get_open_subscription(s, user.id)
    if sub is None:
        raise NotFoundError(f"{user.email} 没有未关闭的订阅")
    return sub


def _shown_sub(s: Session, user: User) -> Subscription:
    """查看用：优先未关闭的订阅，否则最近一个（已关闭的订阅仍可查看）。"""
    sub = rules.shown_subscription(s, user.id)
    if sub is None:
        raise NotFoundError(f"{user.email} 还没有订阅")
    return sub


# ---------- 文本排版 ----------


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int, right: bool = False) -> str:
    fill = " " * max(0, width - _width(text))
    return fill + text if right else text + fill


def _table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[int] = ()
) -> None:
    """按显示宽度对齐的简单表格（中文字符占两列）。"""
    widths = [max(_width(r[i]) for r in [headers, *rows]) for i in range(len(headers))]

    def line(cells: Sequence[str]) -> str:
        parts = [_pad(c, widths[i], i in right) for i, c in enumerate(cells)]
        return ("  ".join(parts)).rstrip()

    _emit(line(headers))
    for row in rows:
        _emit(line(row))


def _json_text(value: object) -> str:
    """参数值的显示：字符串原样，其余按 JSON（true/false、数字）。"""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _origin_label(origin: str | None, side: str = "买入") -> str:
    return f"按信号{side}" if origin == "signal" else f"自主{side}"


# ---------- 使用者 ----------


@user_app.command("add")
def user_add(
    ctx: typer.Context,
    email: Email,
    admin: Annotated[bool, typer.Option("--admin", help="同时设为管理员")] = False,
) -> None:
    """创建使用者。"""
    with _unit(ctx) as (_cli, s):
        user = accounts.create_user(s, email, is_admin=admin, actor=CLI)
        _emit(f"已创建使用者 {user.email}{'（管理员）' if user.is_admin else ''}")


@user_app.command("list")
def user_list(ctx: typer.Context) -> None:
    """列出使用者：邮箱、状态、周报开关、订阅情况。"""
    with _unit(ctx) as (_cli, s):
        users = admin_repo.list_users(s)
        if not users:
            _emit("还没有使用者")
            return
        rows: list[list[str]] = []
        for u in users:
            sub = repo.get_open_subscription(s, u.id)
            rows.append(
                [
                    u.email,
                    "管理员" if u.is_admin else "",
                    _USER_STATUS.get(u.status, u.status),
                    "周报：开" if u.weekly_mail else "周报：关",
                    "订阅：" + (_SUB_STATUS.get(sub.status, sub.status) if sub else "无订阅"),
                ]
            )
        _table(["邮箱", "角色", "状态", "周报", "订阅"], rows)


@user_app.command("weekly")
def user_weekly(
    ctx: typer.Context,
    email: Email,
    on: Annotated[bool | None, typer.Option("--on/--off", help="开启或关闭周报邮件")] = None,
) -> None:
    """开关使用者的周报邮件。"""
    with _unit(ctx) as (_cli, s):
        if on is None:
            raise ValidationError("请用 --on 或 --off 指定周报开关")
        user = _user(s, email)
        accounts.set_weekly_mail(s, user.id, on, actor=CLI)
        _emit(f"已{'开启' if on else '关闭'} {user.email} 的周报邮件")


# ---------- 订阅 ----------


@sub_app.command("create")
def sub_create(
    ctx: typer.Context,
    email: Email,
    cash: Annotated[float, typer.Option("--cash", help="本金（元）")],
    set_: SetOption = None,
    start: Annotated[
        str | None,
        typer.Option(
            "--start",
            help="起始日 YYYY-MM-DD；缺省为今天之后第一个交易日。可以选过去的交易日"
            "（最早为 service.backdate_limit_days 个交易日之前），运行时逐日补算，"
            "只给最新一天发邮件",
        ),
    ] = None,
) -> None:
    """创建订阅（同时建立系统账与手动账），起始日缺省为下一个交易日。"""
    with _unit(ctx) as (cli, s):
        overrides = _parse_overrides(set_)
        user = _user(s, email)
        calendar = cli.calendar()
        upcoming = calendar.between(_today() + timedelta(days=1), _FAR_FUTURE)
        if not upcoming:
            raise ValidationError("交易日历还没有覆盖到下一个交易日，请先运行 qt data update")
        first = rules.resolve_start_day(
            start, calendar, upcoming[0], cli.cfg().service.backdate_limit_days, today=_today()
        )
        sub = accounts.create_subscription(s, user.id, cli.cfg(), cash, first, overrides, actor=CLI)
        label = "下一个交易日" if first == upcoming[0] else "自选起始日，运行时会补算"
        _emit(f"已为 {user.email} 创建订阅 {sub.id}：本金 {money(cash)}，起始日 {first}（{label}）")
        if overrides:
            _emit("参数覆盖：" + "，".join(f"{k}={_json_text(v)}" for k, v in overrides.items()))


@sub_app.command("set")
def sub_set(ctx: typer.Context, email: Email, set_: SetOption = None) -> None:
    """修改订阅参数；账本与历史不动，从下一次运行处理的交易日起生效。"""
    with _unit(ctx) as (cli, s):
        overrides = _parse_overrides(set_)
        if not overrides:
            raise ValidationError("请用 --set 路径=值 指定要修改的参数")
        user = _user(s, email)
        sub = _open_sub(s, user)
        cfg = cli.cfg()
        effective = rules.next_effective_day(s, sub, cli.calendar())
        accounts.update_params(s, user.id, sub.id, cfg, overrides, effective, actor=CLI)
        _emit(
            f"已修改 {user.email} 的参数："
            + "，".join(f"{k}={_json_text(v)}" for k, v in overrides.items())
        )
        _emit(
            f"从下一次运行处理的交易日 {effective} 起生效（该日还没有运行过，运行时即使用新参数）；"
            "已推进过的日子、已有账本与历史不变"
        )


@sub_app.command("show")
def sub_show(ctx: typer.Context, email: Email) -> None:
    """显示订阅的当前参数，与默认值不同的标出。"""
    with _unit(ctx) as (cli, s):
        user = _user(s, email)
        sub = _shown_sub(s, user)
        site = cli.cfg()
        defaults = {
            "strategy": site.strategy.model_dump(mode="json"),
            "risk": site.risk.model_dump(mode="json"),
        }
        params = sub.params
        _emit(
            f"{user.email} 的订阅 {sub.id}：{_SUB_STATUS.get(sub.status, sub.status)}，"
            f"本金 {money(sub.initial_cash)}，起始日 {sub.start_day}"
        )
        changes = repo.list_param_changes(s, user.id, sub.id)
        if changes:
            _emit(f"参数修改过 {len(changes)} 次，最近一次生效日 {changes[-1].effective_day}")
        different = 0
        for path, label in _PARAM_LABELS.items():
            section, _, field = path.partition(".")
            default = defaults[section][field]
            value = default if path == "strategy.lookback_days" else params[section][field]
            line = f"  {_pad(path, 34)}{label}：{_json_text(value)}"
            if value != default:
                different += 1
                line += f" ← 与默认值不同（默认 {_json_text(default)}）"
            _emit(line)
        _emit(f"共 {different} 项与默认值不同；以上为按你设定的规则计算时使用的参数")


@sub_app.command("pause")
def sub_pause(ctx: typer.Context, email: Email) -> None:
    """暂停订阅（不再出新信号）。"""
    with _unit(ctx) as (_cli, s):
        user = _user(s, email)
        accounts.pause_subscription(s, user.id, _open_sub(s, user).id, actor=CLI)
        _emit(f"已暂停 {user.email} 的订阅")


@sub_app.command("resume")
def sub_resume(ctx: typer.Context, email: Email) -> None:
    """恢复已暂停的订阅。"""
    with _unit(ctx) as (_cli, s):
        user = _user(s, email)
        accounts.resume_subscription(s, user.id, _open_sub(s, user).id, actor=CLI)
        _emit(f"已恢复 {user.email} 的订阅")


@sub_app.command("close")
def sub_close(ctx: typer.Context, email: Email) -> None:
    """关闭订阅（不可恢复；账本与历史保留，仍可查看）。"""
    with _unit(ctx) as (_cli, s):
        user = _user(s, email)
        accounts.close_subscription(s, user.id, _open_sub(s, user).id, actor=CLI)
        _emit(f"已关闭 {user.email} 的订阅，账本与历史保留")


# ---------- 录入 ----------


def _record_trade(
    ctx: typer.Context,
    side: Side,
    email: str,
    symbol: str,
    price: float,
    shares: float,
    day: str | None,
    fees: float | None,
) -> None:
    with _unit(ctx) as (cli, s):
        user = _user(s, email)
        sub = _open_sub(s, user)
        calendar = cli.calendar()
        today = _today()
        trade_day = _day_or_default(day, calendar)
        market = _market(cli, calendar, today)
        code = symbol.strip()
        entry = manual_ops.record_trade(
            s,
            user.id,
            sub.id,
            market,
            calendar,
            cli.cfg(),
            today,
            side,
            code,
            trade_day,
            price,
            shares,
            fees,
            actor=CLI,
        )
        label = "买入" if side is Side.BUY else "卖出"
        estimated = "（估算）" if entry.fees_estimated else ""
        _emit(
            f"已录入{label}：{code} {shares:,.0f} 股 @ {money(price)}，日期 {trade_day}，"
            f"费用 {money(entry.fees)}{estimated}，{_origin_label(entry.origin, label)}"
            f"（流水 {entry.id}）"
        )
        if entry.signal_id is not None:
            _emit(f"关联信号 #{entry.signal_id}")
        if entry.note:
            _emit(f"提示：{entry.note}")


@trade_app.command("buy")
def trade_buy(
    ctx: typer.Context,
    symbol: Symbol,
    email: Email,
    price: Annotated[float, typer.Option("--price", help="实际成交价")],
    shares: Annotated[float, typer.Option("--shares", help="股数")],
    day: Annotated[
        str | None,
        typer.Option(
            "--day",
            help="成交日 YYYY-MM-DD，" + _DAY_DEFAULT_HELP,
        ),
    ] = None,
    fees: Annotated[float | None, typer.Option("--fees", help="实际费用，不填则按规则估算")] = None,
) -> None:
    """录入一笔买入成交。"""
    _record_trade(ctx, Side.BUY, email, symbol, price, shares, day, fees)


@trade_app.command("sell")
def trade_sell(
    ctx: typer.Context,
    symbol: Symbol,
    email: Email,
    price: Annotated[float, typer.Option("--price", help="实际成交价")],
    shares: Annotated[float, typer.Option("--shares", help="股数")],
    day: Annotated[
        str | None,
        typer.Option(
            "--day",
            help="成交日 YYYY-MM-DD，" + _DAY_DEFAULT_HELP,
        ),
    ] = None,
    fees: Annotated[float | None, typer.Option("--fees", help="实际费用，不填则按规则估算")] = None,
) -> None:
    """录入一笔卖出成交。"""
    _record_trade(ctx, Side.SELL, email, symbol, price, shares, day, fees)


@holding_app.command("add")
def holding_add(
    ctx: typer.Context,
    symbol: Symbol,
    email: Email,
    cost: Annotated[float, typer.Option("--cost", help="成本价")],
    shares: Annotated[float, typer.Option("--shares", help="股数")],
    bought: Annotated[str, typer.Option("--bought", help="买入日 YYYY-MM-DD")],
) -> None:
    """录入使用者已有的持仓（转入，记为自主买入，不扣现金）。"""
    with _unit(ctx) as (cli, s):
        user = _user(s, email)
        sub = _open_sub(s, user)
        calendar = cli.calendar()
        today = _today()
        market = _market(cli, calendar, today)
        code = symbol.strip()
        entry = manual_ops.record_holding(
            s,
            user.id,
            sub.id,
            market,
            calendar,
            cli.cfg(),
            today,
            code,
            _parse_day(bought),
            cost,
            shares,
            actor=CLI,
        )
        _emit(
            f"已录入已有持仓：{code} {shares:,.0f} 股，成本价 {money(cost)}，"
            f"买入日 {entry.trade_day}，从 {entry.effective_day} 起进入净值（自主买入）"
        )
        if entry.note:
            _emit(f"提示：{entry.note}")


def _record_cash(
    ctx: typer.Context,
    kind: Literal["deposit", "withdraw"],
    email: str,
    amount: float,
    day: str | None,
) -> None:
    with _unit(ctx) as (cli, s):
        user = _user(s, email)
        sub = _open_sub(s, user)
        calendar = cli.calendar()
        today = _today()
        cash_day = _day_or_default(day, calendar)
        market = _market(cli, calendar, today)
        entry = manual_ops.record_cash(
            s,
            user.id,
            sub.id,
            market,
            calendar,
            cli.cfg(),
            today,
            kind,
            cash_day,
            amount,
            actor=CLI,
        )
        label = "入金" if kind == "deposit" else "出金"
        _emit(f"已录入{label} {money(amount)}，日期 {cash_day}（流水 {entry.id}）")


@cash_app.command("deposit")
def cash_deposit(
    ctx: typer.Context,
    email: Email,
    amount: Annotated[float, typer.Option("--amount", help="金额（元）")],
    day: Annotated[
        str | None,
        typer.Option(
            "--day",
            help="日期 YYYY-MM-DD，" + _DAY_DEFAULT_HELP,
        ),
    ] = None,
) -> None:
    """录入入金。"""
    _record_cash(ctx, "deposit", email, amount, day)


@cash_app.command("withdraw")
def cash_withdraw(
    ctx: typer.Context,
    email: Email,
    amount: Annotated[float, typer.Option("--amount", help="金额（元）")],
    day: Annotated[
        str | None,
        typer.Option(
            "--day",
            help="日期 YYYY-MM-DD，" + _DAY_DEFAULT_HELP,
        ),
    ] = None,
) -> None:
    """录入出金。"""
    _record_cash(ctx, "withdraw", email, amount, day)


@entry_app.command("void")
def entry_void(
    ctx: typer.Context,
    email: Email,
    entry_id: Annotated[int, typer.Argument(metavar="ID", help="要作废的流水编号")],
) -> None:
    """作废一条流水（追加一条作废记录，原记录保留）。"""
    with _unit(ctx) as (cli, s):
        user = _user(s, email)
        sub = _open_sub(s, user)
        calendar = cli.calendar()
        today = _today()
        market = _market(cli, calendar, today)
        row = manual_ops.void_entry(
            s, user.id, sub.id, market, calendar, cli.cfg(), today, entry_id, actor=CLI
        )
        _emit(f"已作废流水 {entry_id}（新增作废记录 {row.id}）")


# ---------- 账本 ----------


def _entry_text(row: LedgerEntry, voided: set[int]) -> str:
    kind = _KIND_LABELS.get(row.kind, row.kind)
    head = f"#{row.id}  {row.trade_day}  {kind}"
    if row.kind in ("deposit", "withdraw"):
        body = f"金额 {money(row.price)}"
    elif row.kind == "void":
        body = f"#{row.voids_entry_id}  {row.symbol or ''}".rstrip()
    elif row.kind == "transfer_in":
        body = (
            f"{row.symbol}  {row.shares:,.0f} 股  成本价 {money(row.price)}"
            f"  从 {row.effective_day} 起进入净值"
        )
    else:
        estimated = "（估算）" if row.fees_estimated else ""
        body = (
            f"{row.symbol}  {row.shares:,.0f} 股 @ {money(row.price)}"
            f"  费用 {money(row.fees)}{estimated}  {'按信号' if row.origin == 'signal' else '自主'}"
        )
        if row.signal_id is not None:
            body += f"  信号 #{row.signal_id}"
    text = f"{head}  {body}"
    if row.id in voided:
        text += "  [已作废]"
    if row.note:
        text += f"  提示：{row.note}"
    return text


def _action_texts(action: CorporateAction) -> list[str]:
    texts: list[str] = []
    if action.cash > 0:
        texts.append(f"{action.day}  分红（推算）  {action.symbol}  现金 {money(action.cash)}")
    if action.bonus_shares > 0:
        texts.append(
            f"{action.day}  送转（推算）  {action.symbol}  送转 {action.bonus_shares:,.2f} 股"
        )
    return texts


def _recent_entries(rows: Sequence[LedgerEntry], actions: Sequence[CorporateAction]) -> list[str]:
    """最近的流水（含推算出的分红送转）：按日期排序，同日先实际流水后推算记录。"""
    voided = {r.voids_entry_id for r in rows if r.kind == "void" and r.voids_entry_id is not None}
    items: list[tuple[date, int, int, str]] = [
        (r.trade_day, 0, i, _entry_text(r, voided)) for i, r in enumerate(rows)
    ]
    for j, action in enumerate(actions):
        items.extend((action.day, 1, j * 2 + k, t) for k, t in enumerate(_action_texts(action)))
    items.sort(key=lambda it: it[:3])
    return [text for *_, text in items[-_RECENT_ENTRIES:]]


def _show_header(
    user: User, sub: Subscription, kind: str, through: date, equity: float, cash: float, nav: float
) -> None:
    name = "手动账" if kind == "manual" else "系统账"
    _emit(
        f"{user.email} · 订阅 {sub.id}（{_SUB_STATUS.get(sub.status, sub.status)}）· {name}"
        f" · 截至 {through}（按你设定的规则计算，仅供提示）"
    )
    _emit(f"净值 {money(equity)}  现金 {money(cash)}  单位净值 {nav:.4f}  收益 {pct(nav - 1)}")


def _position_rows(
    positions: Sequence[tuple[Position, str]], market: StoreMarket, through: date
) -> list[list[str]]:
    """(持仓, 来源) -> 表格行；收益含持有期间收到的现金分红，现价取不到时显示 -。"""
    rows: list[list[str]] = []
    for pos, origin in positions:
        symbol, shares, cost = pos.symbol, pos.shares, pos.cost
        try:
            close: float | None = market.last_close(through, symbol)
        except KeyError:
            close = None
        rows.append(
            [
                symbol,
                f"{shares:,.0f}",
                money(cost / shares),
                "-" if close is None else money(close),
                "-" if close is None else money(shares * close),
                "-" if close is None else pct(pos.ret(close)),
                origin,
            ]
        )
    return rows


_POSITION_HEADERS = ["代码", "股数", "成本价", "现价", "市值", "收益", "来源"]


def _show_manual(
    cli: Runtime, s: Session, user: User, sub: Subscription, book: Book, calendar: TradingCalendar
) -> None:
    today = _today()
    market = _market(cli, calendar, today)
    rows = repo.list_ledger_entries(s, user.id, book.id)
    days = calendar.between(_EARLIEST, today)
    through = max([days[-1], *(r.effective_day for r in rows if r.kind != "void")])
    state = manual_ops.manual_state(s, user.id, sub.id, market, calendar, cli.cfg(), through)
    equity = state.equity[-1] if state.equity else sub.initial_cash
    nav = state.unit_nav[-1] if state.unit_nav else 1.0
    _show_header(user, sub, "manual", through, equity, state.cash, nav)
    _emit("")
    if state.positions:
        _emit("持仓：")
        held = [(p, _origin_label(state.origins.get(p.symbol))) for p in state.positions.values()]
        _table(_POSITION_HEADERS, _position_rows(held, market, through), right=[1, 2, 3, 4, 5])
    else:
        _emit("持仓：无")
    _emit("")
    _emit(f"最近 {_RECENT_ENTRIES} 条流水（含推算出的除权记录）：")
    lines = _recent_entries(rows, state.actions)
    for line in lines or ["暂无流水"]:
        _emit(f"  {line}")


def _show_system(
    cli: Runtime, s: Session, user: User, sub: Subscription, book: Book, calendar: TradingCalendar
) -> None:
    state_row = repo.get_system_state(s, user.id, book.id)
    book_days = repo.list_book_days(s, user.id, book.id)
    if state_row is None or not book_days:
        _emit(f"{user.email} 的系统账尚未推进过（每日运行之后才有数据）")
        return
    engine_state, _risk = load_state(state_row.state)
    last = book_days[-1]
    _show_header(user, sub, "system", last.day, last.equity, last.cash, last.unit_nav)
    _emit("")
    market = _market(cli, calendar, last.day)
    if engine_state.positions:
        _emit("持仓：")
        held = [(p, "按信号买入") for p in engine_state.positions.values()]
        _table(_POSITION_HEADERS, _position_rows(held, market, last.day), right=[1, 2, 3, 4, 5])
    else:
        _emit("持仓：无")


@book_app.command("show")
def book_show(ctx: typer.Context, email: Email, book: BookOption = "manual") -> None:
    """显示账本：净值、现金、持仓（成本、现价、收益、来源）与最近 20 条流水。"""
    with _unit(ctx) as (cli, s):
        kind = _book_kind(book)
        user = _user(s, email)
        sub = _shown_sub(s, user)
        row = repo.get_book_of_kind(s, user.id, sub.id, kind)
        calendar = cli.calendar()
        if kind == "manual":
            _show_manual(cli, s, user, sub, row, calendar)
        else:
            _show_system(cli, s, user, sub, row, calendar)


# ---------- 信号、日志、运行记录 ----------


def _status_value(text: str) -> str:
    """--status 接受英文取值或中文名。"""
    if text in _SIGNAL_STATUS_LABELS:
        return SignalStatus(text).value
    for value, label in _SIGNAL_STATUS_LABELS.items():
        if text == label:
            return value
    raise ValidationError(
        "状态只能是：" + "、".join(f"{v}（{label}）" for v, label in _SIGNAL_STATUS_LABELS.items())
    )


def _signal_row(sig: Signal) -> list[str]:
    status = _SIGNAL_STATUS_LABELS.get(sig.status, sig.status)
    note = sig.reason + (f"（{sig.void_note}）" if sig.void_note else "")
    return [
        str(sig.id),
        str(sig.signal_day),
        _SIDE_LABELS.get(sig.side, sig.side),
        sig.symbol,
        f"{sig.shares:,.0f}",
        money(sig.ref_price),
        status,
        str(sig.status_day),
        "-" if sig.valid_until is None else str(sig.valid_until),
        note,
    ]


@admin_app.command("signals")
def signals(
    ctx: typer.Context,
    email: Email,
    book: BookOption = "manual",
    status: Annotated[str | None, typer.Option("--status", help="只看某个状态")] = None,
    days: Annotated[int, typer.Option("--days", help="只看最近多少天的信号")] = 30,
) -> None:
    """信号列表（按你设定的规则计算出的提示）。"""
    with _unit(ctx) as (_cli, s):
        kind = _book_kind(book)
        wanted = None if status is None else _status_value(status)
        user = _user(s, email)
        sub = _shown_sub(s, user)
        row = repo.get_book_of_kind(s, user.id, sub.id, kind)
        since = _today() - timedelta(days=days)
        found = [
            sig
            for sig in repo.list_signals(
                s, user.id, row.id, statuses=None if wanted is None else [wanted]
            )
            if sig.signal_day >= since
        ]
        if not found:
            _emit(f"最近 {days} 天没有符合条件的信号")
            return
        _emit(f"{user.email} · {'手动账' if kind == 'manual' else '系统账'}的信号（仅为提示）：")
        _table(
            [
                "编号",
                "信号日",
                "方向",
                "代码",
                "股数",
                "参考价",
                "状态",
                "状态日",
                "有效期至",
                "说明",
            ],
            [_signal_row(sig) for sig in reversed(found)],
            right=[0, 4, 5],
        )


@admin_app.command("journal")
def journal(ctx: typer.Context, email: Email, book: BookOption = "manual") -> None:
    """交易日志：已完成的交易与离场、放弃、被否决之后的跟踪记录。"""
    with _unit(ctx) as (_cli, s):
        kind = _book_kind(book)
        user = _user(s, email)
        sub = _shown_sub(s, user)
        row = repo.get_book_of_kind(s, user.id, sub.id, kind)
        trades = repo.list_trades(s, user.id, row.id)
        _emit(f"{user.email} · {'手动账' if kind == 'manual' else '系统账'}")
        _emit("已完成的交易：")
        if trades:
            _table(
                [
                    "代码",
                    "买入日",
                    "卖出日",
                    "股数",
                    "成本",
                    "收回",
                    "收益",
                    "持有天数",
                    "离场",
                    "来源",
                ],
                [
                    [
                        t.symbol,
                        str(t.entry_day),
                        str(t.exit_day),
                        f"{t.shares:,.0f}",
                        money(t.cost),
                        money(t.proceeds + t.cash_received),
                        pct((t.proceeds + t.cash_received) / t.cost - 1),
                        str(t.holding_days),
                        _EXIT_LABELS.get(t.exit_reason, t.exit_reason),
                        _origin_label(t.origin),
                    ]
                    for t in trades
                ],
                right=[3, 4, 5, 6, 7],
            )
        else:
            _emit("  暂无")
        _emit("")
        _emit("跟踪记录（事件之后 20、60 个交易日的复权涨跌）：")
        followups = repo.list_followups(s, user.id, row.id)
        if followups:
            _table(
                ["代码", "事件", "事件日", "20 日", "60 日"],
                [
                    [
                        f.symbol,
                        _FOLLOWUP_LABELS.get(f.event, f.event),
                        str(f.event_day),
                        "待填" if f.ret_20 is None else pct(f.ret_20),
                        "待填" if f.ret_60 is None else pct(f.ret_60),
                    ]
                    for f in followups
                ],
                right=[3, 4],
            )
        else:
            _emit("  暂无")


def _reason_lines(detail: dict[str, Any]) -> list[str]:
    """从每日运行的 detail 里取出失败原因与异常明细。"""
    lines: list[str] = []
    error = detail.get("error")
    if error:
        lines.append(f"原因：{error}")
    for problem in cast("list[Any]", detail.get("problems") or []):
        lines.append(f"数据问题：{problem}")
    failed = cast("dict[str, Any]", detail.get("download_failed") or {})
    if failed:
        lines.append(f"下载失败 {len(failed)} 只（已沿用本地数据）：")
        lines.extend(f"  {symbol}：{message}" for symbol, message in failed.items())
    bulk = detail.get("bulk")
    if isinstance(bulk, dict):
        info = cast("dict[str, Any]", bulk)
        lines.append(
            format_bulk(
                bool(info.get("used")),
                int(info.get("bars") or 0),
                int(info.get("suspended") or 0),
                int(info.get("per_symbol") or 0),
                str(info.get("reason") or ""),
            )
        )
    retried = detail.get("bars_retried")
    if retried:
        lines.append(f"缺当日日线、已再下载一次：{_json_text(retried)}")
    for item in cast("list[dict[str, Any]]", detail.get("user_errors") or []):
        lines.append(
            f"使用者 {item.get('user_id')}（订阅 {item.get('subscription_id')}）处理出错："
            f"{item.get('error')}"
        )
    labels = {
        "followups_error": "跟踪记录回填出错",
        "mail_error": "邮件发送出错",
        "fundamentals_failed": "基本面复核失败",
        "recheck_errors": "基本面复核出错",
        "weekly_errors": "周报出错",
    }
    for key, label in labels.items():
        value = detail.get(key)
        if value:
            lines.append(f"{label}：{value if isinstance(value, str) else _json_text(value)}")
    mail = detail.get("mail")
    if isinstance(mail, dict):
        sent = cast("dict[str, Any]", mail)
        lines.append(f"邮件：已发 {sent.get('sent', 0)} 封，失败 {sent.get('failed', 0)} 封")
    return lines


@admin_app.command("runs")
def runs(
    ctx: typer.Context,
    last: Annotated[int, typer.Option("--last", help="显示最近多少个交易日")] = 10,
) -> None:
    """每日运行记录与失败原因。"""
    with _unit(ctx) as (_cli, s):
        counts = admin_repo.count_outbox_by_status(s)
        _emit(
            f"发件箱：待发 {counts.get('pending', 0)} 封，发送失败 {counts.get('failed', 0)} 封"
            "（qt admin outbox list 查看）"
        )
        found = admin_repo.list_recent_runs(s, last)
        if not found:
            _emit("还没有运行记录")
            return
        for run in found:
            started = run.started_at.astimezone(_MARKET_TZ).strftime("%Y-%m-%d %H:%M")
            finished = (
                "未结束"
                if run.finished_at is None
                else run.finished_at.astimezone(_MARKET_TZ).strftime("%H:%M")
            )
            _emit(
                f"{run.trade_day}  {_RUN_STATUS_LABELS.get(run.status, run.status)}"
                f"  阶段 {run.stage}  开始 {started}  结束 {finished}"
            )
            for line in _reason_lines(run.detail):
                _emit(f"    {line}")


# ---------- 发件箱 ----------


def _outbox_status(text: str) -> str:
    """--status 接受英文取值或中文名。"""
    for value, label in _OUTBOX_STATUS_LABELS.items():
        if text in (value, label):
            return value
    raise ValidationError(
        "状态只能是：" + "、".join(f"{v}（{label}）" for v, label in _OUTBOX_STATUS_LABELS.items())
    )


@outbox_app.command("list")
def outbox_list(
    ctx: typer.Context,
    status: Annotated[
        str | None, typer.Option("--status", help="只看某个状态：pending、sent、failed")
    ] = None,
    last: Annotated[int, typer.Option("--last", help="显示最近多少封")] = 50,
) -> None:
    """列出发件箱里最近的邮件：状态、尝试次数与最近一次错误。"""
    with _unit(ctx) as (_cli, s):
        wanted = None if status is None else _outbox_status(status)
        rows = admin_repo.list_outbox(s, wanted, last)
        if not rows:
            _emit("发件箱里没有符合条件的邮件")
            return
        _table(
            ["编号", "入队时间", "状态", "种类", "收件人", "尝试", "主题", "最近错误"],
            [
                [
                    str(row.id),
                    row.created_at.astimezone(_MARKET_TZ).strftime("%Y-%m-%d %H:%M"),
                    _OUTBOX_STATUS_LABELS.get(row.status, row.status),
                    row.kind,
                    row.to_addr,
                    str(row.attempts),
                    row.subject,
                    (row.last_error or "")[:_OUTBOX_ERROR_CHARS],
                ]
                for row in rows
            ],
            right=[0, 5],
        )


@outbox_app.command("retry")
def outbox_retry(
    ctx: typer.Context,
    outbox_id: Annotated[
        int | None, typer.Argument(metavar="ID", help="要重新排队的邮件编号")
    ] = None,
    all_failed: Annotated[
        bool, typer.Option("--all-failed", help="把全部发送失败的邮件重新排队")
    ] = False,
) -> None:
    """把发送失败的邮件重新排队（重新开始 48 小时的重试期），由 worker 下一次补发。"""
    with _unit(ctx) as (_cli, s):
        if (outbox_id is None) == (not all_failed):
            raise ValidationError("请指定一个邮件编号，或用 --all-failed 处理全部发送失败的邮件")
        rows = requeue_failed(s, None if all_failed else outbox_id, now())
        _emit(f"已重新排队 {len(rows)} 封邮件，worker 会在下一次补发时发送")
