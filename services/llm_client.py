import httpx
import logging
from config import settings

logger = logging.getLogger(__name__)

class LLMClient:
    """多模型路由：阿里百炼为主，智谱GLM为备"""

    def __init__(self):
        self.providers = [
            {
                "name": "bailian",
                "base_url": settings.BAILIAN_BASE_URL,
                "api_key": settings.BAILIAN_API_KEY,
                "model": settings.BAILIAN_MODEL,
            },
            {
                "name": "glm",
                "base_url": settings.GLM_BASE_URL,
                "api_key": settings.GLM_API_KEY,
                "model": settings.GLM_MODEL,
            },
        ]
        self.client = httpx.AsyncClient(timeout=settings.LLM_TIMEOUT)

    async def chat(self, messages: list[dict],
                   temperature: float = 0.7,
                   max_tokens: int = 1024) -> str:
        """带故障转移的LLM调用"""
        last_error = None
        for provider in self.providers:
            if not provider["api_key"]:
                continue
            try:
                response = await self.client.post(
                    f"{provider['base_url']}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {provider['api_key']}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": provider["model"],
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                )
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                logger.warning(f"LLM提供商 {provider['name']} 失败: {e}")
                last_error = e
                continue

        raise RuntimeError(f"所有LLM提供商均不可用: {last_error}")

    async def close(self):
        await self.client.aclose()
    async def chat_with_search(self, query: str) -> dict:
        """使用百炼内置联网搜索能力回答问题"""
        for provider in self.providers:
            if not provider["api_key"] or "bailian" not in provider["name"]:
                continue
            try:
                response = await self.client.post(
                    f"{provider['base_url']}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {provider['api_key']}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": provider["model"],
                        "messages": [
                            {"role": "system", "content": "你是信息检索专家。请基于联网搜索结果，用中文简洁回答，不要编造信息。"},
                            {"role": "user", "content": query},
                        ],
                        "enable_search": True,           # 开启联网搜索
                        "search_options": {
                            "forced_search": True,       # 强制搜索
                            "search_strategy": "pro",
                        },
                        "temperature": 0.3,
                        "max_tokens": 800,
                    },
                )
                response.raise_for_status()
                data = response.json()
                choice = data["choices"][0]["message"]

                # 提取搜索结果引用（不同版本字段名可能不同，做兼容）
                search_info = choice.get("search_info") or {}
                references = []
                for item in search_info.get("search_results", []):
                    references.append({
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "snippet": item.get("content", "")[:200],
                        "credibility": self._judge_url(item.get("url", "")),
                    })

                return {
                    "answer": choice.get("content", ""),
                    "references": references,
                }
            except Exception as e:
                logger.warning(f"联网检索失败 ({provider['name']}): {e}")
                continue
        raise RuntimeError("联网检索不可用")

    def _judge_url(self, url: str) -> str:
        authoritative = ["neea.edu.cn", "cet-bm.neea.edu.cn", "moe.gov.cn", "gov.cn", "edu.cn"]
        if any(d in url for d in authoritative):
            return "authoritative"
        if any(d in url for d in ["nature.com", "science.org", "ieee.org", "cnki.net"]):
            return "reference"
        return "unverified"
llm_client = LLMClient()