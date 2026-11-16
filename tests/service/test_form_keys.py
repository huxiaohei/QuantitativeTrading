"""表单一次性值：认领、重复认领、回滚后可再认领、并发的两次认领排队。"""

import threading
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from qt.service.form_keys import claim_form, is_valid_form_value, new_form_value

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def test_value_format() -> None:
    assert is_valid_form_value(new_form_value())
    assert not any(is_valid_form_value(v) for v in ("", "abc", "a b" * 10, "é" * 20, "x" * 65))


def test_claim_once_per_user_and_value(db: Session) -> None:
    value = new_form_value()
    assert claim_form(db, 1, value, NOW) is True
    assert claim_form(db, 1, value, NOW) is False
    assert claim_form(db, 2, value, NOW) is True  # 别的使用者的同一个值互不影响
    assert claim_form(db, 1, new_form_value(), NOW) is True


def test_claim_rolls_back_with_savepoint(db: Session) -> None:
    value = new_form_value()
    nested = db.begin_nested()
    assert claim_form(db, 1, value, NOW) is True
    nested.rollback()
    assert claim_form(db, 1, value, NOW) is True


def test_concurrent_claim_waits_for_the_first_and_then_sees_it(
    db: Session, db_engine: Engine
) -> None:
    value = new_form_value()
    first = Session(db_engine)
    second = Session(db_engine)
    result: list[bool] = []
    started = threading.Event()

    def later() -> None:
        started.set()
        result.append(claim_form(second, 1, value, NOW))
        second.commit()

    try:
        assert claim_form(first, 1, value, NOW) is True  # 第一个事务认领后还没提交
        worker = threading.Thread(target=later)
        worker.start()
        started.wait(5)
        worker.join(0.5)
        assert worker.is_alive()  # 第二个在等第一个的锁，没有抢先通过
        first.commit()
        worker.join(10)
        assert not worker.is_alive()
        assert result == [False]  # 等到之后看到第一个的记录
    finally:
        first.rollback()
        first.close()
        second.close()
