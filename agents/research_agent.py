from agents.base import BaseAgent
from services.llm_client import llm_client


class ResearchAgent(BaseAgent):
    """自主联网检索 — 基于百炼内置联网搜索"""

    def __init__(self):
        super().__init__(name="ResearchAgent", max_retries=2)

    async def _run(self, task: dict) -> dict:
        query = task["query"]
        result = await llm_client.chat_with_search(query)

        return {
            "query": query,
            "summary": result["answer"],
            "results": result.get("references", []),
            "source_count": len(result.get("references", [])),
        }

    def _validate_output(self, result: dict):
        if not result.get("summary"):
            raise ValueError("检索结果为空")