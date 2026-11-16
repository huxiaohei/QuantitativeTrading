"""网页测试夹具：真实数据库、写文件的发信器、可前拨的时钟、测试客户端与登录辅助函数。"""

import email
import email.policy
import re
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.config import AppConfig, load_config
from qt.data.store import ParquetStore
from qt.db import admin_repo
from qt.db.models import Subscription, User
from qt.mail.send import FileSender
from qt.service import auth
from qt.service.accounts import create_subscription, create_user
from qt.service.actor import CLI
from qt.service.market import StoreMarket, build_store_market
from qt.web.app import create_app
from qt.web.deps import WebDeps
from tests.fakes import populate_trading_store
from tests.jobs.conftest import CASH, DAY, Env, FakeClock, evening

CFG: AppConfig = load_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
BASE = "https://quant.test"
PW = "correct-horse-battery"
SH = ZoneInfo("Asia/Shanghai")
START = datetime(2026, 11, 16, 9, 0, tzinfo=SH)
ADMIN_EMAIL = "admin@example.com"

_CSRF = re.compile(r'name="csrf" value="([^"]+)"')


@dataclass
class Clock:
    """可前拨的固定时钟。"""

    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def clock() -> Clock:
    return Clock(START)


@pytest.fixture
def mail_dir(tmp_path: Path) -> Path:
    return tmp_path / "mails"


def _calendar() -> TradingCalendar:
    return TradingCalendar([date(2026, 11, 16)])


def small_market(
    store: ParquetStore, calendar: Callable[[], TradingCalendar] = _calendar
) -> Callable[[date, Collection[str]], StoreMarket]:
    """用小夹具数据现建行情的函数（测试里不缓存）。"""

    def _market(today: date, symbols: Collection[str]) -> StoreMarket:
        return build_store_market(store, CFG, calendar(), today, symbols)

    return _market


def make_deps(
    engine: Engine,
    tmp_path: Path,
    clock: Clock,
    mail_dir: Path,
    base_url: str = BASE,
    calendar: Callable[[], TradingCalendar] = _calendar,
) -> WebDeps:
    """按测试需要构造 WebDeps；后台线程关闭。calendar 缺省是只有 2026-11-16 一天的日历。"""
    store = ParquetStore(tmp_path / "data")
    return WebDeps(
        engine=engine,
        cfg=CFG,
        base_url=base_url,
        sender=FileSender(mail_dir),
        now=clock,
        store=store,
        calendar=calendar,
        market=small_market(store, calendar),
        background=False,
    )


class World:
    """测试数据：交易日历、行情存储与停在第二晚的时钟（2A 的每日任务夹具在 env 里）。"""

    def __init__(self, tmp_path: Path, engine: Engine) -> None:
        self.store = ParquetStore(tmp_path / "data")
        self.days = populate_trading_store(self.store)
        self.calendar = TradingCalendar(self.days)
        self.clock = Clock(evening(self.days[DAY + 1]))
        self.env = Env(engine, self.store, self.days, FakeClock(evening(self.days[DAY])))


@pytest.fixture
def world(tmp_path: Path, db_engine: Engine, db: Session) -> World:
    # 依赖 db 夹具：测试结束后清空所有表
    del db
    return World(tmp_path, db_engine)


def world_deps(world: World, engine: Engine, tmp_path: Path) -> WebDeps:
    """用 World 的交易日历、行情存储（同一目录）与时钟构造 WebDeps。"""
    return make_deps(
        engine, tmp_path, world.clock, tmp_path / "mails", calendar=lambda: world.calendar
    )


def subscribe(db: Session, world: World, user_id: int) -> Subscription:
    """给使用者建订阅：起始资金 CASH，起始日 days[DAY]。"""
    sub = create_subscription(db, user_id, CFG, CASH, world.days[DAY], None, actor=CLI)
    db.commit()
    return sub


@pytest.fixture
def web_deps(
    db: Session, db_engine: Engine, tmp_path: Path, clock: Clock, mail_dir: Path
) -> WebDeps:
    # 依赖 db 夹具：测试结束后清空所有表
    del db
    return make_deps(db_engine, tmp_path, clock, mail_dir)


@pytest.fixture
def client(web_deps: WebDeps) -> Iterator[TestClient]:
    with TestClient(create_app(web_deps), base_url=BASE) as c:
        yield c


MakeUser = Callable[..., User]


@pytest.fixture
def make_user(db_engine: Engine, clock: Clock) -> MakeUser:
    """建一个有密码的使用者；默认已验证邮箱、已同意当前条款。"""

    def _make(
        email_addr: str,
        verified: bool = True,
        admin: bool = False,
        password: str = PW,
        terms_version: str | None = CFG.web.terms_version,
    ) -> User:
        with Session(db_engine, expire_on_commit=False) as s:
            user = create_user(s, email_addr, is_admin=admin, actor=CLI)
            user.password_hash = auth.hash_password(password)
            user.email_verified_at = clock.now if verified else None
            user.terms_version = terms_version
            user.terms_accepted_at = clock.now
            s.commit()
            return user

    return _make


@pytest.fixture
def invite_code(db_engine: Engine, clock: Clock) -> Callable[[], str]:
    """生成一个可用的邀请码（首次调用时建管理员）。"""

    def _make() -> str:
        with Session(db_engine) as s:
            admin = admin_repo.find_user_by_email(s, ADMIN_EMAIL)
            if admin is None:
                admin = create_user(s, ADMIN_EMAIL, is_admin=True, actor=CLI)
            code = auth.create_invite(s, admin.id, "测试", 14, clock.now).code
            s.commit()
            return code

    return _make


def csrf_of(response: httpx.Response) -> str:
    """取页面表单里的防伪造令牌。"""
    found = _CSRF.search(response.text)
    assert found is not None, "页面里没有 csrf 字段"
    return found.group(1)


def login_as(client: TestClient, email_addr: str, password: str = PW) -> httpx.Response:
    """走登录页登录，返回登录 POST 的响应（不跟随跳转）。"""
    page = client.get("/login")
    return client.post(
        "/login",
        data={"email": email_addr, "password": password, "csrf": csrf_of(page)},
        follow_redirects=False,
    )


def read_mails(mail_dir: Path, to_addr: str) -> list[EmailMessage]:
    """按发出顺序读出发给某地址的全部邮件。"""
    found: list[EmailMessage] = []
    for path in sorted(mail_dir.glob("*.eml")):
        msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        assert isinstance(msg, EmailMessage)
        if msg["To"] == to_addr:
            found.append(msg)
    return found


def mail_text(msg: EmailMessage) -> str:
    body = msg.get_body(preferencelist=("plain",))
    assert isinstance(body, EmailMessage)
    content = body.get_content()
    assert isinstance(content, str)
    return content


@pytest.fixture
def last_mail(mail_dir: Path) -> Callable[[str], str]:
    """最后一封发给某地址的邮件正文（纯文本）。"""

    def _last(to_addr: str) -> str:
        mails = read_mails(mail_dir, to_addr)
        assert mails, f"没有发给 {to_addr} 的邮件"
        return mail_text(mails[-1])

    return _last


def link_path(text: str, path: str) -> str:
    """从邮件正文里取出以 {BASE}{path} 开头的链接，返回站内路径（含查询串）。"""
    found = re.search(re.escape(BASE + path) + r"\S*", text)
    assert found is not None, f"邮件里没有 {path} 链接"
    return found.group(0)[len(BASE) :]
