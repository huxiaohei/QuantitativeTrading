"""使用者与订阅：创建、参数修改、暂停、恢复、关闭。

本模块的函数只 flush、不 commit，事务边界由调用方（session_scope）决定。
每个改动使用者数据的函数都写一条审计日志。
"""

import math
import unicodedata
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, cast

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qt.backtest.sweep import override
from qt.config import AppConfig, RiskConfig, StrategyConfig
from qt.db import admin_repo, repo
from qt.db.models import Book, ParamChange, Subscription, User
from qt.service.errors import ValidationError

STRATEGY_NAME = "high_dividend_low_position"
# 使用者可调整的区段；其余区段与 strategy.lookback_days 全站统一
_USER_SECTIONS = ("strategy", "risk")
_SITE_WIDE_FIELDS = frozenset({"strategy.lookback_days"})
_MAX_EMAIL_LENGTH = 254  # RFC 5321 的地址上限


def _now() -> datetime:
    return datetime.now(UTC)


def _params_of(cfg: AppConfig) -> dict[str, Any]:
    return {
        "strategy": cfg.strategy.model_dump(mode="json"),
        "risk": cfg.risk.model_dump(mode="json"),
    }


def _apply_overrides(cfg: AppConfig, overrides: Mapping[str, object]) -> AppConfig:
    """逐条应用参数覆盖；路径不可调整或取值不合法时抛 ValidationError。"""
    for path, value in overrides.items():
        section = path.partition(".")[0]
        if section not in _USER_SECTIONS or path in _SITE_WIDE_FIELDS:
            raise ValidationError(f"参数 {path} 全站统一，不能单独修改")
        try:
            cfg = override(cfg, path, value)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    return cfg


def subscription_config(cfg: AppConfig, sub: Subscription) -> AppConfig:
    """站点配置，加上该订阅自己的 strategy 与 risk。lookback_days 始终取站点值。"""
    try:
        raw_strategy = {**cast("dict[str, Any]", sub.params["strategy"])}
        raw_strategy["lookback_days"] = cfg.strategy.lookback_days
        strategy = StrategyConfig.model_validate(raw_strategy)
        risk = RiskConfig.model_validate(sub.params["risk"])
    except PydanticValidationError as exc:
        raise ValidationError(
            f"订阅 {sub.id} 的参数已不符合当前规则：{exc.errors()[0]['msg']}"
        ) from exc
    return cfg.model_copy(update={"strategy": strategy, "risk": risk})


def _risk_of(params: Mapping[str, Any], sub: Subscription) -> RiskConfig:
    try:
        return RiskConfig.model_validate(params["risk"])
    except (PydanticValidationError, KeyError) as exc:
        raise ValidationError(f"订阅 {sub.id} 的历史风控参数已不符合当前规则：{exc}") from exc


def risk_schedule(s: Session, sub: Subscription) -> list[tuple[date, RiskConfig]]:
    """订阅风控参数的时间表 [(生效日, 配置)]，按修改先后排列，第一项的生效日为 date.min。

    没有修改过时只有当前参数；修改过时，第一次修改之前用那次修改的 old，之后每次修改的 new
    从它的生效日起作用。手动账重放据此逐日取当天适用的风控参数。
    """
    changes = repo.list_param_changes(s, sub.user_id, sub.id)
    if not changes:
        return [(date.min, _risk_of(sub.params, sub))]
    schedule = [(date.min, _risk_of(changes[0].old, sub))]
    schedule.extend((c.effective_day, _risk_of(c.new, sub)) for c in changes)
    return schedule


def _valid_email(address: str) -> bool:
    """只有一个 @、本地部分非空、域名含点且各段非空；不含空白与控制字符（含不可见格式字符）。"""
    if len(address) > _MAX_EMAIL_LENGTH:
        return False
    if any(unicodedata.category(ch).startswith(("C", "Z")) for ch in address):
        return False  # 控制字符、不可见格式字符与各类空白
    local, at, domain = address.partition("@")
    if not at or not local or "@" in domain:
        return False
    labels = domain.split(".")
    return len(labels) >= 2 and all(labels)


def create_user(s: Session, email: str, is_admin: bool = False) -> User:
    """注册使用者；邮箱转小写，重复或格式不对时抛 ValidationError。

    邮箱之后会作为收件人，也会被打印进给使用者的命令里，所以格式在这里严格检查。
    """
    normalized = email.strip().lower()
    if not _valid_email(normalized):
        raise ValidationError("邮箱格式不正确")
    if admin_repo.find_user_by_email(s, normalized) is not None:
        raise ValidationError("该邮箱已注册")
    user = User(email=normalized, is_admin=is_admin)
    try:
        with s.begin_nested():
            s.add(user)
    except IntegrityError as exc:
        raise ValidationError("该邮箱已注册") from exc
    repo.add_audit(s, user.id, "user.create", f"user:{user.id}", {"is_admin": is_admin})
    return user


