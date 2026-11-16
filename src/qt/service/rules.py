"""命令行与网页共用的业务规则：订阅生效日与起始日、订阅的查看口径、可调参数的说明。

这里只放规则，不碰界面：命令行（qt.admin_cli）与网页都从这里取，保证两边口径一致。
"""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.db import repo
from qt.db.models import Subscription
from qt.runtime import now
from qt.service.errors import ValidationError


@dataclass(frozen=True)
class ParamInfo:
    """一个使用者可调参数的展示信息。

    minimum / maximum 用配置里的单位（比例类为 0 到 1 的小数）；
    没有数值范围的项（开关、二选一）为 None。
    """

    key: str
    label: str
    description: str
    kind: Literal["int", "float", "percent"]
    minimum: float | None
    maximum: float | None


PARAMS: tuple[ParamInfo, ...] = (
    ParamInfo(
        "strategy.min_dividend_yield",
        "最低股息率",
        "候选股票的股息率必须不低于这个值。调高会让候选更少、更偏重分红；调低会放进更多候选。",
        "percent",
        0.0,
        1.0,
    ),
    ParamInfo(
        "strategy.max_price_position",
        "最高价格位置",
        "价格位置指现价在回看区间内从最低到最高的相对位置，候选股票必须不高于这个值。"
        "调低会只留下更靠近区间低点的股票；调高会放进位置更高的股票。",
        "percent",
        0.0,
        1.0,
    ),
    ParamInfo(
        "strategy.lookback_days",
        "回看天数（全站统一）",
        "计算价格位置时回看的交易日数，全站统一，不能为单个使用者单独调整。"
        "天数越多，区间越长、对短期波动越不敏感；天数越少，位置越随近期行情变化。",
        "int",
        1,
        None,
    ),
    ParamInfo(
        "strategy.min_dividend_years",
        "最少连续分红年数",
        "候选股票至少要连续分红这么多年。调高会排除分红历史较短的公司；调低会放宽分红历史的要求。",
        "int",
        0,
        None,
    ),
    ParamInfo(
        "strategy.exclude_dividend_cut",
        "排除削减分红的股票",
        "取值为是或否。选是时，最近一次分红比上一次减少的股票不会进入候选；选否时不做这项排除。",
        "int",
        None,
        None,
    ),
    ParamInfo(
        "strategy.min_roe_avg3",
        "三年平均净资产收益率下限",
        "候选股票近三年平均净资产收益率必须不低于这个值。调高会只留下盈利能力更强的公司；调低会放宽盈利要求。",
        "percent",
        None,
        None,
    ),
    ParamInfo(
        "strategy.max_payout_ratio",
        "分红率上限",
        "分红占净利润的比例不得超过这个值。调低会排除分红占利润过高、可持续性存疑的公司；调高会放宽这项限制。",
        "percent",
        0.0,
        None,
    ),
    ParamInfo(
        "strategy.min_profit_yoy",
        "净利润同比下限",
        "候选股票最近的净利润同比增速必须不低于这个值。调高会要求利润增长更好；调低（可为负数）会容许利润下滑的公司入选。",
        "percent",
        None,
        None,
    ),
    ParamInfo(
        "strategy.max_debt_ratio",
        "资产负债率上限",
        "资产负债率超过这个值的股票不会进入候选。调低会排除负债更重的公司；调高会放宽这项限制。",
        "percent",
        0.0,
        1.0,
    ),
    ParamInfo(
        "strategy.take_profit_mode",
        "止盈方式",
        "二选一：固定止盈（涨到固定比例就提示卖出）或移动止盈（涨到启动涨幅后，从最高点回撤到设定比例再提示卖出）。"
        "固定止盈规则更简单，移动止盈在上涨行情里能多持有一段。",
        "int",
        None,
        None,
    ),
    ParamInfo(
        "strategy.take_profit_fixed",
        "固定止盈比例",
        "选用固定止盈时，持仓相对成本上涨到这个比例就提示卖出。调高会持有更久、等待更大涨幅；调低会更早兑现。",
        "percent",
        0.0,
        None,
    ),
    ParamInfo(
        "strategy.trailing_activate",
        "移动止盈启动涨幅",
        "选用移动止盈时，持仓相对成本上涨超过这个比例，才开始跟踪最高点。调高会让跟踪启动得更晚；调低会更早启动。",
        "percent",
        0.0,
        None,
    ),
    ParamInfo(
        "strategy.trailing_drawdown",
        "移动止盈回撤比例",
        "跟踪启动后，价格从最高点回落到这个比例就提示卖出。调大会容忍更深的回落、持有更久；调小会更快提示卖出。",
        "percent",
        0.0,
        1.0,
    ),
    ParamInfo(
        "strategy.stop_loss",
        "止损比例",
        "持仓相对成本亏损到这个比例就提示卖出。调大会容忍更大的亏损；调小会更早止损，但也更容易被正常波动触发。",
        "percent",
        0.0,
        1.0,
    ),
    ParamInfo(
        "strategy.time_stop_days",
        "持有天数上限",
        "持仓超过这么多个交易日仍未触发其他卖出条件，就提示卖出。调大允许持有更久；调小会更快换仓。",
        "int",
        1,
        None,
    ),
    ParamInfo(
        "strategy.fundamental_profit_yoy",
        "基本面卖出的净利润同比线",
        "持仓公司最新净利润同比低于这条线时，提示基本面变差而卖出。调高会更敏感、更容易提示卖出；调低会更宽容。",
        "percent",
        None,
        None,
    ),
    ParamInfo(
        "risk.max_position_pct",
        "单只仓位上限",
        "单只股票的市值占总资产的比例上限。调大会让资金更集中在少数股票；调小会更分散，但每只买得更少。",
        "percent",
        0.0,
        1.0,
    ),
    ParamInfo(
        "risk.max_positions",
        "最多持仓只数",
        "同时持有的股票只数上限，达到后不再提示新买入。调大会更分散；调小会更集中。",
        "int",
        1,
        None,
    ),
    ParamInfo(
        "risk.max_industry_pct",
        "单一行业仓位上限",
        "同一行业的持仓市值占总资产的比例上限。调小会让持仓分布在更多行业；调大则允许集中在少数行业。",
        "percent",
        0.0,
        1.0,
    ),
    ParamInfo(
        "risk.warn_drawdown",
        "回撤预警线",
        "账户从历史最高净值回落到这个比例时，发出回撤预警提示。调小会更早收到预警；调大则只在回撤更深时才提示。",
        "percent",
        0.0,
        1.0,
    ),
    ParamInfo(
        "risk.pause_drawdown",
        "回撤暂停线",
        "账户从历史最高净值回落到这个比例时，暂停新的买入提示。调小会更早暂停；调大则要回撤更深才暂停。",
        "percent",
        0.0,
        1.0,
    ),
    ParamInfo(
        "risk.pause_days",
        "暂停买入的交易日数",
        "触发回撤暂停后，持续暂停买入提示的交易日数。调大会暂停更久；调小会更快恢复。",
        "int",
        1,
        None,
    ),
    ParamInfo(
        "risk.cooldown_days",
        "止损后的冷却交易日数",
        "某只股票止损卖出后，这么多个交易日内不再对它提示买入。"
        "调大会让同一只股票隔更久才再次入选；调到 0 表示不冷却。",
        "int",
        0,
        None,
    ),
)


