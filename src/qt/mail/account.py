"""账号类邮件：验证邮箱、重置密码、重复注册提示、密码已修改。

令牌只出现在邮件正文的链接里，不写日志，也不进去重键等其他列；邮件不含密码。
链接只由调用方传入的 base_url 拼出，这里不写死任何域名。
"""

from datetime import datetime
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.db.models import Outbox, User
from qt.mail.outbox import enqueue
from qt.mail.render import Mail, render_html

ACCOUNT_KINDS: frozenset[str] = frozenset(
    {"account_verify", "account_reset", "account_register_attempt", "account_password_changed"}
)
_IGNORE = "如果不是你本人操作，忽略即可。"


def _link(base_url: str, path: str, token: str | None = None) -> str:
    base = base_url.rstrip("/")
    return f"{base}/{path}" if token is None else f"{base}/{path}?token={quote(token, safe='')}"


def _html(template: str, subject: str, **fields: object) -> str:
    return render_html(template, subject=subject, disclaimer=None, **fields)


def render_verify(base_url: str, token: str, hours: int) -> Mail:
    """验证邮箱的邮件，链接 {base_url}/verify?token=…。"""
    subject = "【验证邮箱】请确认这个邮箱属于你"
    link = _link(base_url, "verify", token)
    intro = "请打开下面的链接，确认这个邮箱属于你。"
    text = f"{subject}\n\n{intro}\n{link}\n\n链接 {hours} 小时内有效，只能使用一次。\n{_IGNORE}\n"
    html = _html("account_verify.html.j2", subject, intro=intro, link=link, hours=hours)
    return Mail(subject, html, text)


def render_reset(base_url: str, token: str, hours: int) -> Mail:
    """重置密码的邮件，链接 {base_url}/reset?token=…。"""
    subject = "【重置密码】设置新密码的链接"
    link = _link(base_url, "reset", token)
    intro = "有人申请重置这个账号的密码。请打开下面的链接设置新密码。"
    text = f"{subject}\n\n{intro}\n{link}\n\n链接 {hours} 小时内有效，只能使用一次。\n{_IGNORE}\n"
    html = _html("account_reset.html.j2", subject, intro=intro, link=link, hours=hours)
    return Mail(subject, html, text)


def render_register_attempt(base_url: str) -> Mail:
    """邮箱已注册时有人再次注册，提示本人直接登录。"""
    subject = "【账号提示】这个邮箱已经注册过"
    link = _link(base_url, "login")
    text = (
        f"{subject}\n\n有人用这个邮箱尝试注册，但这个邮箱已经注册过了，所以没有创建新账号。\n"
        f"如果是你本人操作，直接登录即可；忘记密码可以在登录页找回：\n{link}\n\n{_IGNORE}\n"
    )
    return Mail(subject, _html("account_register_attempt.html.j2", subject, link=link), text)


def render_password_changed(base_url: str, at: datetime, *, kept_current: bool) -> Mail:
    """密码已修改的通知，不含密码。at 是北京时间。

    kept_current 为真表示登录后在设置页修改（当前浏览器的登录保留），
    为假表示通过找回密码重置（全部登录都已失效）。
    """
    subject = "【账号提示】你的密码已修改"
    link = _link(base_url, "forgot")
    when = at.strftime("%Y-%m-%d %H:%M") + "（北京时间）"
    effect = "其他设备需要重新登录，当前浏览器不受影响" if kept_current else "所有设备需要重新登录"
    text = (
        f"{subject}\n\n你的账号密码已于 {when} 修改，{effect}。\n"
        f"如果不是你本人操作，请立即通过下面的链接找回密码：\n{link}\n\n"
        "如果是你本人操作，忽略即可。\n"
    )
    html = _html("account_password_changed.html.j2", subject, link=link, at=when, effect=effect)
    return Mail(subject, html, text)


def queue_account_mail(s: Session, user: User, kind: str, mail: Mail, now: datetime) -> int | None:
    """账号邮件入队，去重键 "{kind}:{用户编号}:{now 的 ISO 时间}"；返回新行的 outbox.id。

    去重键已存在（同一时刻重复入队）返回 None。只 flush、不 commit。
    """
    key = f"{kind}:{user.id}:{now.isoformat()}"
    if not enqueue(s, user.id, user.email, kind, key, mail.subject, mail.html, mail.text):
        return None
    return s.scalars(select(Outbox.id).where(Outbox.dedupe_key == key)).one()
