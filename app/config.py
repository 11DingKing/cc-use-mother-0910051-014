from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    APP_NAME: str = "遗产日活动排班管理系统"
    APP_VERSION: str = "1.0.0"
    DATABASE_URL: str = "sqlite:///./heritage_scheduling.db"
    DEBUG: bool = True

    STARTER_THRESHOLD: int = 5
    WARNING_STAFF_SHORTAGE_THRESHOLD: float = 0.3

    # 草稿场次资源暂占有效期（秒），超时未排定自动释放
    DRAFT_HOLD_TTL_SECONDS: int = 1800
    # 变更申请在审批流程中的暂占有效期（秒），超时未审批自动释放
    REVIEW_HOLD_TTL_SECONDS: int = 86400
    # 后台过期暂占清扫间隔（秒）
    HOLD_SWEEP_INTERVAL_SECONDS: int = 60
    # 无线设备套装（全局共享池）的总数量
    DEVICE_POOL_CAPACITY: int = 8

    class Config:
        env_file = ".env"


settings = Settings()
