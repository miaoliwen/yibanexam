"""统一异常类型。

为什么要单独一层：`config.load()` 以前直接 `raise SystemExit`，而 SystemExit 继承
自 BaseException —— 在 GUI 的 HTTP 处理线程、或任何 `except Exception` 里都抓不到，
请求会永远悬着。现在所有可预期的失败都走 AppError 分支，调用方可以安全兜底。
"""
from __future__ import annotations


class AppError(RuntimeError):
    """本项目所有可预期错误的基类。"""


class ConfigError(AppError):
    """配置缺失 / 非法。"""


class LoginError(AppError):
    """登录失败。kind 用于区分处置方式：

    credentials —— 账号密码错误（不要重试）
    rate_limited —— 本地/平台限频（等一会儿）
    captcha     —— 需要人工验证码（改用浏览器）
    network     —— 网络问题（可重试）
    unknown
    """

    def __init__(self, msg: str = "", kind: str = "unknown"):
        super().__init__(msg)
        self.kind = kind


class AuthError(AppError):
    """登录态失效（401/403）。"""


class SessionError(AppError):
    """浏览器会话不可用（未登录、页面被关闭、驱动异常）。"""


class RiskControlError(AppError):
    """平台明确返回的风控/限频信号（429、频繁、验证码校验等）。"""


class TransientError(AppError):
    """网络抖动、超时、5xx —— 可重试，但写操作重试前必须先核实服务端状态。"""


class ExamWindowError(AppError):
    """考试未开始 / 已截止 / 次数已用完 / 列表为空等业务前提不满足。"""


class StateError(AppError):
    """本地状态、锁、断点续跑相关的错误。"""


class ApiError(AppError):
    """接口返回业务失败（result=false / 非 JSON / 4xx 非鉴权类）。"""


class SubmitUnconfirmed(StateError):
    """交卷请求已发出但无法确认服务端是否收到 —— 绝不能盲目重发。"""


class LLMError(AppError):
    """大模型调用失败。"""


class LLMConfigError(LLMError):
    """大模型配置错误（缺 key、key 无效），重试无意义。"""
