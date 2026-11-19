from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

PlayerStatus = Literal["joining", "online", "afk", "offline"]


class RustAppHandshake(BaseModel):
    centrifugo_token: SecretStr = Field(min_length=1)


class RustAppPlayerStatusUpdate(BaseModel):
    model_config = ConfigDict(strict=True)

    players: dict[str, PlayerStatus]
