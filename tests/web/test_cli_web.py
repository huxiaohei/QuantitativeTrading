"""qt web：QT_BASE_URL 缺失或不合规时报错退出；正常时用单进程 uvicorn 启动应用。"""

from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from typer.testing import CliRunner

import qt.cli
from tests.conftest import resolve_test_db_url

_CONFIG = str(Path(__file__).resolve().parents[2] / "config" / "default.yaml")


def _invoke(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(qt.cli.app, ["web", "--config", _CONFIG, *args])
    return result.exit_code, result.output


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", resolve_test_db_url())
    monkeypatch.setenv("MAIL_BACKEND", "file")
    monkeypatch.setenv("MAIL_FILE_DIR", str(tmp_path / "mails"))


def test_web_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QT_BASE_URL", raising=False)
    code, output = _invoke()
    assert code == 1
    assert "QT_BASE_URL" in output


def test_web_rejects_plain_http_on_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_BASE_URL", "http://quant.example.com")
    code, output = _invoke()
    assert code == 1
    assert "https" in output


def test_web_runs_single_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_BASE_URL", "https://quant.example.com/")
    calls: list[dict[str, object]] = []

    def fake_run(app: FastAPI, **kwargs: object) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    code, output = _invoke("--host", "0.0.0.0", "--port", "8123")
    assert code == 0, output
    assert len(calls) == 1
    call = calls[0]
    assert isinstance(call["app"], FastAPI)
    assert call["host"] == "0.0.0.0"
    assert call["port"] == 8123
    assert call["proxy_headers"] is False
    assert call["access_log"] is False
