import httpx
import logging
from agents.base import BaseAgent
from config import settings

logger = logging.getLogger(__name__)

class ContentAgent(BaseAgent):
    """文生图/视频Agent — 调用即梦AI免费接口"""

    def __init__(self):
        super().__init__(name="ContentAgent", max_retries=2)
        self.client = httpx.AsyncClient(timeout=60)

    async def _run(self, task: dict) -> dict:
        prompt = task["prompt"]
        content_type = task.get("content_type", "image")

        if not settings.JIMENG_TOKEN:
            # 优雅降级：返回占位图
            return self._fallback_result(prompt, content_type)

        try:
            if content_type == "image":
                return await self._gen_image(prompt)
            else:
                return await self._gen_video(prompt)
        except Exception as e:
            logger.warning(f"内容生成失败，降级处理: {e}")
            return self._fallback_result(prompt, content_type)

    async def _gen_image(self, prompt: str) -> dict:
        # 优先走智谱 CogView-3-Flash
        if settings.GLM_API_KEY:
            try:
                resp = await self.client.post(
                    f"{settings.GLM_BASE_URL}/images/generations",
                    headers={
                        "Authorization": f"Bearer {settings.GLM_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "cogview-3-flash",
                        "prompt": prompt,
                        "size": "1024x1024",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return {
                    "type": "image",
                    "url": data["data"][0]["url"],
                    "prompt": prompt,
                    "success": True,
                    "provider": "cogview-3-flash",
                }
            except Exception as e:
                logger.warning(f"CogView 生成失败: {e}，尝试即梦AI")

        # 降级到即梦AI
        if not settings.JIMENG_TOKEN:
            return self._fallback_result(prompt, "image")

        resp = await self.client.post(
            f"{settings.JIMENG_BASE_URL}/v1/images/generations",
            headers={
                "Authorization": f"Bearer {settings.JIMENG_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"model": "jimeng-2.1", "prompt": prompt, "n": 1,
                  "size": "1024x1024", "response_format": "url"},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "type": "image",
            "url": data["data"][0]["url"],
            "prompt": prompt,
            "success": True,
            "provider": "jimeng",
        }

    async def _gen_video(self, prompt: str) -> dict:
        resp = await self.client.post(
            f"{settings.JIMENG_BASE_URL}/v1/videos/generations",
            headers={
                "Authorization": f"Bearer {settings.JIMENG_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "model": "jimeng-video-3.0",
                "prompt": prompt,
                "duration": 3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "type": "video",
            "url": data["data"][0]["url"],
            "prompt": prompt,
            "success": True,
        }

    def _fallback_result(self, prompt: str, content_type: str) -> dict:
        """降级策略 — 生成SVG占位图，不阻塞业务"""
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512">
        <rect width="512" height="512" fill="#f0f4f8"/>
        <text x="256" y="240" font-size="20" text-anchor="middle" fill="#4a5568">
        图片生成服务暂不可用</text>
        <text x="256" y="280" font-size="14" text-anchor="middle" fill="#718096">
        {prompt[:30]}</text></svg>'''
        return {
            "type": content_type,
            "url": f"data:image/svg+xml;base64,{self._b64(svg)}",
            "prompt": prompt,
            "success": False,
            "fallback": True,
        }

    def _b64(self, text: str) -> str:
        import base64
        return base64.b64encode(text.encode()).decode()

    def _validate_output(self, result: dict):
        if "url" not in result:
            raise ValueError("内容生成结果缺少url")

    async def close(self):
        await self.client.aclose()