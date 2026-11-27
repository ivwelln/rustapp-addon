from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

PlayerStatus = Literal["joining", "online", "afk", "offline"]


class RustAppHandshake(BaseModel):
    centrifugo_token: SecretStr = Field(min_length=1)


class RustAppPlayerStatusUpdate(BaseModel):
    model_config = ConfigDict(strict=True)

    players: dict[str, PlayerStatus]


class RustAppReportTarget(BaseModel):
    steam_id: str


class RustAppReportPublication(BaseModel):
    model_config = ConfigDict(strict=True)

    id: int = Field(gt=0, le=2**63 - 1)
    created_at: int = Field(gt=0, le=2**63 - 1)
    project_id: int | None = None
    target: RustAppReportTarget
