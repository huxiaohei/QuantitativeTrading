"""命令行共用的运行环境：配置路径、数据目录、数据库、交易日历与当前时间。

qt run-day、qt worker、qt admin 都从这里解析配置与数据目录（同一组选项与环境变量
QT_CONFIG、QT_DATA_DIR、DATABASE_URL），保证管理命令与每晚的任务用同一份规则。
每日任务独有的部分（ADMIN_EMAIL、数据源、发信器）在 qt.cli.make_context 里叠加在其上。
"""

from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
import yaml
from sqlalchemy import Engine

from qt.calendar import TradingCalendar
from qt.config import AppConfig, load_config
from qt.data.store import ParquetStore
from qt.data.update import load_calendar
from qt.db.engine import database_url, make_engine

DEFAULT_CONFIG = Path("config/default.yaml")
DEFAULT_DATA_DIR = Path("data")

ConfigOption = Annotated[Path, typer.Option("--config", help="配置文件路径", envvar="QT_CONFIG")]
DataDirOption = Annotated[Path, typer.Option("--data-dir", help="数据目录", envvar="QT_DATA_DIR")]


class SetupError(RuntimeError):
    """运行环境无法建立（配置文件、DATABASE_URL 等）；消息为中文。"""


def now() -> datetime:
    """当前时间（上海时区）。"""
    return datetime.now(ZoneInfo("Asia/Shanghai"))


class Runtime:
    """配置、数据库引擎与数据存储；各项在用到时才创建。"""

    def __init__(self, config: Path, data_dir: Path) -> None:
        self._config = config
        self._data_dir = data_dir
        self._cfg: AppConfig | None = None
        self._engine: Engine | None = None
        self._store: ParquetStore | None = None

    def cfg(self) -> AppConfig:
        if self._cfg is None:
            try:
                self._cfg = load_config(self._config)
            except (OSError, yaml.YAMLError, ValueError) as exc:  # ValidationError 也是 ValueError
                raise SetupError(f"无法读取配置文件 {self._config}：{exc}") from exc
        return self._cfg

    def engine(self) -> Engine:
        if self._engine is None:
            try:
                self._engine = make_engine(database_url())
            except RuntimeError as exc:  # 缺 DATABASE_URL：消息已是中文
                raise SetupError(str(exc)) from exc
        return self._engine

    def store(self) -> ParquetStore:
        if self._store is None:
            self._store = ParquetStore(self._data_dir)
        return self._store

    def calendar(self) -> TradingCalendar:
        return load_calendar(self.store())

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
