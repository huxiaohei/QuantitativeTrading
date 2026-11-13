"""使用者与订阅：创建、参数修改、暂停、恢复、关闭。

本模块的函数只 flush、不 commit，事务边界由调用方（session_scope）决定。
每个改动使用者数据的函数都写一条审计日志。
"""

import math
import re
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
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def create_user(s: Session, email: str, is_admin: bool = False) -> User:
    """注册使用者；邮箱转小写，重复或格式不对时抛 ValidationError。"""
    normalized = email.strip().lower()
    if not _EMAIL_PATTERN.match(normalized):
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
