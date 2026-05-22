from decimal import Decimal
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    database_url: str = "postgresql+asyncpg://weixin:weixin@localhost:5432/weixin"
    secret_key: str = "change-me-in-production"

    wechat_miniapp_appid: str = ""
    wechat_miniapp_secret: str = ""

    wechat_pay_mch_id: str = ""
    wechat_pay_api_v3_key: str = ""
    wechat_pay_cert_path: str = ""
    wechat_pay_notify_url: str = ""

    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "weixin-dev"
    s3_region: str = "us-east-1"

    # 二级分销 cap. See CLAUDE.md red-line section.
    commission_tier1_rate: Decimal = Field(default=Decimal("0.10"))
    commission_tier2_rate: Decimal = Field(default=Decimal("0.05"))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
