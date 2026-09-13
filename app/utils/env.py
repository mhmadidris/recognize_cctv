from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import Optional

class EnvSettings(BaseSettings):
    IS_PRODUCTION: bool = False
    IS_DEBUG: bool = False
    
    DB_HOST: str = ""
    DB_PORT: Optional[int] = None
    DB_USERNAME: str = ""
    DB_PASSWORD: str = ""
    DB_NAME: str = ""
    DB_TYPE: str = ""
    FIREBASE_STORAGE_BUCKET_URL: Optional[str] = None
    CCTV_MAX_CAMERAS_PER_COMPANY: Optional[int] = None

    @field_validator("DB_PORT", mode="before")
    @classmethod
    def parse_db_port(cls, v):
        if v == "" or v is None:
            return None
        return int(v)

    class Config:
        env_file = ".env"
        extra = "ignore"

env = EnvSettings()
