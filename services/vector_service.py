"""
向量检索服务 — 基于 ChromaDB（嵌入式）
未装 ChromaDB 或初始化失败时优雅降级为 no-op。
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_AVAILABLE = False
try:
    import chromadb
    _AVAILABLE = True
except ImportError:
    logger.info("[VectorService] ChromaDB 未安装，向量检索禁用")


class VectorService:
    def __init__(self, persist_dir: str = "data/chroma"):
        self.enabled = False
        self.client = None
        self.collection = None

        if not _AVAILABLE:
            return

        try:
            self.client = chromadb.PersistentClient(path=persist_dir)
            self.collection = self.client.get_or_create_collection(
                name="questions",
                metadata={"hnsw:space": "cosine"},
            )
            self.enabled = True
            logger.info(
                f"[VectorService] 已就绪，现有 {self.collection.count()} 条向量"
            )
        except Exception as e:
            logger.error(f"[VectorService] 初始化失败：{e}")
            self.enabled = False

    def add_questions(self, questions: list[dict]) -> int:
        """批量写入题目到向量库，返回成功写入数"""
        if not self.enabled or not questions:
            return 0

        valid = [q for q in questions
                 if q.get("question_id") and q.get("content")]
        if not valid:
            return 0

        try:
            self.collection.add(
                documents=[q["content"][:2000] for q in valid],
                metadatas=[{
                    "question_id": q["question_id"],
                    "type": q.get("question_type", ""),
                    "difficulty": int(q.get("difficulty", 3)),
                    "topic": q.get("topic", ""),
                    "source": q.get("source", "ai_generated"),
                } for q in valid],
                ids=[q["question_id"] for q in valid],
            )
            return len(valid)
        except Exception as e:
            logger.error(f"[VectorService] 写入失败：{e}")
            return 0

    def find_similar(self, query_text: str, k: int = 5,
                     where: Optional[dict] = None) -> list[dict]:
        """语义检索相似题"""
        if not self.enabled or not query_text:
            return []

        try:
            results = self.collection.query(
                query_texts=[query_text[:2000]],
                n_results=k,
                where=where,
            )
            items = []
            ids = (results.get("ids") or [[]])[0]
            docs = (results.get("documents") or [[]])[0]
            metas = (results.get("metadatas") or [[]])[0]
            distances = (results.get("distances") or [[]])[0]

            for i, qid in enumerate(ids):
                items.append({
                    "question_id": qid,
                    "content": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                    "distance": distances[i] if i < len(distances) else 1.0,
                })
            return items
        except Exception as e:
            logger.error(f"[VectorService] 检索失败：{e}")
            return []

    def count(self) -> int:
        if not self.enabled:
            return 0
        try:
            return self.collection.count()
        except Exception:
            return 0


# 全局单例
vector_service = VectorService()