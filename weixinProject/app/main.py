from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import settings

app = FastAPI(title="Weixin S2b2c API", version="0.1.0")
app.include_router(api_router, prefix="/api/v1")


@app.get("/")
async def root() -> dict[str, str]:
    return {"app": "weixin-s2b2c", "env": settings.app_env}
