"""门票服务共用的安全业务错误，不包含提交的身份信息。"""


class TicketRegistrationError(Exception):
    def __init__(self, message: str, code: int = 400):
        self.message, self.code = message, code
        super().__init__(message)
