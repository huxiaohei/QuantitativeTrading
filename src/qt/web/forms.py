"""表单字段解析：只判断格式，范围与业务含义交给 qt.service。

格式不对抛 FormError(字段名, 中文原因)，页面把原因显示在对应字段旁。
"""

import re
from datetime import date

_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRUE = frozenset({"on", "true", "1", "yes"})
PASSWORD_MAX_LENGTH = 1024


class FormError(Exception):
    """某个表单字段格式不对。"""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def parse_date(text: str, field: str) -> date:
    """YYYY-MM-DD 格式的日期。"""
    value = text.strip()
    if _DATE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise FormError(field, "日期格式应为 YYYY-MM-DD，例如 2026-01-05")


def _parse_number(text: str) -> float | None:
    value = text.strip()
    return float(value) if _NUMBER.match(value) else None


def parse_money(text: str, field: str) -> float:
    """金额：普通的十进制数字（不带逗号、不用科学计数法）。"""
    value = _parse_number(text)
    if value is None:
        raise FormError(field, "金额应为数字，例如 10000 或 12.50")
    return value


def parse_shares(text: str, field: str) -> float:
    """数量（股）：普通的十进制数字。"""
    value = _parse_number(text)
    if value is None:
        raise FormError(field, "数量应为数字，例如 100")
    return value


def parse_bool(text: str | None) -> bool:
    """勾选框：勾上时浏览器提交 on，没勾不提交。"""
    return text is not None and text.strip().lower() in _TRUE


def parse_password(text: str, field: str) -> str:
    """密码只在这里限制最大长度（防止超长输入拖慢哈希）；其余规则由 service 判断。"""
    if len(text) > PASSWORD_MAX_LENGTH:
        raise FormError(field, f"密码不能超过 {PASSWORD_MAX_LENGTH} 个字符")
    return text