def next_effective_day(s: Session, sub: Subscription, calendar: TradingCalendar) -> date:
    """订阅两本账下一个要处理的交易日（较早的那本）；从没推进过为起始日。

    改参数之后，每日运行从这一天起使用新参数（系统账、手动账一致），已推进过的日子不变。
    """
    starts: list[date] = []
    for kind in ("system", "manual"):
        book = repo.get_book_of_kind(s, sub.user_id, sub.id, kind)
        if book.processed_through is None:
            starts.append(sub.start_day)
            continue
        try:
            starts.append(calendar.offset(book.processed_through, 1))
        except ValueError as exc:
            raise ValidationError(
                "交易日历还没有覆盖到下一次运行的日期，请先运行 qt data update"
            ) from exc
    return min(starts)


def resolve_start_day(
    text: str | None,
    calendar: TradingCalendar,
    default: date,
    backdate_limit_days: int,
    *,
    today: date | None = None,
) -> date:
    """订阅起始日：缺省取 default；自选的必须是交易日，不晚于 default，不早于回溯上限。

    回溯上限从今天算起；today 缺省取当前日期，测试可以显式给出。
    """
    if text is None:
        return default
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"日期格式应为 YYYY-MM-DD，收到 {text!r}") from exc
    if not calendar.is_trading_day(day):
        raise ValidationError(f"起始日 {day} 不是交易日")
    if day > default:
        raise ValidationError(f"起始日不能晚于下一个交易日 {default}")
    try:
        earliest = calendar.offset(today or now().date(), -backdate_limit_days)
    except ValueError:
        earliest = calendar.first_day
    if day < earliest:
        raise ValidationError(
            f"起始日 {day} 太早：最早只能选 {earliest}（{backdate_limit_days} 个交易日之前）"
        )
    return day


def shown_subscription(s: Session, user_id: int) -> Subscription | None:
    """查看用：优先未关闭的订阅，否则最近一个（已关闭的订阅仍可查看）；一个都没有为 None。"""
    return repo.get_open_subscription(s, user_id) or repo.get_latest_subscription(s, user_id)


def list_subscriptions(s: Session, user_id: int) -> list[Subscription]:
    """该使用者的全部订阅，按创建时间倒序。"""
    return repo.list_subscriptions(s, user_id)
