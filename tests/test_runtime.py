"""运行环境：QT_BASE_URL 的解析与 cookie 安全标记。"""

import pytest

from qt.runtime import SetupError, base_url, cookie_secure


def test_base_url_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QT_BASE_URL", raising=False)
    assert base_url() is None


def test_base_url_https_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_BASE_URL", "https://quant.example.com")
    assert base_url() == "https://quant.example.com"


@pytest.mark.parametrize(
    "url", ["http://localhost:8000", "http://localhost", "http://127.0.0.1:8000"]
)
def test_base_url_local_http_accepted(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("QT_BASE_URL", url)
    assert base_url() == url


def test_base_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_BASE_URL", "https://quant.example.com/")
    assert base_url() == "https://quant.example.com"


@pytest.mark.parametrize(
    "url", ["http://quant.example.com", "http://localhost.evil.com", "ftp://x", "quant.example.com"]
)
def test_base_url_rejects_plain_http(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("QT_BASE_URL", url)
    with pytest.raises(SetupError, match="QT_BASE_URL 必须是 https 地址"):
        base_url()


def test_cookie_secure_only_for_https() -> None:
    assert cookie_secure("https://quant.example.com") is True
    assert cookie_secure("http://localhost:8000") is False
