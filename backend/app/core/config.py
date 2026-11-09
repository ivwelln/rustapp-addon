from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
