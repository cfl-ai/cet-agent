"""业务路由模块包。

拆分原则：main.py 只保留应用装配与既有接口，新增的能力按领域放在 routers/ 下，
每个模块暴露一个 `router`（fastapi.APIRouter），由 main.py include_router 挂载。
"""