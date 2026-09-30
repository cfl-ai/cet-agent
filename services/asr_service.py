"""轻量级ASR — 优先使用Moonshine ONNX，降级到浏览器Web Speech API"""
import base64
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class ASRService:
    """
    策略：
    1. 若安装了moonshine-onnx，本地推理（27M参数，CPU可跑）
    2. 否则返回提示，由前端Web Speech API兜底
    """

    def __init__(self):
        self.available = False
        try:
            # 可选依赖，未安装则自动降级
            import moonshine_onnx  # noqa
            self.available = True
            logger.info("Moonshine ASR 已加载")
        except ImportError:
            logger.info("Moonshine 未安装，使用前端Web Speech API降级")

    async def transcribe(self, audio_base64: str, language: str = "en") -> dict:
        if not self.available:
            return {
                "success": False,
                "text": "",
                "message": "服务端ASR未启用，请前端使用Web Speech API",
                "fallback": "web_speech_api",
            }

        try:
            audio_bytes = base64.b64decode(audio_base64)
            tmp_path = Path("/tmp/asr_input.wav")
            tmp_path.write_bytes(audio_bytes)

            from moonshine_onnx import transcribe as moonshine_transcribe
            text = moonshine_transcribe(str(tmp_path))
            return {"success": True, "text": text, "language": language}
        except Exception as e:
            logger.error(f"ASR失败: {e}")
            return {"success": False, "text": "", "message": str(e)}

asr_service = ASRService()