from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

from app.services.moderation.facts import Patterns


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        frozen=True,
        secrets_dir="/run/secrets",
        extra="ignore",
        hide_input_in_errors=True,
    )

    rustapp_access_token: SecretStr = Field(min_length=1)
    rustapp_project_id: int = Field(default=9867, gt=0)
    rustapp_timeout: float = Field(default=15, gt=0, allow_inf_nan=False)


class DatabaseSettings(BaseSettings):
    model_config = Settings.model_config

    db_user: str = "rustapp_app"
    db_host: str = "db"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "rustapp_addon"
    db_password: SecretStr = Field(min_length=1)

    @property
    def db_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


class ModerationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_MODERATION_", env_nested_delimiter="__", frozen=True, extra="ignore"
    )
    server_id: int = Field(default=163, gt=0)
    enforce: bool = False
    ban_guard_url: str = "http://ban-guard:8000"
    poll_seconds: int = Field(default=150, ge=1)
    initial_ban_pages: int = Field(default=1, ge=1)
    request_interval: float = Field(default=1.0, gt=0)
    patterns: Patterns = Patterns()