def set_weekly_mail(s: Session, user_id: int, enabled: bool) -> User:
    """开关使用者的周报邮件，并写审计日志。"""
    user = repo.get_user(s, user_id)
    user.weekly_mail = enabled
    s.flush()
    repo.add_audit(s, user_id, "user.weekly_mail", f"user:{user_id}", {"enabled": enabled})
    return user


def create_subscription(
    s: Session,
    user_id: int,
    cfg: AppConfig,
    initial_cash: float,
    start_day: date,
    overrides: Mapping[str, object] | None = None,
) -> Subscription:
    """为使用者建立订阅，同时建立系统账与手动账。每人同时只能有一个未关闭的订阅。"""
    user = repo.get_user(s, user_id)
    if user.status != "active":
        raise ValidationError("账号已停用，不能创建订阅")
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValidationError("本金必须是大于 0 的有限数值")
    if repo.get_open_subscription(s, user_id) is not None:
        raise ValidationError("已有生效的订阅，请先关闭后再创建新的")
    merged = _apply_overrides(cfg, overrides or {})
    sub = Subscription(
        user_id=user_id,
        strategy_name=STRATEGY_NAME,
        params=_params_of(merged),
        initial_cash=initial_cash,
        status="active",
        start_day=start_day,
    )
    try:
        with s.begin_nested():
            s.add(sub)
    except IntegrityError as exc:
        raise ValidationError("已有生效的订阅，请先关闭后再创建新的") from exc
    s.add_all([Book(subscription_id=sub.id, user_id=user_id, kind=k) for k in ("system", "manual")])
    s.flush()
    repo.add_audit(
        s,
        user_id,
        "subscription.create",
        f"subscription:{sub.id}",
        {"initial_cash": initial_cash, "start_day": start_day.isoformat(), "params": sub.params},
    )
    return sub


def update_params(
    s: Session,
    user_id: int,
    subscription_id: int,
    cfg: AppConfig,
    overrides: Mapping[str, object],
    effective_day: date,
) -> Subscription:
    """修改订阅参数，保留修改前后的完整快照；账本与历史不动。"""
    sub = repo.get_subscription(s, user_id, subscription_id)
    if sub.status == "closed":
        raise ValidationError("订阅已关闭，不能修改参数")
    merged = _apply_overrides(subscription_config(cfg, sub), overrides)
    old = sub.params
    new = _params_of(merged)
    if new == old:
        raise ValidationError("参数没有变化")
    sub.params = new
    s.add(
        ParamChange(
            subscription_id=sub.id,
            user_id=user_id,
            old=old,
            new=new,
            effective_day=effective_day,
        )
    )
    s.flush()
    repo.add_audit(
        s,
        user_id,
        "subscription.update_params",
        f"subscription:{sub.id}",
        {
            "overrides": {
                path: new[path.partition(".")[0]][path.partition(".")[2]] for path in overrides
            },
            "effective_day": effective_day.isoformat(),
        },
    )
    return sub


def _transition(
    s: Session, user_id: int, subscription_id: int, allowed: tuple[str, ...], to: str, action: str
) -> None:
    sub = repo.get_subscription(s, user_id, subscription_id)
    if sub.status not in allowed:
        raise ValidationError(f"订阅当前状态为 {sub.status}，不能执行该操作")
    sub.status = to
    if to == "closed":
        sub.closed_at = _now()
    s.flush()
    repo.add_audit(s, user_id, action, f"subscription:{sub.id}", {"to": to})


def pause_subscription(s: Session, user_id: int, subscription_id: int) -> None:
    """暂停生效中的订阅。"""
    _transition(s, user_id, subscription_id, ("active",), "paused", "subscription.pause")


def resume_subscription(s: Session, user_id: int, subscription_id: int) -> None:
    """恢复已暂停的订阅。"""
    _transition(s, user_id, subscription_id, ("paused",), "active", "subscription.resume")


def close_subscription(s: Session, user_id: int, subscription_id: int) -> None:
    """关闭订阅（不可恢复）；账本与历史保留。"""
    _transition(s, user_id, subscription_id, ("active", "paused"), "closed", "subscription.close")
