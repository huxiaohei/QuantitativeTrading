"""部署配置的静态断言：解析 compose.yaml 与 Caddyfile，防止网页服务被误暴露。"""

from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> dict[str, object]:
    return cast(dict[str, object], yaml.safe_load((ROOT / name).read_text(encoding="utf-8")))


def _service(name: str, compose: str = "compose.yaml") -> dict[str, object]:
    services = cast(dict[str, dict[str, object]], _load(compose)["services"])
    return services[name]


def _list(value: object) -> list[str]:
    return [str(v) for v in cast(list[object], value)]


def test_web_不映射宿主机端口() -> None:
    assert "ports" not in _service("web")


def test_web_命令与数据只读挂载() -> None:
    web = _service("web")
    assert _list(web["command"]) == ["qt", "web", "--host", "0.0.0.0", "--port", "8000"]
    volumes = _list(web["volumes"])
    assert any(v.endswith(":/app/data:ro") for v in volumes)
    assert "./reports:/app/reports:ro" in volumes
    assert not any(v.split(":")[1:2] == ["/app/data"] and not v.endswith(":ro") for v in volumes)
    assert web["mem_limit"] == "800m"
    assert web["restart"] == "unless-stopped"


def test_proxy_只映射80和443且在public档位() -> None:
    proxy = _service("proxy")
    assert sorted(_list(proxy["ports"])) == ["443:443", "80:80"]
    assert _list(proxy["profiles"]) == ["public"]
    assert "web" in cast(dict[str, object], proxy["depends_on"]) or "web" in _list(
        proxy["depends_on"]
    )


def test_db_端口仍只绑定回环地址() -> None:
    ports = _list(_service("db")["ports"])
    assert ports and all(p.startswith("127.0.0.1:") for p in ports)


def test_开发覆盖文件把web映射到回环地址() -> None:
    assert _list(_service("web", "compose.dev.yaml")["ports"]) == ["127.0.0.1:8000:8000"]


def test_caddyfile_不含域名常量且不开访问日志() -> None:
    text = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "huxiaohei" not in text
    assert "{$QT_SITE}" in text
    code = [ln.strip() for ln in text.splitlines() if not ln.strip().startswith("#")]
    assert not any(ln.startswith("log") for ln in code)


def test_web_健康检查不走代理且NO_PROXY含回环地址() -> None:
    web = _service("web")
    health = cast(dict[str, object], web["healthcheck"])
    assert "ProxyHandler" in " ".join(_list(health["test"]))
    env = cast(dict[str, object], web["environment"])
    for key in ("NO_PROXY", "no_proxy"):
        assert "127.0.0.1" in str(env[key]).split(",")


def test_db_不先被内核挑中杀掉() -> None:
    assert _service("db")["oom_score_adj"] == -500


def test_每个服务的日志都有大小上限() -> None:
    services = cast(dict[str, dict[str, object]], _load("compose.yaml")["services"])
    for name, service in services.items():
        logging = cast(dict[str, object], service.get("logging"))
        assert logging is not None, name
        assert logging["driver"] == "json-file", name
        options = cast(dict[str, object], logging["options"])
        assert options == {"max-size": "10m", "max-file": "3"}, name


def test_caddyfile_说明转发头是替换而不是追加() -> None:
    text = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "追加 X-Forwarded-For" not in text
    assert "X-Forwarded-For" in text and "替换" in text
