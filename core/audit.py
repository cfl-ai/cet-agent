import hashlib
import json
import time
import uuid
from pathlib import Path

class AuditLogger:
        _instance = None

        def __new__(cls, log_dir="data/audit"):
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_logger(log_dir)
            return cls._instance
class AuditLogger:
    """哈希链审计 — 每条记录前向链接，篡改可检测"""

    def __init__(self, log_dir: str = "data/audit"):
        self.log_dir = Path(log_dir)
        # 兼容 Windows 并发的安全创建方式
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            if not self.log_dir.is_dir():
                raise RuntimeError(
                    f"路径 {self.log_dir} 已存在且不是一个目录，请手动删除该文件后重试。"
                )
        self._last_hash = self._load_last_hash()
    def _load_last_hash(self) -> str:
        chain_file = self.log_dir / "chain.json"
        if chain_file.exists():
            data = json.loads(chain_file.read_text())
            return data.get("last_hash", "0" * 64)
        return "0" * 64

    def _compute_hash(self, prev_hash: str, record: dict) -> str:
        raw = prev_hash + json.dumps(record, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def record_start(self, agent: str, task_id: str, task: dict) -> str:
        audit_id = str(uuid.uuid4())
        record = {
            "audit_id": audit_id, "agent": agent, "task_id": task_id,
            "action": "start", "task_hash": hashlib.sha256(
                json.dumps(task, sort_keys=True).encode()
            ).hexdigest()[:16], "timestamp": time.time()
        }
        self._append_record(record)
        return audit_id

    def record_success(self, audit_id: str, result: dict):
        record = {
            "audit_id": audit_id, "action": "success",
            "result_hash": hashlib.sha256(
                json.dumps(result, sort_keys=True).encode()
            ).hexdigest()[:16], "timestamp": time.time()
        }
        self._append_record(record)

    def record_failure(self, audit_id: str, error: str, reason: str):
        record = {
            "audit_id": audit_id, "action": "failure",
            "error": error[:200], "reason": reason, "timestamp": time.time()
        }
        self._append_record(record)

    def _append_record(self, record: dict):
        prev_hash = self._last_hash
        record["prev_hash"] = prev_hash
        record["hash"] = self._compute_hash(prev_hash, record)
        self._last_hash = record["hash"]

        log_file = self.log_dir / "audit_chain.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # 更新链头
        (self.log_dir / "chain.json").write_text(
            json.dumps({"last_hash": self._last_hash})
        )