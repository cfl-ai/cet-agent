import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # ---- LLM 主 ----
    BAILIAN_API_KEY = os.getenv("BAILIAN_API_KEY", "")
    BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    BAILIAN_MODEL = os.getenv("BAILIAN_MODEL", "qwen3.7-plus")

    # ---- LLM 备 ----
    GLM_API_KEY = os.getenv("GLM_API_KEY", "")
    GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
    GLM_MODEL = os.getenv("GLM_MODEL", "glm-4-flash")

    # ---- 文生图 ----
    JIMENG_BASE_URL = os.getenv("JIMENG_BASE_URL", "http://localhost:3000")
    JIMENG_TOKEN = os.getenv("JIMENG_TOKEN", "")

    # ---- 服务器 ----
    HOST = "0.0.0.0"
    PORT = 8001

    # ---- 重试 ----
    MAX_RETRIES = {
        "transient": 3, "semantic": 2, "tool": 2, "business": 0,
    }

    # ---- 并发 ----
    MAX_CONCURRENT_AGENTS = 2
    LLM_TIMEOUT = 60

    # ---- 新功能开关（默认全开，装不上依赖会自动降级）----
    ENABLE_TRACING = os.getenv("ENABLE_TRACING", "1") == "1"
    ENABLE_VECTOR = os.getenv("ENABLE_VECTOR", "1") == "1"
    ENABLE_GRAPH = os.getenv("ENABLE_GRAPH", "1") == "1"

    # ---- OTel ----
    OTEL_ENDPOINT = os.getenv("OTEL_ENDPOINT", "")

    # ---- 向量库 ----
    VECTOR_DIR = os.getenv("VECTOR_DIR", "data/chroma")


settings = Settings()