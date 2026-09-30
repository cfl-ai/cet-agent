"""翻译服务 — LLM驱动，支持多语种互译"""
from services.llm_client import llm_client

LANG_NAMES = {
    "zh": "中文", "en": "英语", "ja": "日语",
    "ko": "韩语", "es": "西班牙语", "fr": "法语",
    "de": "德语", "ru": "俄语",
}

class TranslateService:

    async def translate(self, text: str, source: str, target: str) -> dict:
        src_name = LANG_NAMES.get(source, source)
        tgt_name = LANG_NAMES.get(target, target)

        prompt = f"""将以下{src_name}翻译成{tgt_name}。只输出译文，不要解释。

原文：
{text}"""

        try:
            result = await llm_client.chat([
                {"role": "system", "content": f"你是专业{src_name}-{tgt_name}翻译。"},
                {"role": "user", "content": prompt},
            ], temperature=0.2, max_tokens=800)
            return {"success": True, "translation": result.strip(),
                    "source": source, "target": target}
        except Exception as e:
            return {"success": False, "translation": "", "message": str(e)}

    async def translate_cet(self, chinese_text: str) -> dict:
        """四六级翻译专用 — 按官方标准风格"""
        prompt = f"""这是四六级汉译英练习题。请按官方参考答案风格翻译，要求：
1. 准确传达原文意思
2. 使用恰当的句式，避免中式英语
3. 词汇难度符合六级水平

原文：
{chinese_text}

请按JSON输出：
{{"translation": "参考译文", "key_points": ["要点1", "要点2"], "vocabulary": [{{"word": "词汇", "meaning": "释义"}}]}}"""

        import json, re
        try:
            result = await llm_client.chat([
                {"role": "system", "content": "你是四六级翻译专家。"},
                {"role": "user", "content": prompt},
            ], temperature=0.3)
            result = result.strip()
            if result.startswith("```"):
                result = re.sub(r"^```(?:json)?\n?", "", result)
                result = re.sub(r"\n?```$", "", result)
            return {"success": True, **json.loads(result)}
        except Exception as e:
            return {"success": False, "message": str(e)}

translate_service = TranslateService()