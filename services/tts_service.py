"""
服务端 TTS 服务 — 基于 edge-tts（免费，无需 API Key）
未装 edge-tts 或生成失败时优雅降级。
"""
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 音频缓存目录
AUDIO_CACHE_DIR = Path("data/audio_cache")
AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 尝试导入 edge-tts
_EDGE_TTS_AVAILABLE = False
try:
    import edge_tts
    _EDGE_TTS_AVAILABLE = True
    logger.info("[TTSService] edge-tts 已加载")
except ImportError:
    logger.info("[TTSService] edge-tts 未安装，服务端 TTS 禁用")


# 语言 → voice 映射
VOICE_MAP = {
    "en-US": "en-US-JennyNeural",
    "en-GB": "en-GB-SoniaNeural",
    "en": "en-US-JennyNeural",
    "zh-CN": "zh-CN-XiaoxiaoNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
}


class TTSService:
    def __init__(self):
        self.available = _EDGE_TTS_AVAILABLE

    def _cache_key(self, text: str, lang: str) -> str:
        return hashlib.sha256(f"{lang}:{text}".encode("utf-8")).hexdigest()[:16]

    def _cache_path(self, text: str, lang: str) -> Path:
        return AUDIO_CACHE_DIR / f"{self._cache_key(text, lang)}.mp3"

    async def generate(self, text: str, lang: str = "en-US") -> dict:
        """
        生成音频并缓存，返回：
          {"success": True, "url": "/api/tts/audio/<key>.mp3", "cached": bool}
        或：
          {"success": False, "message": "..."}
        """
        if not text:
            return {"success": False, "message": "文本为空"}

        # 长度限制（避免 TTS 过长）
        if len(text) > 2000:
            text = text[:2000]

        cache_file = self._cache_path(text, lang)

        # 1. 缓存命中
        if cache_file.exists() and cache_file.stat().st_size > 0:
            return {
                "success": True,
                "url": f"/api/tts/audio/{cache_file.name}",
                "cached": True,
                "size": cache_file.stat().st_size,
            }

        # 2. edge-tts 不可用
        if not self.available:
            return {
                "success": False,
                "message": "服务端 TTS 未启用（请安装 edge-tts）",
            }

        # 3. 生成音频
        try:
            voice = VOICE_MAP.get(lang) or VOICE_MAP.get(lang[:2]) or "en-US-JennyNeural"
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(cache_file))

            if cache_file.exists() and cache_file.stat().st_size > 0:
                return {
                    "success": True,
                    "url": f"/api/tts/audio/{cache_file.name}",
                    "cached": False,
                    "size": cache_file.stat().st_size,
                }
            else:
                return {"success": False, "message": "音频生成失败（空文件）"}

        except Exception as e:
            logger.error(f"[TTSService] 生成失败: {e}")
            return {"success": False, "message": f"TTS 生成异常: {e}"}

    def cleanup_cache(self, max_files: int = 500, max_age_days: int = 30):
        """清理过期的音频缓存"""
        import time
        files = sorted(
            AUDIO_CACHE_DIR.glob("*.mp3"),
            key=lambda f: f.stat().st_mtime,
        )
        now = time.time()
        removed = 0

        for f in files:
            age_days = (now - f.stat().st_mtime) / 86400
            if age_days > max_age_days or len(files) - removed > max_files:
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    continue

        return removed


tts_service = TTSService()