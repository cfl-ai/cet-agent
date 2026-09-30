"""
存储守卫 — 所有数据入库前的唯一守门人
职责：
1. 基于内容SHA256去重（同内容不同文件名也视为重复）
2. 基于来源URL去重（同一URL不重复下载）
3. 存储配额控制（超过阈值时按优先级清理）
4. 生成巡检报告
"""
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional


class StorageGuard:
    """数据存储守卫"""

    # 存储配额（可根据服务器磁盘调整）
    MAX_RAW_SIZE_MB = 500          # raw目录最大500MB
    MAX_PARSED_SIZE_MB = 200       # parsed目录最大200MB
    MAX_DB_SIZE_MB = 300           # 数据库最大300MB

    # 文件保留天数（超过此天数的raw文件自动清理）
    RAW_RETENTION_DAYS = 30
    PARSED_RETENTION_DAYS = 7      # parsed文件入库后7天内清理

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.raw_dir = self.data_dir / "raw"
        self.parsed_dir = self.data_dir / "parsed"
        self.checksum_file = self.raw_dir / "checksums.json"
        self.url_index_file = self.raw_dir / "url_index.json"

        # 确保目录存在
        for d in [self.data_dir, self.raw_dir, self.parsed_dir,
                  self.raw_dir / "zhenti", self.raw_dir / "vocab",
                  self.parsed_dir / "questions", self.parsed_dir / "words"]:
            self._ensure_dir(d)

        # 加载已有索引
        self.checksums = self._load_json(self.checksum_file, {})
        self.url_index = self._load_json(self.url_index_file, {})

    # ---- 目录工具 ----

    def _ensure_dir(self, path: Path):
        """健壮地确保目录存在 — 若同名文件存在则先删除"""
        if path.exists() and not path.is_dir():
            try:
                path.unlink()
            except Exception as e:
                raise RuntimeError(
                    f"路径 {path} 是文件且无法删除，请手动删除后重试: {e}"
                )
        path.mkdir(parents=True, exist_ok=True)

    # ---- 去重检查 ----

    def compute_hash(self, content: bytes) -> str:
        """计算内容SHA256"""
        return hashlib.sha256(content).hexdigest()

    def is_duplicate_content(self, content: bytes) -> tuple[bool, Optional[str]]:
        """
        基于内容去重
        返回 (是否重复, 已存在的文件路径)
        """
        h = self.compute_hash(content)
        if h in self.checksums:
            return True, self.checksums[h]["path"]
        return False, None

    def is_duplicate_url(self, url: str) -> tuple[bool, Optional[str]]:
        """基于URL去重，避免重复下载"""
        if url in self.url_index:
            return True, self.url_index[url]
        return False, None

    def register_content(self, content: bytes, path: str, url: str = "",
                         source_type: str = "unknown"):
        """注册新内容到索引"""
        h = self.compute_hash(content)
        self.checksums[h] = {
            "path": path,
            "size": len(content),
            "url": url,
            "source_type": source_type,
            "registered_at": time.time(),
        }
        if url:
            self.url_index[url] = path
        self._save_json(self.checksum_file, self.checksums)
        self._save_json(self.url_index_file, self.url_index)

    # ---- 安全写入 ----

    def safe_write(self, content: bytes, relative_path: str,
                   url: str = "", source_type: str = "unknown") -> dict:
        """
        安全写入：先去重，再写盘，最后注册
        返回 {"written": bool, "reason": str, "path": str}
        """
        # 1. 内容去重
        is_dup, existing = self.is_duplicate_content(content)
        if is_dup:
            return {"written": False, "reason": "content_duplicate",
                    "path": existing}

        # 2. URL去重
        if url:
            is_url_dup, existing_url = self.is_duplicate_url(url)
            if is_url_dup:
                return {"written": False, "reason": "url_duplicate",
                        "path": existing_url}

        # 3. 检查配额
        if not self._has_quota(len(content)):
            self.auto_cleanup()
            if not self._has_quota(len(content)):
                return {"written": False, "reason": "quota_exceeded",
                        "path": ""}

        # 4. 写入文件
        full_path = self.data_dir / relative_path
        self._ensure_dir(full_path.parent)
        full_path.write_bytes(content)

        # 5. 注册
        self.register_content(content, str(full_path), url, source_type)

        return {"written": True, "reason": "ok", "path": str(full_path)}

    def safe_write_json(self, data: dict, relative_path: str,
                        url: str = "", source_type: str = "parsed") -> dict:
        """写入JSON数据（自动序列化后走safe_write）"""
        content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        return self.safe_write(content, relative_path, url, source_type)

    # ---- 配额与清理 ----

    def _has_quota(self, incoming_bytes: int) -> bool:
        """检查是否有足够配额"""
        raw_size = self._dir_size(self.raw_dir)
        parsed_size = self._dir_size(self.parsed_dir)
        return (raw_size + incoming_bytes < self.MAX_RAW_SIZE_MB * 1024 * 1024
                and parsed_size < self.MAX_PARSED_SIZE_MB * 1024 * 1024)

    def _dir_size(self, path: Path) -> int:
        """计算目录总大小"""
        total = 0
        if not path.exists():
            return 0
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except Exception:
                    continue
        return total

    def auto_cleanup(self) -> dict:
        """
        自动清理 — 按优先级删除：
        1. 过期raw文件（超过保留天数）
        2. 已入库的parsed文件（超过保留天数）
        3. 最旧的raw文件（配额不足时）
        """
        report = {"deleted_raw": 0, "deleted_parsed": 0,
                  "freed_bytes": 0, "details": []}
        now = time.time()

        # 1. 清理过期raw文件
        for f in self.raw_dir.rglob("*"):
            if not f.is_file() or f.name in ("checksums.json", "url_index.json"):
                continue
            try:
                age_days = (now - f.stat().st_mtime) / 86400
                if age_days > self.RAW_RETENTION_DAYS:
                    freed = f.stat().st_size
                    f.unlink()
                    report["deleted_raw"] += 1
                    report["freed_bytes"] += freed
                    report["details"].append(f"raw: {f.name} ({age_days:.0f}天前)")
            except Exception:
                continue

        # 2. 清理已入库的parsed文件
        for f in self.parsed_dir.rglob("*"):
            if not f.is_file():
                continue
            try:
                age_days = (now - f.stat().st_mtime) / 86400
                if age_days > self.PARSED_RETENTION_DAYS:
                    freed = f.stat().st_size
                    f.unlink()
                    report["deleted_parsed"] += 1
                    report["freed_bytes"] += freed
                    report["details"].append(f"parsed: {f.name} ({age_days:.0f}天前)")
            except Exception:
                continue

        # 3. 如果仍超配额，删除最旧的raw文件
        if self._dir_size(self.raw_dir) > self.MAX_RAW_SIZE_MB * 1024 * 1024:
            raw_files = sorted(
                [f for f in self.raw_dir.rglob("*") if f.is_file()
                 and f.name not in ("checksums.json", "url_index.json")],
                key=lambda f: f.stat().st_mtime
            )
            for f in raw_files:
                if self._dir_size(self.raw_dir) < self.MAX_RAW_SIZE_MB * 1024 * 1024 * 0.8:
                    break
                try:
                    freed = f.stat().st_size
                    f.unlink()
                    report["deleted_raw"] += 1
                    report["freed_bytes"] += freed
                    report["details"].append(f"raw(配额): {f.name}")
                except Exception:
                    continue

        # 更新索引（移除已删除的文件）
        self._rebuild_index()
        return report

    def _rebuild_index(self):
        """重建索引，移除已删除文件的记录"""
        valid_checksums = {}
        for h, info in self.checksums.items():
            try:
                if Path(info["path"]).exists():
                    valid_checksums[h] = info
            except Exception:
                continue
        self.checksums = valid_checksums

        valid_urls = {}
        for url, path in self.url_index.items():
            try:
                if Path(path).exists():
                    valid_urls[url] = path
            except Exception:
                continue
        self.url_index = valid_urls

        self._save_json(self.checksum_file, self.checksums)
        self._save_json(self.url_index_file, self.url_index)

    # ---- 巡检报告 ----

    def inspect(self) -> dict:
        """生成存储巡检报告"""
        raw_size = self._dir_size(self.raw_dir)
        parsed_size = self._dir_size(self.parsed_dir)
        db_path = self.data_dir / "cet_agent.db"
        db_size = db_path.stat().st_size if db_path.exists() else 0

        return {
            "timestamp": time.time(),
            "raw": {
                "size_mb": round(raw_size / 1024 / 1024, 2),
                "limit_mb": self.MAX_RAW_SIZE_MB,
                "usage_pct": round(raw_size / (self.MAX_RAW_SIZE_MB * 1024 * 1024) * 100, 1),
                "file_count": len([f for f in self.raw_dir.rglob("*") if f.is_file()])
                              if self.raw_dir.exists() else 0,
            },
            "parsed": {
                "size_mb": round(parsed_size / 1024 / 1024, 2),
                "limit_mb": self.MAX_PARSED_SIZE_MB,
                "usage_pct": round(parsed_size / (self.MAX_PARSED_SIZE_MB * 1024 * 1024) * 100, 1),
            },
            "database": {
                "size_mb": round(db_size / 1024 / 1024, 2),
                "limit_mb": self.MAX_DB_SIZE_MB,
            },
            "checksums_count": len(self.checksums),
            "url_index_count": len(self.url_index),
            "status": "healthy" if raw_size < self.MAX_RAW_SIZE_MB * 1024 * 1024 * 0.8 else "warning",
        }

    # ---- 工具方法 ----

    def _load_json(self, path: Path, default):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                return default
        return default

    def _save_json(self, path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))