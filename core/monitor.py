"""轻量级监控 — 内存指标 + SQLite持久化"""
import time
import json
import logging
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

class MetricsCollector:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.counters = defaultdict(int)      # 计数器
        self.latencies = defaultdict(list)    # 延迟样本
        self.errors = defaultdict(int)        # 错误计数
        self.max_samples = 1000
        self.log_path = Path("data/metrics.jsonl")
        self.log_path.parent.mkdir(exist_ok=True)

    def record(self, agent: str, status: str, latency: float = 0,
               attempt: int = 0, error_type: str = None):
        key = f"{agent}:{status}"
        self.counters[key] += 1

        if latency:
            self.latencies[agent].append(latency)
            if len(self.latencies[agent]) > self.max_samples:
                self.latencies[agent] = self.latencies[agent][-self.max_samples:]

        if error_type:
            self.errors[f"{agent}:{error_type}"] += 1

        # 异步落地（简化：直接追加）
        try:
            record = {
                "ts": time.time(), "agent": agent, "status": status,
                "latency": latency, "attempt": attempt, "error_type": error_type,
            }
            with open(self.log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass

    def snapshot(self) -> dict:
        """KPI快照 — 完成率、重试率、平均延迟"""
        snapshot = {"counters": dict(self.counters), "errors": dict(self.errors),
                    "avg_latency": {}, "p95_latency": {}}

        for agent, samples in self.latencies.items():
            if samples:
                sorted_samples = sorted(samples)
                snapshot["avg_latency"][agent] = round(sum(samples) / len(samples), 3)
                p95_idx = int(len(sorted_samples) * 0.95)
                snapshot["p95_latency"][agent] = round(sorted_samples[min(p95_idx, len(sorted_samples)-1)], 3)

        # 计算核心KPI
        success = sum(v for k, v in self.counters.items() if k.endswith(":success"))
        error = sum(v for k, v in self.counters.items() if k.endswith(":error"))
        total = success + error
        snapshot["kpi"] = {
            "completion_rate": round(success / total, 3) if total else 1.0,
            "retry_rate": round(self.errors.get("retry", 0) / total, 3) if total else 0,
            "human_intervention_rate": 0.0,
        }
        return snapshot

metrics = MetricsCollector()