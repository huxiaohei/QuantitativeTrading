"""服务层异常。消息为中文，可直接展示给使用者。"""


class NotFoundError(Exception):
    """记录不存在，或属于其他使用者（对外一律表现为不存在）。"""


class ValidationError(Exception):
    """输入不合法或当前状态不允许该操作。"""


class LedgerInconsistentError(Exception):
    """账本现有流水本身已不一致（系统问题，不是本次输入的错），需要管理员处理。"""
