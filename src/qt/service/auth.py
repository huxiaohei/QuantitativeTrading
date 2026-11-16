"""账号服务：注册、邮箱验证、登录与会话、找回密码、邀请码、频率限制。

本模块的函数只 flush、不 commit，事务边界由调用方决定。时间一律由调用方传入。
注意：登录失败、注册等会"先记一次频率、再抛 AuthError"，调用方遇到 AuthError 时
应提交事务（而不是回滚），否则失败次数和审计记录会丢失，限流就形同虚设。
令牌（会话、邮件）只在库里存 SHA-256；密码只存 argon2id 哈希；两者的原文都不写日志。
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qt.config import WebConfig
from qt.db import admin_repo, repo
from qt.db.models import EmailToken, Invite, RateEvent, User, WebSession
from qt.service import accounts
from qt.service.actor import Actor, web_actor
from qt.service.errors import (
    AuthError,
    EmailNotVerifiedError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)

EmailTokenKind = Literal["verify", "reset"]

_HASHER = PasswordHasher()
# 用户不存在时对它做一次真实校验，让"邮箱不存在"与"密码错"耗时同量级
_DUMMY_HASH = _HASHER.hash("dummy-password-for-timing")
_ANONYMOUS_WEB = Actor(None, "web")

_LOGIN_FAIL_MESSAGE = "邮箱或密码不正确"
_INVITE_INVALID_MESSAGE = "邀请码无效或已过期"
_TOKEN_INVALID_MESSAGE = "链接无效或已过期"
_SESSION_WRITE_INTERVAL = timedelta(minutes=5)
_RATE_KEY_MAX = 200  # rate_events.key 的列宽
_RATE_KEEP = timedelta(days=2)


@dataclass(frozen=True)
class RegisterResult:
    """注册结果：created 为新建（带验证令牌原文），exists 为邮箱已存在（无令牌）。"""

    kind: Literal["created", "exists"]
    user_id: int
    token: str | None


@dataclass(frozen=True)
class LoginResult:
    """登录结果：token 是会话令牌原文，只交给使用者的 cookie。"""

    token: str
    session: WebSession
    user: User


# ---- 基础工具 ----


def normalize_email(text: str) -> str:
    """去首尾空白并转小写。"""
    return text.strip().lower()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _rate_key(prefix: str, value: str) -> str:
    """拼频率键；过长（邮箱可达 254 位）时对值取哈希，避免超过列宽。"""
    key = f"{prefix}:{value}"
    return key if len(key) <= _RATE_KEY_MAX else f"{prefix}:{_sha256(value)}"


def hash_password(plain: str) -> str:
    """argon2id 哈希（库的默认参数）。"""
    return _HASHER.hash(plain)


def verify_password(hashed: str | None, plain: str) -> bool:
    """校验密码；hashed 为 None 时也做一次真实校验再返回假，防止从耗时探测账号。"""
    try:
        _HASHER.verify(_DUMMY_HASH if hashed is None else hashed, plain)
    except (VerificationError, InvalidHashError):
        return False
    return hashed is not None


def check_password_rules(email: str, plain: str, min_length: int) -> None:
    """不足长度或与邮箱相同时抛 AuthError。"""
    if len(plain) < min_length:
        raise AuthError(f"密码至少 {min_length} 位")
    if normalize_email(plain) == normalize_email(email):
        raise AuthError("密码不能与邮箱相同")


# ---- 频率限制 ----


def rate_check(s: Session, key: str, limit: int, window: timedelta, now: datetime) -> None:
    """窗口内已记录的次数达到 limit 时抛 RateLimitedError。"""
    count = s.scalar(
        select(func.count())
        .select_from(RateEvent)
        .where(RateEvent.key == key, RateEvent.at > now - window)
    )
    if (count or 0) >= limit:
        raise RateLimitedError


def rate_hit(s: Session, key: str, now: datetime) -> None:
    """记一次事件。"""
    s.add(RateEvent(key=key, at=now))
    s.flush()


# ---- 邀请码 ----


def create_invite(s: Session, admin_id: int, note: str, valid_days: int, now: datetime) -> Invite:
    """管理员生成邀请码。"""
    invite = Invite(
        code=secrets.token_urlsafe(12),
        created_by=admin_id,
        note=note.strip(),
        expires_at=now + timedelta(days=valid_days),
    )
    s.add(invite)
    s.flush()
    repo.add_audit(
        s,
        admin_id,
        "invite_create",
        f"invite:{invite.id}",
        {"note": invite.note, "valid_days": valid_days},
        web_actor(admin_id),
    )
    return invite


def revoke_invite(s: Session, admin_id: int, invite_id: int, now: datetime) -> Invite:
    """作废邀请码；已被使用的不能作废，已作废的重复作废无副作用。"""
    invite = s.get(Invite, invite_id, with_for_update=True)
    if invite is None:
        raise NotFoundError("邀请码不存在")
    if invite.used_by is not None:
        raise AuthError("邀请码已被使用，不能作废")
    if invite.revoked_at is None:
        invite.revoked_at = now
        s.flush()
        repo.add_audit(s, admin_id, "invite_revoke", f"invite:{invite.id}", {}, web_actor(admin_id))
    return invite


def invite_state(invite: Invite, now: datetime) -> Literal["usable", "used", "expired", "revoked"]:
    """邀请码此刻的状态：已使用优先，其次已作废、已过期，都不是则可用（与注册时的判断一致）。"""
    if invite.used_by is not None:
        return "used"
    if invite.revoked_at is not None:
        return "revoked"
    if invite.expires_at <= now:
        return "expired"
    return "usable"


def _lock_valid_invite(s: Session, code: str, now: datetime) -> Invite:
    """按邀请码加锁取出可用的那条；不存在、过期、作废、已用一律同一句话。"""
    invite = s.scalars(select(Invite).where(Invite.code == code.strip()).with_for_update()).first()
    if (
        invite is None
        or invite.revoked_at is not None
        or invite.used_by is not None
        or invite.expires_at <= now
    ):
        raise AuthError(_INVITE_INVALID_MESSAGE)
    return invite


# ---- 邮件令牌 ----


def issue_email_token(
    s: Session, user_id: int, kind: EmailTokenKind, hours: int, now: datetime
) -> str:
    """签发邮件令牌，返回原文；同一使用者同类的旧令牌作废（删除）。"""
    s.execute(
        delete(EmailToken).where(
            EmailToken.user_id == user_id,
            EmailToken.kind == kind,
            EmailToken.used_at.is_(None),
        )
    )
    token = _new_token()
    s.add(
        EmailToken(
            kind=kind,
            token_hash=_sha256(token),
            user_id=user_id,
            expires_at=now + timedelta(hours=hours),
        )
    )
    s.flush()
    return token


def _find_token(
    s: Session, token: str, kind: EmailTokenKind, now: datetime, *, lock: bool
) -> EmailToken | None:
    stmt = select(EmailToken).where(
        EmailToken.token_hash == _sha256(token),
        EmailToken.kind == kind,
        EmailToken.used_at.is_(None),
        EmailToken.expires_at > now,
    )
    return s.scalars(stmt.with_for_update() if lock else stmt).first()


def peek_email_token(s: Session, token: str, kind: EmailTokenKind, now: datetime) -> int | None:
    """令牌有效则返回 user_id，不消耗。"""
    found = _find_token(s, token, kind, now, lock=False)
    return None if found is None else found.user_id


def verify_email(s: Session, token: str, now: datetime) -> User | None:
    """消耗验证令牌并标记邮箱已验证；令牌无效返回 None。"""
    found = _find_token(s, token, "verify", now, lock=True)
    if found is None:
        return None
    found.used_at = now
    user = repo.get_user(s, found.user_id)
    if user.email_verified_at is None:
        user.email_verified_at = now
    s.flush()
    repo.add_audit(s, user.id, "email_verify", f"user:{user.id}", {}, web_actor(user.id))
    return user


# ---- 注册 ----


def register(
    s: Session,
    email: str,
    password: str,
    invite_code: str,
    accepted_terms: bool,
    cfg: WebConfig,
    ip: str,
    now: datetime,
) -> RegisterResult:
    """注册。邀请码与密码先于邮箱查重校验，页面上看不出邮箱是否已注册。

    signup_mode 为 open 时不需要邀请码（填了也忽略）。
    """
    ip_key = _rate_key("register-ip", ip)
    rate_check(s, ip_key, 5, timedelta(hours=1), now)
    rate_hit(s, ip_key, now)
    normalized = normalize_email(email)
    if not accepted_terms:
        raise AuthError("请先阅读并同意使用条款与风险提示")
    invite = _lock_valid_invite(s, invite_code, now) if cfg.signup_mode == "invite" else None
    check_password_rules(normalized, password, cfg.password_min_length)

    existing = admin_repo.find_user_by_email(s, normalized)
    if existing is not None:
        repo.add_audit(
            s,
            existing.id,
            "register_duplicate",
            f"user:{existing.id}",
            {"ip": ip},
            _ANONYMOUS_WEB,
        )
        return RegisterResult("exists", existing.id, None)

    try:
        user = accounts.create_user(s, normalized, actor=_ANONYMOUS_WEB)
    except ValidationError as exc:  # 格式不对（或并发下被抢注）
        raise AuthError(str(exc)) from exc
    user.password_hash = hash_password(password)
    user.terms_version = cfg.terms_version
    user.terms_accepted_at = now
    if invite is not None:
        invite.used_by = user.id
        invite.used_at = now
    s.flush()
    token = issue_email_token(s, user.id, "verify", cfg.verify_token_hours, now)
    repo.add_audit(
        s,
        user.id,
        "register",
        f"user:{user.id}",
        {
            "ip": ip,
            "terms_version": cfg.terms_version,
            "invite_id": None if invite is None else invite.id,
        },
        web_actor(user.id),
    )
    return RegisterResult("created", user.id, token)


# ---- 找回密码与重发验证邮件 ----

_MAIL_LIMIT = 3
_MAIL_WINDOW = timedelta(hours=1)


def request_password_reset(
    s: Session, email: str, cfg: WebConfig, now: datetime
) -> tuple[User, str] | None:
    """申请重置：邮箱不存在、账号停用、频率超限都返回 None（对外不可区分）。"""
    normalized = normalize_email(email)
    user = admin_repo.find_user_by_email(s, normalized)
    if user is None or user.status != "active":
        return None
    key = _rate_key("mail", normalized)
    try:
        rate_check(s, key, _MAIL_LIMIT, _MAIL_WINDOW, now)
    except RateLimitedError:
        return None
    rate_hit(s, key, now)
    token = issue_email_token(s, user.id, "reset", cfg.reset_token_hours, now)
    repo.add_audit(s, user.id, "password_reset_request", f"user:{user.id}", {}, _ANONYMOUS_WEB)
    return user, token


def resend_verification(s: Session, user_id: int, cfg: WebConfig, now: datetime) -> str | None:
    """重发验证邮件：已验证或账号停用返回 None；与重置邮件共用每小时 3 封的频率。"""
    user = repo.get_user(s, user_id)
    if user.email_verified_at is not None or user.status != "active":
        return None
    key = _rate_key("mail", user.email)
    rate_check(s, key, _MAIL_LIMIT, _MAIL_WINDOW, now)
    rate_hit(s, key, now)
    token = issue_email_token(s, user.id, "verify", cfg.verify_token_hours, now)
    repo.add_audit(s, user.id, "verification_resend", f"user:{user.id}", {}, web_actor(user.id))
    return token


def _delete_sessions(s: Session, user_id: int, keep_session_id: int | None = None) -> int:
    stmt = delete(WebSession).where(WebSession.user_id == user_id)
    if keep_session_id is not None:
        stmt = stmt.where(WebSession.id != keep_session_id)
    return len(s.scalars(stmt.returning(WebSession.id)).all())


def reset_password(
    s: Session, token: str, new_password: str, cfg: WebConfig, now: datetime
) -> User:
    """用重置令牌设新密码：消耗令牌、补验证邮箱、删除该使用者全部会话。

    新密码不合规时先报错、不消耗令牌，使用者可以重试。
    """
    found = _find_token(s, token, "reset", now, lock=True)
    if found is None:
        raise AuthError(_TOKEN_INVALID_MESSAGE)
    user = repo.get_user(s, found.user_id)
    if user.status != "active":
        raise AuthError(_TOKEN_INVALID_MESSAGE)
    check_password_rules(user.email, new_password, cfg.password_min_length)
    found.used_at = now
    user.password_hash = hash_password(new_password)
    if user.email_verified_at is None:
        user.email_verified_at = now
    revoked = _delete_sessions(s, user.id)
    s.flush()
    repo.add_audit(
        s,
        user.id,
        "password_reset",
        f"user:{user.id}",
        {"sessions_removed": revoked},
        web_actor(user.id),
    )
    return user


# ---- 登录与会话 ----


def _login_failed(
    s: Session, normalized: str, ip: str, user: User | None, reason: str, now: datetime
) -> AuthError:
    """记失败次数与审计，返回统一的 AuthError 供调用方抛出。"""
    rate_hit(s, _rate_key("login", normalized), now)
    rate_hit(s, _rate_key("login-ip", ip), now)
    repo.add_audit(
        s,
        None if user is None else user.id,
        "login_failed",
        "login",
        {"email": normalized, "ip": ip, "reason": reason},
        _ANONYMOUS_WEB,
    )
    return AuthError(_LOGIN_FAIL_MESSAGE)


def login(
    s: Session,
    email: str,
    password: str,
    ip: str,
    user_agent: str,
    cfg: WebConfig,
    now: datetime,
) -> LoginResult:
    """登录。失败（含停用、不存在）一律同一句话，且只在失败时计数。"""
    normalized = normalize_email(email)
    window = timedelta(minutes=15)
    rate_check(s, _rate_key("login", normalized), 5, window, now)
    rate_check(s, _rate_key("login-ip", ip), 20, window, now)
    user = admin_repo.find_user_by_email(s, normalized)
    password_ok = verify_password(None if user is None else user.password_hash, password)
    if user is None or not password_ok:
        raise _login_failed(s, normalized, ip, user, "bad_credentials", now)
    if user.status != "active":
        raise _login_failed(s, normalized, ip, user, "disabled", now)
    if user.email_verified_at is None:
        raise EmailNotVerifiedError(user.id)
    token = _new_token()
    session = WebSession(
        token_hash=_sha256(token),
        user_id=user.id,
        csrf_token=_new_token(),
        last_seen_at=now,
        expires_at=now + timedelta(days=cfg.session_days),
        ip=ip[:64],
        user_agent=user_agent[:300],
    )
    s.add(session)
    s.flush()
    repo.add_audit(s, user.id, "login_ok", f"user:{user.id}", {"ip": ip}, web_actor(user.id))
    return LoginResult(token, session, user)


def load_session(
    s: Session, token: str, cfg: WebConfig, now: datetime
) -> tuple[WebSession, User] | None:
    """按令牌取会话与使用者；过期或使用者停用返回 None。有活动则顺延，最多每 5 分钟写一次库。"""
    session = s.scalars(select(WebSession).where(WebSession.token_hash == _sha256(token))).first()
    if session is None or session.expires_at <= now:
        return None
    user = repo.get_user(s, session.user_id)
    if user.status != "active":
        return None
    if now - session.last_seen_at >= _SESSION_WRITE_INTERVAL:
        session.last_seen_at = now
        session.expires_at = now + timedelta(days=cfg.session_days)
        s.flush()
    return session, user


def logout(s: Session, token: str) -> None:
    """退出：删除该会话（不存在则无事发生）。"""
    session = s.scalars(select(WebSession).where(WebSession.token_hash == _sha256(token))).first()
    if session is None:
        return
    user_id = session.user_id
    s.delete(session)
    s.flush()
    repo.add_audit(s, user_id, "logout", f"user:{user_id}", {}, web_actor(user_id))


def logout_all(s: Session, user_id: int) -> int:
    """删除该使用者的全部会话，返回删除数。"""
    removed = _delete_sessions(s, user_id)
    repo.add_audit(
        s,
        user_id,
        "logout_all",
        f"user:{user_id}",
        {"sessions_removed": removed},
        web_actor(user_id),
    )
    return removed


def change_password(
    s: Session,
    user_id: int,
    current: str,
    new: str,
    cfg: WebConfig,
    keep_session_id: int,
    now: datetime,
) -> None:
    """已登录改密码：校验当前密码，删除除当前会话外的全部会话。"""
    user = repo.get_user(s, user_id)
    if not verify_password(user.password_hash, current):
        raise AuthError("当前密码不正确")
    check_password_rules(user.email, new, cfg.password_min_length)
    user.password_hash = hash_password(new)
    removed = _delete_sessions(s, user_id, keep_session_id)
    s.flush()
    repo.add_audit(
        s,
        user_id,
        "password_change",
        f"user:{user_id}",
        {"sessions_removed": removed},
        web_actor(user_id),
    )


# ---- 条款与账号状态 ----


def needs_terms(user: User, cfg: WebConfig) -> bool:
    """使用者同意的条款版本不是当前版本时为真。"""
    return user.terms_version != cfg.terms_version


def accept_terms(s: Session, user_id: int, cfg: WebConfig, now: datetime, actor: Actor) -> None:
    """记录同意当前版本的条款。"""
    user = repo.get_user(s, user_id)
    user.terms_version = cfg.terms_version
    user.terms_accepted_at = now
    s.flush()
    repo.add_audit(
        s, user_id, "terms_accept", f"user:{user_id}", {"terms_version": cfg.terms_version}, actor
    )


def set_user_status(
    s: Session,
    admin_id: int,
    user_id: int,
    status: Literal["active", "disabled"],
    now: datetime,
) -> User:
    """管理员停用或恢复账号；停用时删除其全部会话，不能停用自己。"""
    if status == "disabled" and admin_id == user_id:
        raise AuthError("不能停用自己的账号")
    user = repo.get_user(s, user_id)
    if user.status == status:
        return user
    user.status = status
    removed = _delete_sessions(s, user_id) if status == "disabled" else 0
    s.flush()
    repo.add_audit(
        s,
        user_id,
        "user_disable" if status == "disabled" else "user_enable",
        f"user:{user_id}",
        {"sessions_removed": removed},
        web_actor(admin_id),
    )
    return user


def purge_expired(s: Session, now: datetime) -> int:
    """清理过期的会话、过期的邮件令牌、超过 2 天的频率记录，返回删除总数。"""
    total = 0
    for stmt in (
        delete(WebSession).where(WebSession.expires_at <= now).returning(WebSession.id),
        delete(EmailToken).where(EmailToken.expires_at <= now).returning(EmailToken.id),
        delete(RateEvent).where(RateEvent.at < now - _RATE_KEEP).returning(RateEvent.id),
    ):
        total += len(s.scalars(stmt).all())
    return total
