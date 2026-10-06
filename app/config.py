from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    APP_NAME: str = "遗产日活动排班管理系统"
    APP_VERSION: str = "1.0.0"
    DATABASE_URL: str = "sqlite:///./heritage_scheduling.db"
    DEBUG: bool = True

    STARTER_THRESHOLD: int = 5
    WARNING_STAFF_SHORTAGE_THRESHOLD: float = 0.3

    # 资源暂占超时时间（分钟），超时未确认的暂占自动过期释放
    RESOURCE_HOLD_TTL_MINUTES: int = 60 * 24

    class Config:
        env_file = ".env"


settings = Settings()
