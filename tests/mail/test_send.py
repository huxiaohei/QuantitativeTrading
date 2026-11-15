"""发送器：文件后端、SMTP（假 smtplib）与环境变量选择。"""

import email
import email.message
import email.policy
import ssl
from pathlib import Path
from types import TracebackType
from typing import ClassVar, Self

import pytest

from qt.mail import send as send_module
from qt.mail.send import FileSender, SmtpSender, make_sender

_ENV_NAMES = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "MAIL_FROM",
    "MAIL_BACKEND",
    "MAIL_FILE_DIR",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_file_sender_writes_eml(tmp_path: Path) -> None:
    sender = FileSender(tmp_path / "out", from_addr="qt@example.com")
    sender.send("u@example.com", "【买入信号】2026-11-16 1 只", "<p>你好</p>", "你好")
    sender.send("u@example.com", "第二封", "<p>b</p>", "b")
    files = sorted((tmp_path / "out").glob("*.eml"))
    assert len(files) == 2
    msg = email.message_from_bytes(files[0].read_bytes(), policy=email.policy.default)
    assert msg["To"] == "u@example.com"
    assert msg["From"] == "qt@example.com"
    assert msg["Subject"] == "【买入信号】2026-11-16 1 只"
    text = msg.get_body(("plain",))
    html = msg.get_body(("html",))
    assert text is not None and html is not None
    assert "你好" in text.get_content()
    assert "<p>你好</p>" in html.get_content()


class FakeSmtp:
    """记录交互的假 smtplib 连接。"""

    instances: ClassVar[list["FakeSmtp"]] = []

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> None:
        self.host, self.port = host, port
        self.context = context
        self.starttls_context: ssl.SSLContext | None = None
        self.calls: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.sent: list[email.message.EmailMessage] = []
        FakeSmtp.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def starttls(self, context: ssl.SSLContext | None = None) -> None:
        self.calls.append("starttls")
        self.starttls_context = context

    def login(self, user: str, password: str) -> None:
        self.calls.append("login")
        self.login_args = (user, password)

    def send_message(self, msg: email.message.EmailMessage) -> None:
        self.calls.append("send")
        self.sent.append(msg)


class FakeSmtpSsl(FakeSmtp):
    pass


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSmtp]:
    FakeSmtp.instances = []
    monkeypatch.setattr(send_module.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(send_module.smtplib, "SMTP_SSL", FakeSmtpSsl)
    return FakeSmtp


def test_smtp_sender_uses_starttls_on_587(fake_smtp: type[FakeSmtp]) -> None:
    SmtpSender("smtp.x.com", 587, "u", "pw", "from@x.com").send("to@x.com", "主题", "<p>h</p>", "t")
    (conn,) = fake_smtp.instances
    assert not isinstance(conn, FakeSmtpSsl)
    assert conn.calls == ["starttls", "login", "send"]
    assert conn.login_args == ("u", "pw")
    assert conn.sent[0]["To"] == "to@x.com"
    assert conn.sent[0]["Subject"] == "主题"


def test_smtp_sender_uses_ssl_on_465(fake_smtp: type[FakeSmtp]) -> None:
    SmtpSender("smtp.x.com", 465, "u", "pw", "from@x.com").send("to@x.com", "主题", "<p>h</p>", "t")
    (conn,) = fake_smtp.instances
    assert isinstance(conn, FakeSmtpSsl)
    assert conn.calls == ["login", "send"]


def test_make_sender_missing_smtp_settings_raises_chinese_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.x.com")
    monkeypatch.setenv("SMTP_PASSWORD", "never-shown")
    with pytest.raises(RuntimeError) as info:
        make_sender()
    message = str(info.value)
    assert "SMTP_PORT" in message and "SMTP_USER" in message and "MAIL_FROM" in message
    assert "缺少" in message
    assert "never-shown" not in message


def test_make_sender_smtp_from_env(
    monkeypatch: pytest.MonkeyPatch, fake_smtp: type[FakeSmtp]
) -> None:
    monkeypatch.setenv("MAIL_BACKEND", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.x.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_FROM", "from@x.com")
    sender = make_sender()
    assert isinstance(sender, SmtpSender)
    sender.send("to@x.com", "s", "<p>h</p>", "t")
    assert fake_smtp.instances[0].port == 465


def test_make_sender_bad_port_raises_chinese_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "h")
    monkeypatch.setenv("SMTP_PORT", "abc")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_FROM", "f@x.com")
    with pytest.raises(RuntimeError, match="SMTP_PORT"):
        make_sender()


def test_make_sender_file_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAIL_BACKEND", "file")
    monkeypatch.setenv("MAIL_FILE_DIR", str(tmp_path / "mails"))
    sender = make_sender()
    assert isinstance(sender, FileSender)
    sender.send("to@x.com", "s", "<p>h</p>", "t")
    assert len(list((tmp_path / "mails").glob("*.eml"))) == 1


def test_make_sender_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_BACKEND", "carrier-pigeon")
    with pytest.raises(RuntimeError, match="MAIL_BACKEND"):
        make_sender()


def _assert_verifying(context: ssl.SSLContext | None) -> None:
    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_smtp_ssl_passes_verifying_context(fake_smtp: type[FakeSmtp]) -> None:
    SmtpSender("smtp.x.com", 465, "u", "pw", "from@x.com").send("to@x.com", "s", "<p>h</p>", "t")
    _assert_verifying(fake_smtp.instances[0].context)


def test_smtp_starttls_passes_verifying_context(fake_smtp: type[FakeSmtp]) -> None:
    SmtpSender("smtp.x.com", 587, "u", "pw", "from@x.com").send("to@x.com", "s", "<p>h</p>", "t")
    _assert_verifying(fake_smtp.instances[0].starttls_context)
