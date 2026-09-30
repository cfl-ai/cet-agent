import asyncio
import httpx
import json
from enum import Enum

# 将 ErrorType 移到 retry.py 中定义
class ErrorType(Enum):
    TRANSIENT = "transient"      # 网络抖动、限流
    SEMANTIC = "semantic"        # LLM输出格式错误
    TOOL = "tool"                # 工具/API调用失败
    BUSINESS = "business"        # 业务规则冲突

def classify_error(exception: Exception) -> ErrorType:
    """四类错误分类 — 企业级核心机制"""
    # 瞬时错误
    if isinstance(exception, (httpx.TimeoutException, httpx.ConnectError,
                              httpx.ReadTimeout, asyncio.TimeoutError)):
        return ErrorType.TRANSIENT
    if isinstance(exception, httpx.HTTPStatusError):
        if exception.response.status_code in (429, 502, 503, 504):
            return ErrorType.TRANSIENT

    # 语义错误（LLM输出问题）
    if isinstance(exception, (ValueError, KeyError, json.JSONDecodeError)):
        return ErrorType.SEMANTIC

    # 工具错误
    if isinstance(exception, (ConnectionRefusedError, OSError)):
        return ErrorType.TOOL

    # 默认为业务错误（保守策略）
    return ErrorType.BUSINESS


class RetryPolicy:
    """有界重试策略"""
    def __init__(self):
        self.limits = {
            ErrorType.TRANSIENT: 3,
            ErrorType.SEMANTIC: 2,
            ErrorType.TOOL: 2,
            ErrorType.BUSINESS: 0,
        }

    def can_retry(self, error_type: ErrorType, attempt: int) -> bool:
        return attempt < self.limits.get(error_type, 0)

    def get_backoff(self, attempt: int) -> float:
        return min(2 ** attempt, 8)