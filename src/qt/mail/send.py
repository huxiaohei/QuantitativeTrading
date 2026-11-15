"""邮件发送器：SMTP 与文件两种后端，由环境变量选择。

密码只从环境变量读取，不写日志、不进异常信息。
"""

import os
import smtplib
import time
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

_SMTP_TIMEOUT_SECONDS = 30
_SSL_PORT = 465
_DEFAULT_FILE_DIR = "mail_out"


class Sender(Protocol):
    """发送一封邮件；失败时抛异常。"""

    def send(self, to_addr: str, subject: str, html: str, text: str) -> None: ...


def build_message(from_addr: str, to_addr: str, subject: str, html: str, text: str) -> EmailMessage:
    """纯文本为主、HTML 为替代的多部分邮件。"""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(text, charset="utf-8")
    msg.add_alternative(html, subtype="html", charset="utf-8")
    return msg


class SmtpSender:
    """标准库 smtplib 发送：465 端口用 SSL，其余端口用 STARTTLS。"""

    def __init__(self, host: str, port: int, user: str, password: str, from_addr: str) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from = from_addr

    def send(self, to_addr: str, subject: str, html: str, text: str) -> None:
        msg = build_message(self._from, to_addr, subject, html, text)
        if self._port == _SSL_PORT:
            with smtplib.SMTP_SSL(self._host, self._port, timeout=_SMTP_TIMEOUT_SECONDS) as smtp:
                smtp.login(self._user, self._password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self._host, self._port, timeout=_SMTP_TIMEOUT_SECONDS) as smtp:
                smtp.starttls()
                smtp.login(self._user, self._password)
                smtp.send_message(msg)


class FileSender:
    """每封邮件写成目录下的一个 .eml 文件，用于本地试运行。"""

    def __init__(self, directory: Path, from_addr: str = "qt@localhost") -> None:
        self._dir = directory
        self._from = from_addr

    def send(self, to_addr: str, subject: str, html: str, text: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        msg = build_message(self._from, to_addr, subject, html, text)
        name = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.eml"
        (self._dir / name).write_bytes(bytes(msg))


def make_sender() -> Sender:
    """按 MAIL_BACKEND（smtp 或 file，默认 smtp）创建发送器。"""
    backend = os.environ.get("MAIL_BACKEND", "smtp").strip().lower()
    if backend == "file":
        directory = Path(os.environ.get("MAIL_FILE_DIR") or _DEFAULT_FILE_DIR)
        return FileSender(directory, os.environ.get("MAIL_FROM") or "qt@localhost")
    if backend != "smtp":
        raise RuntimeError(f"MAIL_BACKEND 取值无效：{backend!r}，只能是 smtp 或 file")
    names = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM")
    values = {n: os.environ.get(n, "").strip() for n in names}
    missing = [n for n in names if not values[n]]
    if missing:
        raise RuntimeError(f"缺少邮件环境变量：{'、'.join(missing)}")
    try:
        port = int(values["SMTP_PORT"])
    except ValueError:
        raise RuntimeError("环境变量 SMTP_PORT 必须是整数") from None
    return SmtpSender(
        values["SMTP_HOST"], port, values["SMTP_USER"], values["SMTP_PASSWORD"], values["MAIL_FROM"]
    )
