import time
import hashlib
import json
import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Optional
from core.retry import ErrorType, classify_error, RetryPolicy
from core.idempotency import IdempotencyStore
from core.audit import AuditLogger
from core.monitor import MetricsCollector

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """所有Agent的基类，封装企业级容错机制"""

    def __init__(self, name: str, max_retries: int = 2, idempotent: bool = True):
        self.name = name
        self.max_retries = max_retries
        self.idempotent = idempotent       # ★ 生成类Agent应传 False
        self.idempotency = IdempotencyStore()
        self.audit = AuditLogger()
        self.metrics = MetricsCollector()

    async def execute(self, task: dict, task_id: str) -> dict:
        """带重试、幂等、审计的执行入口"""
        # 1. 幂等性检查（仅写操作类Agent启用）
        idem_key = self._build_idempotency_key(task)
        if self.idempotent:
            cached = await self.idempotency.get(idem_key)
            if cached:
                logger.info(f"[{self.name}] 幂等命中，返回缓存结果")
                return cached

        # 2. 审计记录开始
        audit_id = self.audit.record_start(self.name, task_id, task)

        # 3. 带重试的执行
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                start = time.time()
                result = await self._run(task)
                elapsed = time.time() - start

                # 4. 结果校验
                self._validate_output(result)

                # 5. 记录成功
                self.audit.record_success(audit_id, result)
                self.metrics.record(
                    agent=self.name, status="success",
                    latency=elapsed, attempt=attempt
                )

                # 6. 幂等写入（仅写操作类Agent）
                if self.idempotent:
                    await self.idempotency.set(idem_key, result)
                return result

            except Exception as e:
                error_type = classify_error(e)
                last_error = e

                self.metrics.record(
                    agent=self.name, status="error",
                    error_type=error_type.value, attempt=attempt
                )
                logger.warning(
                    f"[{self.name}] 第{attempt+1}次执行失败 "
                    f"({error_type.value}): {e}"
                )

                # 业务错误直接升级，不重试
                if error_type == ErrorType.BUSINESS:
                    self.audit.record_failure(audit_id, str(e), "business_escalated")
                    raise

                # 检查重试上限
                if attempt >= self.max_retries:
                    break

                # 指数退避
                backoff = min(2 ** attempt, 8)
                await asyncio.sleep(backoff)

        # 所有重试耗尽
        self.audit.record_failure(audit_id, str(last_error), "retries_exhausted")
        raise RuntimeError(
            f"[{self.name}] 执行失败，已重试{self.max_retries}次: {last_error}"
        )

    def _build_idempotency_key(self, task: dict) -> str:
        """基于任务内容生成确定性幂等键"""
        raw = json.dumps(task, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    @abstractmethod
    async def _run(self, task: dict) -> dict:
        """子类实现具体逻辑"""
        ...

    @abstractmethod
    def _validate_output(self, result: dict):
        """子类实现输出校验"""
        ...