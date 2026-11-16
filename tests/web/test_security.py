"""网页安全：防伪造、来源检查、响应头、Cookie 属性、会话过期、错误页、来访 IP。

路由登记与访问类别的总表在 test_access.py。
"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import httpx2 as httpx
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from qt.web.app import create_app
from qt.web.deps import WebDeps, client_ip, require_admin, safe_next
from tests.web.conftest import BASE, PW, Clock, MakeUser, csrf_of, login_as, make_deps

CSP = "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"


def _cookie_headers(resp: httpx.Response) -> list[str]:
    return resp.headers.get_list("set-cookie")


def _session_cookie(resp: httpx.Response, name: str) -> str:
    found = [h for h in _cookie_headers(resp) if h.startswith(name + "=")]
    assert len(found) == 1, _cookie_headers(resp)
    return found[0]


# ---- 防伪造与来源 ----


def _login_data(page: httpx.Response) -> dict[str, str]:
    return {"email": "foo@example.com", "password": PW, "csrf": csrf_of(page)}


def test_public_post_without_csrf_is_403(client: TestClient) -> None:
    client.get("/login")
    resp = client.post("/login", data={"email": "foo@example.com", "password": PW})
    assert resp.status_code == 403
    assert "请求无效" in resp.text


def test_public_post_with_wrong_csrf_is_403(client: TestClient) -> None:
    page = client.get("/login")
    data = {**_login_data(page), "csrf": "x" + csrf_of(page)}
    assert client.post("/login", data=data).status_code == 403


def test_public_post_without_csrf_cookie_is_403(web_deps: WebDeps) -> None:
    with TestClient(create_app(web_deps), base_url=BASE) as first:
        page = first.get("/login")
    with TestClient(create_app(web_deps), base_url=BASE) as other:
        assert other.post("/login", data=_login_data(page)).status_code == 403


def test_foreign_origin_is_403(client: TestClient, make_user: MakeUser) -> None:
    make_user("foo@example.com")
    page = client.get("/login")
    bad = client.post("/login", data=_login_data(page), headers={"Origin": "https://evil.example"})
    assert bad.status_code == 403
    good = client.post(
        "/login", data=_login_data(page), headers={"Origin": BASE}, follow_redirects=False
    )
    assert good.status_code == 303


def test_logged_in_post_checks_session_csrf(client: TestClient, make_user: MakeUser) -> None:
    make_user("foo@example.com")
    login_as(client, "foo@example.com")
    page = client.get("/overview")
    assert client.post("/logout", data={}).status_code == 403
    assert client.post("/logout", data={"csrf": "wrong"}).status_code == 403
    # 未登录时的双重提交令牌不能代替会话令牌
    cookie_token = client.cookies.get("__Host-qt_csrf")
    assert cookie_token is not None
    assert client.post("/logout", data={"csrf": cookie_token}).status_code == 403
    ok = client.post("/logout", data={"csrf": csrf_of(page)}, follow_redirects=False)
    assert ok.status_code == 303


# ---- 响应头与 Cookie ----


@pytest.mark.parametrize("path", ["/login", "/register", "/nope", "/healthz"])
def test_security_headers(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.headers["content-security-policy"] == CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["strict-transport-security"].startswith("max-age=")


def test_static_files_are_cacheable(client: TestClient) -> None:
    resp = client.get("/static/app.css")
    assert resp.status_code == 200
    assert "no-store" not in resp.headers.get("cache-control", "")
    assert resp.headers["content-security-policy"] == CSP
    assert client.get("/static/htmx.min.js").status_code == 200


def test_session_cookie_flags(client: TestClient, make_user: MakeUser) -> None:
    make_user("foo@example.com")
    resp = login_as(client, "foo@example.com")
    cookie = _session_cookie(resp, "__Host-qt_session")
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    assert "domain=" not in lowered


def test_csrf_cookie_flags(client: TestClient) -> None:
    resp = client.get("/login")
    cookie = _session_cookie(resp, "__Host-qt_csrf").lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "path=/" in cookie


def test_http_localhost_cookies_have_no_host_prefix(
    db: Session,
    db_engine: Engine,
    tmp_path: Path,
    clock: Clock,
    mail_dir: Path,
    make_user: MakeUser,
) -> None:
    del db
    local = "http://localhost:8000"
    deps = make_deps(db_engine, tmp_path, clock, mail_dir, base_url=local)
    make_user("foo@example.com")
    with TestClient(create_app(deps), base_url=local) as c:
        page = c.get("/login")
        csrf_cookie = _session_cookie(page, "qt_csrf")
        assert "secure" not in csrf_cookie.lower()
        assert "strict-transport-security" not in page.headers
        resp = login_as(c, "foo@example.com")
        assert resp.status_code == 303
        cookie = _session_cookie(resp, "qt_session")
        assert "__Host-" not in cookie
        assert "secure" not in cookie.lower()
        assert c.get("/overview").status_code == 200


# ---- 会话过期 ----


def test_expired_session_post_redirects_to_login(
    client: TestClient, make_user: MakeUser, clock: Clock
) -> None:
    make_user("foo@example.com")
    login_as(client, "foo@example.com")
    page = client.get("/overview")
    clock.advance(timedelta(days=31))
    resp = client.post("/logout", data={"csrf": csrf_of(page)}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?msg=expired"
    assert "登录已过期" in client.get(resp.headers["location"]).text


def test_expired_session_get_redirects_with_next(
    client: TestClient, make_user: MakeUser, clock: Clock
) -> None:
    make_user("foo@example.com")
    login_as(client, "foo@example.com")
    clock.advance(timedelta(days=31))
    resp = client.get("/overview?a=1", follow_redirects=False)
    assert resp.headers["location"] == "/login?next=%2Foverview%3Fa%3D1"


def test_active_session_cookie_is_extended(
    client: TestClient, make_user: MakeUser, clock: Clock
) -> None:
    make_user("foo@example.com")
    login_as(client, "foo@example.com")
    clock.advance(timedelta(days=20))
    refreshed = client.get("/overview")
    assert refreshed.status_code == 200
    assert _session_cookie(refreshed, "__Host-qt_session")
    clock.advance(timedelta(days=20))
    assert client.get("/overview", follow_redirects=False).status_code == 200


# ---- 错误页 ----


def test_404_page_is_chinese(client: TestClient) -> None:
    resp = client.get("/no-such-page")
    assert resp.status_code == 404
    assert "页面不存在" in resp.text


def test_500_page_hides_exception(web_deps: WebDeps, caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(web_deps)

    def boom() -> str:
        raise RuntimeError("SecretBoomDetail")

    app.add_api_route("/boom", boom)
    with TestClient(app, base_url=BASE) as c:
        resp = c.get("/boom")
    assert resp.status_code == 500
    assert "RuntimeError" not in resp.text
    assert "SecretBoomDetail" not in resp.text
    assert "Traceback" not in resp.text
    assert "出错" in resp.text
    assert resp.headers["content-security-policy"] == CSP
    assert "SecretBoomDetail" in caplog.text


def test_oversized_body_is_rejected(client: TestClient) -> None:
    page = client.get("/login")
    data = {"email": "a" * 70_000, "password": PW, "csrf": csrf_of(page)}
    resp = client.post("/login", data=data)
    assert resp.status_code == 413
    assert "过大" in resp.text


def test_oversized_chunked_body_is_rejected(client: TestClient) -> None:
    page = client.get("/login")
    body = f"csrf={csrf_of(page)}&email=" + "a" * 70_000

    def chunks() -> Iterator[bytes]:
        raw = body.encode()
        for i in range(0, len(raw), 8192):
            yield raw[i : i + 8192]

    resp = client.post(
        "/login",
        content=chunks(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 413


def test_require_admin(web_deps: WebDeps, make_user: MakeUser) -> None:
    app = create_app(web_deps)

    def secret() -> str:
        return "admin-only"

    app.add_api_route("/admin-test", secret, dependencies=[Depends(require_admin)])
    make_user("foo@example.com")
    make_user("boss@example.com", admin=True)
    with TestClient(app, base_url=BASE) as c:
        assert c.get("/admin-test", follow_redirects=False).status_code == 303
        login_as(c, "foo@example.com")
        assert c.get("/admin-test").status_code == 404
    with TestClient(app, base_url=BASE) as c:
        login_as(c, "boss@example.com")
        assert c.get("/admin-test").text == '"admin-only"'


# ---- 来访 IP 与 next ----


def _request(client_host: str, forwarded: str | None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (client_host, 1),
        }
    )


@pytest.mark.parametrize(
    ("host", "forwarded", "expected"),
    [
        ("172.18.0.5", "1.2.3.4, 203.0.113.9", "203.0.113.9"),
        ("127.0.0.1", "203.0.113.9", "203.0.113.9"),
        ("8.8.8.8", "203.0.113.9", "8.8.8.8"),
        ("172.18.0.5", None, "172.18.0.5"),
        ("172.18.0.5", "garbage", "172.18.0.5"),
    ],
)
def test_client_ip(host: str, forwarded: str | None, expected: str) -> None:
    assert client_ip(_request(host, forwarded)) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/signals", "/signals"),
        ("/manual?sub=3", "/manual?sub=3"),
        ("//evil.example", None),
        ("/\\evil.example", None),
        ("https://evil.example", None),
        ("evil", None),
        ("", None),
        (None, None),
        ("/a\nb", None),
    ],
)
def test_safe_next(raw: str | None, expected: str | None) -> None:
    assert safe_next(raw) == expected
