"""操作人：审计日志用它记录是谁、通过哪个渠道做的操作。"""

from dataclasses import dataclass
from typing import Literal

Channel = Literal["web", "cli", "job"]


@dataclass(frozen=True)
class Actor:
    """操作人。user_id 为空表示不是某个使用者本人（命令行管理员或夜间任务）。"""

    user_id: int | None
    channel: Channel


CLI = Actor(None, "cli")
JOB = Actor(None, "job")


def web_actor(user_id: int) -> Actor:
    """网页渠道的操作人，user_id 是当前登录的使用者。"""
    return Actor(user_id, "web")
