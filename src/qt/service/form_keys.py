"""表单的一次性值：同一个表单（同一个值）只能提交成功一次，连点两次不会产生两条流水。

值记在 rate_events 表里（键 form:{使用者}:{值}，auth.purge_expired 会清掉两天前的记录）。
claim_form 用数据库的事务级咨询锁让同一使用者的认领排队：锁到事务结束才放，
所以并发的两次提交里后到的会等先到的提交完成，然后看到它的记录。
认领与写入账本必须在同一个事务（或保存点）里，写入失败时认领一起回滚，改正后可以再提交。
"""

import re
import secrets
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from qt.db.models import RateEvent

_VALID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def new_form_value() -> str:
    """渲染表单时生成的随机值。"""
    return secrets.token_urlsafe(16)


def is_valid_form_value(value: str) -> bool:
    """值的格式是否合法（缺失、被改成别的东西都不合法）。"""
    return _VALID.match(value) is not None


def claim_form(s: Session, user_id: int, value: str, now: datetime) -> bool:
    """认领这个表单值：第一次返回 True（并记下），已经认领过返回 False。

    value 必须先通过 is_valid_form_value。
    """
    s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"form:{user_id}"})
    key = f"form:{user_id}:{value}"
    taken = s.scalar(select(func.count()).select_from(RateEvent).where(RateEvent.key == key))
    if taken:
        return False
    s.add(RateEvent(key=key, at=now))
    s.flush()
    return True
