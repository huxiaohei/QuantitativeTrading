"""每日运行记录的展示（全站共用，不属于某个使用者）。"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy.orm import Session

from qt.data.update import format_bulk
from qt.db import admin_repo
from qt.db.models import DailyRun
from qt.service.views.base import RUN_STATUS_LABELS

_DATA_FAILED = "data_failed"


@dataclass(frozen=True)
class RunView:
    """最近一次每日运行：data_ok 为假表示当天数据异常、没有出信号。"""

    day: date
    status: str
    status_label: str
    finished_at: datetime | None
    data_ok: bool


@dataclass(frozen=True)
class RunDetail:
    """一次每日运行的全部展示信息；reasons 是失败原因与异常明细，每项一行。"""

    day: date
    status: str
    status_label: str
    stage: str
    started_at: datetime
    finished_at: datetime | None
    data_ok: bool
    reasons: list[str]


@dataclass(frozen=True)
class RunsView:
    """最近的每日运行（日期从新到旧）与发件箱里待发、发送失败的邮件数。"""

    pending_mails: int
    failed_mails: int
    runs: list[RunDetail]


def _run_view(run: DailyRun) -> RunView:
    return RunView(
        day=run.trade_day,
        status=run.status,
        status_label=RUN_STATUS_LABELS.get(run.status, run.status),
        finished_at=run.finished_at,
        data_ok=run.status != _DATA_FAILED,
    )


def latest_run(s: Session) -> RunView | None:
    """交易日最新的一次每日运行；还没有运行过为 None。"""
    found = admin_repo.list_recent_runs(s, 1)
    return _run_view(found[0]) if found else None


def _json_text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def reason_lines(detail: dict[str, Any]) -> list[str]:
    """从每日运行的 detail 里取出失败原因与异常明细。"""
    lines: list[str] = []
    error = detail.get("error")
    if error:
        lines.append(f"原因：{error}")
    for problem in cast("list[Any]", detail.get("problems") or []):
        lines.append(f"数据问题：{problem}")
    failed = cast("dict[str, Any]", detail.get("download_failed") or {})
    if failed:
        lines.append(f"下载失败 {len(failed)} 只（已沿用本地数据）：")
        lines.extend(f"  {symbol}：{message}" for symbol, message in failed.items())
    bulk = detail.get("bulk")
    if isinstance(bulk, dict):
        info = cast("dict[str, Any]", bulk)
        lines.append(
            format_bulk(
                bool(info.get("used")),
                int(info.get("bars") or 0),
                int(info.get("suspended") or 0),
                int(info.get("per_symbol") or 0),
                str(info.get("reason") or ""),
            )
        )
    retried = detail.get("bars_retried")
    if retried:
        lines.append(f"缺当日日线、已再下载一次：{_json_text(retried)}")
    for item in cast("list[dict[str, Any]]", detail.get("user_errors") or []):
        lines.append(
            f"使用者 {item.get('user_id')}（订阅 {item.get('subscription_id')}）处理出错："
            f"{item.get('error')}"
        )
    labels = {
        "followups_error": "跟踪记录回填出错",
        "mail_error": "邮件发送出错",
        "fundamentals_failed": "基本面复核失败",
        "recheck_errors": "基本面复核出错",
        "weekly_errors": "周报出错",
    }
    for key, label in labels.items():
        value = detail.get(key)
        if value:
            lines.append(f"{label}：{value if isinstance(value, str) else _json_text(value)}")
    mail = detail.get("mail")
    if isinstance(mail, dict):
        sent = cast("dict[str, Any]", mail)
        lines.append(f"邮件：已发 {sent.get('sent', 0)} 封，失败 {sent.get('failed', 0)} 封")
    return lines


def recent_runs(s: Session, limit: int) -> RunsView:
    """最近 limit 个交易日的每日运行（管理员看的运行记录），附发件箱待发与失败数。"""
    counts = admin_repo.count_outbox_by_status(s)
    runs = [
        RunDetail(
            day=run.trade_day,
            status=run.status,
            status_label=RUN_STATUS_LABELS.get(run.status, run.status),
            stage=run.stage,
            started_at=run.started_at,
            finished_at=run.finished_at,
            data_ok=run.status != _DATA_FAILED,
            reasons=reason_lines(run.detail),
        )
        for run in admin_repo.list_recent_runs(s, limit)
    ]
    return RunsView(counts.get("pending", 0), counts.get("failed", 0), runs)
