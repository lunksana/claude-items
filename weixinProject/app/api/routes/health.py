from fastapi import APIRouter
from sqlalchemy import text

from app.core.db import SessionLocal

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok"}
