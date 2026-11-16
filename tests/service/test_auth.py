"""账号服务：密码、注册、令牌、登录、会话、找回密码、改密码、停用、邀请码、清理、审计。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qt.config import WebConfig
from qt.db import admin_repo, repo
from qt.db.models import AuditLog, EmailToken, Invite, RateEvent, User, WebSession
from qt.service import auth
from qt.service.accounts import create_user
from qt.service.actor import CLI, Actor, web_actor
from qt.service.errors import (
    AuthError,
    EmailNotVerifiedError,
    NotFoundError,
    RateLimitedError,
)

CFG = WebConfig()
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
PW = "correct-horse-battery"
IP = "203.0.113.7"
LOGIN_FAIL = "邮箱或密码不正确"


def _admin(db: Session) -> User:
    return create_user(db, "admin@example.com", is_admin=True, actor=CLI)


def _invite(db: Session, admin: User, now: datetime = NOW) -> str:
    return auth.create_invite(db, admin.id, "给朋友", 14, now).code


def _register(
    db: Session,
    email: str = "foo@example.com",
    code: str | None = None,
    ip: str = IP,
    now: datetime = NOW,
) -> auth.RegisterResult:
    if code is None:
        code = _invite(db, _admin_once(db), now)
    return auth.register(db, email, PW, code, True, CFG, ip, now)


def _admin_once(db: Session) -> User:
    found = admin_repo.find_user_by_email(db, "admin@example.com")
    return found if found is not None else _admin(db)


def _verified_user(db: Session, email: str = "foo@example.com") -> User:
    res = _register(db, email)
    assert res.token is not None
    user = auth.verify_email(db, res.token, NOW)
    assert user is not None
    return user


def _login(
    db: Session, email: str = "foo@example.com", pw: str = PW, now: datetime = NOW
) -> auth.LoginResult:
    return auth.login(db, email, pw, IP, "pytest-agent", CFG, now)


def _count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


# ---- 密码 ----


def test_password_hash_and_verify() -> None:
    hashed = auth.hash_password(PW)
    assert hashed != PW
    assert auth.verify_password(hashed, PW)
    assert not auth.verify_password(hashed, PW + "x")


def test_verify_password_none_is_false() -> None:
    assert not auth.verify_password(None, "x")
    assert not auth.verify_password("not-a-hash", "x")


def test_password_rules() -> None:
    with pytest.raises(AuthError):
        auth.check_password_rules("a@example.com", "123456789", 10)
    auth.check_password_rules("a@example.com", "1234567890", 10)
    with pytest.raises(AuthError):
        auth.check_password_rules("longenough@example.com", " LongEnough@Example.com", 10)


def test_normalize_email() -> None:
    assert auth.normalize_email(" Foo@Example.com ") == "foo@example.com"


# ---- 注册 ----


def test_register_ok_stores_hashes_and_terms(db: Session) -> None:
    res = _register(db, " Foo@Example.com ")
    assert res.kind == "created"
    assert res.token is not None
    user = repo.get_user(db, res.user_id)
    assert user.email == "foo@example.com"
    assert user.password_hash is not None
    assert user.password_hash != PW
    assert user.terms_version == CFG.terms_version
    assert user.terms_accepted_at == NOW
    assert user.email_verified_at is None
    token_row = db.scalars(select(EmailToken)).one()
    assert token_row.kind == "verify"
    assert token_row.token_hash != res.token
    assert token_row.expires_at == NOW + timedelta(hours=48)
    invite = db.scalars(select(Invite)).one()
    assert invite.used_by == user.id
    assert invite.used_at == NOW


def test_register_requires_terms(db: Session) -> None:
    code = _invite(db, _admin(db))
    with pytest.raises(AuthError, match="条款"):
        auth.register(db, "foo@example.com", PW, code, False, CFG, IP, NOW)


def test_register_bad_invites(db: Session) -> None:
    admin = _admin(db)
    with pytest.raises(AuthError):
        auth.register(db, "foo@example.com", PW, "nope", True, CFG, IP, NOW)
    expired = _invite(db, admin)
    with pytest.raises(AuthError):
        auth.register(db, "foo@example.com", PW, expired, True, CFG, IP, NOW + timedelta(days=15))
    revoked = auth.create_invite(db, admin.id, "x", 14, NOW)
    auth.revoke_invite(db, admin.id, revoked.id, NOW)
    with pytest.raises(AuthError):
        auth.register(db, "foo@example.com", PW, revoked.code, True, CFG, IP, NOW)
    used = _invite(db, admin)
    auth.register(db, "first@example.com", PW, used, True, CFG, IP, NOW)
    with pytest.raises(AuthError):
        auth.register(db, "second@example.com", PW, used, True, CFG, IP, NOW)


def test_register_bad_password(db: Session) -> None:
    code = _invite(db, _admin(db))
    with pytest.raises(AuthError):
        auth.register(db, "foo@example.com", "short", code, True, CFG, IP, NOW)
    with pytest.raises(AuthError):
        auth.register(
            db, "longenough@example.com", "longenough@example.com", code, True, CFG, IP, NOW
        )
    assert db.scalars(select(Invite)).one().used_by is None


def test_register_existing_email_keeps_invite(db: Session) -> None:
    first = _register(db)
    code = _invite(db, _admin_once(db))
    again = auth.register(db, "FOO@example.com", PW, code, True, CFG, IP, NOW)
    assert again.kind == "exists"
    assert again.user_id == first.user_id
    assert again.token is None
    assert db.scalars(select(Invite).where(Invite.code == code)).one().used_by is None


def test_existing_email_does_not_hide_bad_invite(db: Session) -> None:
    _register(db)
    with pytest.raises(AuthError):
        auth.register(db, "foo@example.com", PW, "bogus", True, CFG, IP, NOW)


def test_register_rate_limit_per_ip(db: Session) -> None:
    admin = _admin(db)
    for i in range(5):
        auth.register(db, f"u{i}@example.com", PW, _invite(db, admin), True, CFG, IP, NOW)
    with pytest.raises(RateLimitedError, match="操作太频繁"):
        auth.register(db, "u9@example.com", PW, _invite(db, admin), True, CFG, IP, NOW)
    auth.register(db, "u9@example.com", PW, _invite(db, admin), True, CFG, "198.51.100.1", NOW)


def test_register_signup_mode(db: Session) -> None:
    open_cfg = WebConfig(signup_mode="open")
    res = auth.register(db, "foo@example.com", PW, "", True, open_cfg, IP, NOW)
    assert res.kind == "created"
    with pytest.raises(AuthError):
        auth.register(db, "bar@example.com", PW, "", True, CFG, IP, NOW)


def test_register_invalid_email_format(db: Session) -> None:
    code = _invite(db, _admin(db))
    with pytest.raises(AuthError):
        auth.register(db, "not-an-email", PW, code, True, CFG, IP, NOW)
    assert db.scalars(select(Invite)).one().used_by is None


# ---- 邮件令牌 ----


def test_verify_token_valid_48h_once(db: Session) -> None:
    res = _register(db)
    assert res.token is not None
    assert auth.peek_email_token(db, res.token, "verify", NOW) == res.user_id
    assert auth.peek_email_token(db, res.token, "verify", NOW) == res.user_id  # 不消耗
    assert auth.peek_email_token(db, res.token, "reset", NOW) is None
    user = auth.verify_email(db, res.token, NOW + timedelta(hours=47))
    assert user is not None
    assert user.email_verified_at == NOW + timedelta(hours=47)
    assert auth.verify_email(db, res.token, NOW + timedelta(hours=47)) is None


def test_verify_token_expires(db: Session) -> None:
    res = _register(db)
    assert res.token is not None
    assert auth.verify_email(db, res.token, NOW + timedelta(hours=48, seconds=1)) is None
    assert auth.verify_email(db, "garbage", NOW) is None


def test_resend_invalidates_old_token(db: Session) -> None:
    res = _register(db)
    assert res.token is not None
    new = auth.resend_verification(db, res.user_id, CFG, NOW)
    assert new is not None
    assert new != res.token
    assert auth.verify_email(db, res.token, NOW) is None
    assert auth.verify_email(db, new, NOW) is not None
    assert auth.resend_verification(db, res.user_id, CFG, NOW) is None  # 已验证


def test_resend_rate_limited(db: Session) -> None:
    res = _register(db)
    for _ in range(3):
        assert auth.resend_verification(db, res.user_id, CFG, NOW) is not None
    with pytest.raises(RateLimitedError):
        auth.resend_verification(db, res.user_id, CFG, NOW)


def test_token_hashes_only_in_db(db: Session) -> None:
    res = _register(db)
    _verified_user(db, "bar@example.com")
    login = _login(db, "bar@example.com")
    assert res.token is not None
    hashes = {t.token_hash for t in db.scalars(select(EmailToken))}
    assert res.token not in hashes
    assert login.token not in {w.token_hash for w in db.scalars(select(WebSession))}
    assert login.session.token_hash != login.token


# ---- 登录 ----


def test_login_ok_creates_session(db: Session) -> None:
    _verified_user(db)
    res = _login(db, " FOO@example.com ")
    assert res.user.email == "foo@example.com"
    assert res.session.expires_at == NOW + timedelta(days=30)
    assert res.session.ip == IP
    assert res.session.user_agent == "pytest-agent"
    assert res.session.csrf_token
    assert _count(db, WebSession) == 1


def test_login_failure_messages_identical(db: Session) -> None:
    admin = _admin(db)
    user = _verified_user(db)
    disabled = _verified_user(db, "off@example.com")
    auth.set_user_status(db, admin.id, disabled.id, "disabled", NOW)
    messages: list[str] = []
    for email, pw in (
        ("foo@example.com", "wrong-password!"),
        ("ghost@example.com", PW),
        ("off@example.com", PW),
    ):
        with pytest.raises(AuthError) as exc:
            _login(db, email, pw)
        assert not isinstance(exc.value, (RateLimitedError, EmailNotVerifiedError))
        messages.append(str(exc.value))
    assert messages == [LOGIN_FAIL] * 3
    assert user.id


def test_login_unverified(db: Session) -> None:
    res = _register(db)
    with pytest.raises(EmailNotVerifiedError) as exc:
        _login(db)
    assert exc.value.user_id == res.user_id
    assert _count(db, WebSession) == 0


def test_login_rate_limit_by_email_and_recovery(db: Session) -> None:
    _verified_user(db)
    for _ in range(5):
        with pytest.raises(AuthError, match=LOGIN_FAIL):
            _login(db, pw="wrong-password!")
    with pytest.raises(RateLimitedError):
        _login(db)  # 即使密码正确
    assert _count(db, WebSession) == 0
    assert _login(db, now=NOW + timedelta(minutes=15, seconds=1)).token


def test_login_success_not_counted(db: Session) -> None:
    _verified_user(db)
    for _ in range(8):
        _login(db)
    assert _count(db, RateEvent) == 1  # 只有注册那次的 IP 计数


def test_login_rate_limit_by_ip(db: Session) -> None:
    for i in range(20):
        with pytest.raises(AuthError):
            auth.login(db, f"ghost{i}@example.com", PW, IP, "ua", CFG, NOW)
    with pytest.raises(RateLimitedError):
        auth.login(db, "another@example.com", PW, IP, "ua", CFG, NOW)


# ---- 会话 ----


def test_session_expires_after_30_days(db: Session) -> None:
    _verified_user(db)
    res = _login(db)
    assert auth.load_session(db, res.token, CFG, NOW + timedelta(days=30, seconds=1)) is None
    assert auth.load_session(db, "unknown", CFG, NOW) is None


def test_session_slides_with_activity_and_write_throttle(db: Session) -> None:
    _verified_user(db)
    res = _login(db)
    t1 = NOW + timedelta(minutes=2)
    loaded = auth.load_session(db, res.token, CFG, t1)
    assert loaded is not None
    assert loaded[0].last_seen_at == NOW  # 不足 5 分钟不写
    t2 = NOW + timedelta(days=20)
    loaded = auth.load_session(db, res.token, CFG, t2)
    assert loaded is not None
    assert loaded[0].last_seen_at == t2
    assert loaded[0].expires_at == t2 + timedelta(days=30)
    assert auth.load_session(db, res.token, CFG, NOW + timedelta(days=45)) is not None


def test_session_dies_when_user_disabled(db: Session) -> None:
    admin = _admin(db)
    user = _verified_user(db)
    res = _login(db)
    auth.set_user_status(db, admin.id, user.id, "disabled", NOW)
    assert auth.load_session(db, res.token, CFG, NOW) is None


def test_logout_and_logout_all(db: Session) -> None:
    user = _verified_user(db)
    a = _login(db)
    b = _login(db)
    auth.logout(db, a.token)
    assert auth.load_session(db, a.token, CFG, NOW) is None
    assert auth.load_session(db, b.token, CFG, NOW) is not None
    auth.logout(db, a.token)  # 重复退出无事发生
    assert auth.logout_all(db, user.id) == 1
    assert _count(db, WebSession) == 0


# ---- 找回密码 ----


def test_reset_unknown_or_disabled_returns_none(db: Session) -> None:
    admin = _admin(db)
    assert auth.request_password_reset(db, "ghost@example.com", CFG, NOW) is None
    user = _verified_user(db)
    auth.set_user_status(db, admin.id, user.id, "disabled", NOW)
    assert auth.request_password_reset(db, "foo@example.com", CFG, NOW) is None


def test_reset_rate_limit_returns_none(db: Session) -> None:
    _verified_user(db)
    for _ in range(3):
        assert auth.request_password_reset(db, "foo@example.com", CFG, NOW) is not None
    assert auth.request_password_reset(db, "Foo@example.com", CFG, NOW) is None


def test_reset_token_expires_after_1h(db: Session) -> None:
    _verified_user(db)
    got = auth.request_password_reset(db, "foo@example.com", CFG, NOW)
    assert got is not None
    _, token = got
    with pytest.raises(AuthError):
        auth.reset_password(
            db, token, "brand-new-password", CFG, NOW + timedelta(hours=1, seconds=1)
        )


def test_reset_password_success(db: Session) -> None:
    res = _register(db)  # 未验证
    got = auth.request_password_reset(db, "foo@example.com", CFG, NOW)
    assert got is not None
    user, token = got
    assert user.id == res.user_id
    other = _register(db, "bar@example.com")
    assert other.token is not None
    auth.verify_email(db, other.token, NOW)
    session = _login(db, "bar@example.com")  # 别人的会话不受影响
    # 给 foo 造一个会话：先验证再登录，再重置
    assert res.token is not None
    auth.verify_email(db, res.token, NOW)
    foo_session = _login(db)
    new_pw = "brand-new-password"
    done = auth.reset_password(db, token, new_pw, CFG, NOW + timedelta(minutes=5))
    assert done.id == user.id
    assert auth.load_session(db, foo_session.token, CFG, NOW) is None
    assert auth.load_session(db, session.token, CFG, NOW) is not None
    with pytest.raises(AuthError, match=LOGIN_FAIL):
        _login(db, pw=PW)
    assert _login(db, pw=new_pw).token
    with pytest.raises(AuthError):  # 只能用一次
        auth.reset_password(db, token, "another-new-password", CFG, NOW)


def test_reset_verifies_unverified_account(db: Session) -> None:
    _register(db)
    got = auth.request_password_reset(db, "foo@example.com", CFG, NOW)
    assert got is not None
    user = auth.reset_password(db, got[1], "brand-new-password", CFG, NOW)
    assert user.email_verified_at == NOW


def test_reset_weak_password_does_not_burn_token(db: Session) -> None:
    _verified_user(db)
    got = auth.request_password_reset(db, "foo@example.com", CFG, NOW)
    assert got is not None
    with pytest.raises(AuthError):
        auth.reset_password(db, got[1], "short", CFG, NOW)
    assert auth.reset_password(db, got[1], "brand-new-password", CFG, NOW)


# ---- 改密码 ----


def test_change_password(db: Session) -> None:
    user = _verified_user(db)
    keep = _login(db)
    other = _login(db)
    with pytest.raises(AuthError):
        auth.change_password(
            db, user.id, "wrong-password!", "brand-new-password", CFG, keep.session.id, NOW
        )
    assert auth.load_session(db, other.token, CFG, NOW) is not None
    auth.change_password(db, user.id, PW, "brand-new-password", CFG, keep.session.id, NOW)
    assert auth.load_session(db, keep.token, CFG, NOW) is not None
    assert auth.load_session(db, other.token, CFG, NOW) is None
    assert _login(db, pw="brand-new-password").token


# ---- 停用 ----


def test_set_user_status(db: Session) -> None:
    admin = _admin(db)
    user = _verified_user(db)
    with pytest.raises(AuthError):
        auth.set_user_status(db, admin.id, admin.id, "disabled", NOW)
    _login(db)
    updated = auth.set_user_status(db, admin.id, user.id, "disabled", NOW)
    assert updated.status == "disabled"
    assert _count(db, WebSession) == 0
    assert auth.set_user_status(db, admin.id, user.id, "active", NOW).status == "active"


# ---- 条款 ----


def test_terms(db: Session) -> None:
    user = _verified_user(db)
    assert not auth.needs_terms(user, CFG)
    newer = WebConfig(terms_version="2027-01")
    assert auth.needs_terms(user, newer)
    auth.accept_terms(db, user.id, newer, NOW, web_actor(user.id))
    assert not auth.needs_terms(user, newer)
    assert user.terms_accepted_at == NOW


# ---- 邀请码 ----


def test_invites(db: Session) -> None:
    admin = _admin(db)
    inv = auth.create_invite(db, admin.id, " 小王 ", 7, NOW)
    assert inv.note == "小王"
    assert inv.expires_at == NOW + timedelta(days=7)
    assert auth.revoke_invite(db, admin.id, inv.id, NOW).revoked_at == NOW
    auth.revoke_invite(db, admin.id, inv.id, NOW + timedelta(hours=1))  # 重复作废不改时间
    assert inv.revoked_at == NOW
    assert [i.id for i in admin_repo.list_invites(db)] == [inv.id]
    with pytest.raises(NotFoundError):
        auth.revoke_invite(db, admin.id, 9999, NOW)
    used = _invite(db, admin)
    auth.register(db, "x@example.com", PW, used, True, CFG, IP, NOW)
    used_row = db.scalars(select(Invite).where(Invite.code == used)).one()
    with pytest.raises(AuthError):
        auth.revoke_invite(db, admin.id, used_row.id, NOW)


# ---- 清理 ----


def test_purge_expired(db: Session) -> None:
    _verified_user(db)
    _login(db)
    auth.rate_hit(db, "k", NOW - timedelta(days=3))
    auth.rate_hit(db, "k", NOW - timedelta(days=1))
    later = NOW + timedelta(days=31)
    removed = auth.purge_expired(db, later)
    assert removed >= 3  # 会话、令牌（验证令牌已过期）、旧频率记录
    assert _count(db, WebSession) == 0
    assert _count(db, EmailToken) == 0
    assert _count(db, RateEvent) == 0
    assert auth.purge_expired(db, later) == 0


def test_purge_keeps_live_rows(db: Session) -> None:
    _verified_user(db)
    _login(db)
    auth.rate_hit(db, "k", NOW)
    auth.purge_expired(db, NOW + timedelta(days=1))
    assert _count(db, WebSession) == 1
    assert _count(db, RateEvent) >= 1


# ---- 审计 ----


def _audits(db: Session, action: str) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


def test_audit_rows(db: Session) -> None:
    user = _verified_user(db)
    with pytest.raises(AuthError):
        _login(db, "ghost@example.com", "secret-pass-123")
    with pytest.raises(AuthError):
        _login(db, pw="wrong-password!")
    _login(db)
    (reg,) = _audits(db, "register")
    assert (reg.channel, reg.actor_user_id, reg.detail["user_id"]) == ("web", user.id, user.id)
    ghost, known = _audits(db, "login_failed")
    assert ghost.channel == "web"
    assert ghost.actor_user_id is None
    assert "user_id" not in ghost.detail
    assert ghost.detail["email"] == "ghost@example.com"
    assert ghost.detail["ip"] == IP
    assert known.detail["user_id"] == user.id
    assert "secret-pass-123" not in str(ghost.detail)
    assert "wrong-password!" not in str(known.detail)
    (ok,) = _audits(db, "login_ok")
    assert ok.actor_user_id == user.id
    assert _audits(db, "email_verify")


def test_audit_without_subject_user_omits_key(db: Session) -> None:
    entry = repo.add_audit(db, None, "x", "t", {"a": 1}, Actor(None, "web"))
    assert entry.detail == {"a": 1}
