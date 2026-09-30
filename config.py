import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # LLM配置 - 阿里云百炼（主）
    BAILIAN_API_KEY = os.getenv("BAILIAN_API_KEY", "")
    BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    BAILIAN_MODEL = "qwen-turbo"  # 使用免费额度覆盖的模型

    # LLM配置 - 智谱GLM（备）
    GLM_API_KEY = os.getenv("GLM_API_KEY", "")
    GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
    GLM_MODEL = "glm-4-flash"

    # 文生图 - 即梦AI Free
    JIMENG_BASE_URL = os.getenv("JIMENG_BASE_URL", "http://localhost:3000")
    JIMENG_TOKEN = os.getenv("JIMENG_TOKEN", "")

    # 服务器配置
    HOST = "0.0.0.0"
    PORT = 8000

    # 重试策略（硬性上限）
    MAX_RETRIES = {
        "transient": 3,
        "semantic": 2,
        "tool": 2,
        "business": 0,
    }

    # 并发限制（2核服务器严格限制）
    MAX_CONCURRENT_AGENTS = 2
    LLM_TIMEOUT = 30

settings = Settings()