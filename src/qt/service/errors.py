"""服务层异常。消息为中文，可直接展示给使用者。"""


class NotFoundError(Exception):
    """记录不存在，或属于其他使用者（对外一律表现为不存在）。"""


class ValidationError(Exception):
    """输入不合法或当前状态不允许该操作。"""


class AuthError(ValidationError):
    """账号操作被拒绝（注册、登录、令牌、密码等）；消息不泄露邮箱是否存在。"""


class RateLimitedError(AuthError):
    """操作过于频繁。"""

    def __init__(self) -> None:
        super().__init__("操作太频繁，请稍后再试")


class EmailNotVerifiedError(AuthError):
    """密码正确但邮箱尚未验证；带上 user_id 以便页面提供重发验证邮件。"""

    def __init__(self, user_id: int) -> None:
        super().__init__("请先验证邮箱")
        self.user_id = user_id


class LedgerInconsistentError(Exception):
    """账本现有流水本身已不一致（系统问题，不是本次输入的错），需要管理员处理。"""
