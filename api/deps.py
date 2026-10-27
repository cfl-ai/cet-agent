"""跨路由复用的依赖：管理接口鉴权、用户 ID 提取。

历史问题：鉴权逻辑原本只写在 main.py 内，新增路由无法复用；
且使用普通字符串比较（非恒定时间）。这里集中提供，并用 hmac.compare_digest。
"""
import hmac

from fastapi import Header, HTTPException

from config import settings


async def require_admin(x_admin_token: str = Header(default=None)):
    """管理接口统一鉴权。

    - 服务端未配置 ADMIN_TOKEN 时直接拒绝（503），避免接口裸奔
    - 令牌不匹配返回 401（恒定时间比较，避免时序侧信道）
    """
    if not settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 ADMIN_TOKEN，管理接口已禁用",
        )
    if not x_admin_token or not hmac.compare_digest(
        str(x_admin_token), str(settings.ADMIN_TOKEN)
    ):
        raise HTTPException(status_code=401, detail="管理接口鉴权失败")
