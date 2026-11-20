from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_PLAYERS_PAGE_SIZE = 200

GameClient = Literal["pirate", "license"]

class RustAppPlayer(BaseModel):
    model_config = ConfigDict(strict=True)

    steam_id: str = Field(min_length=1)
    project_id: int = Field(gt=0)
    status: str | None = None
    
    # TODO: client? Check response from RustApp.
    # client: GameClient
    
    
    


class RustAppPlayersPage(BaseModel):
    model_config = ConfigDict(strict=True)

    page: int = Field(ge=0)
    limit: int = Field(ge=1, le=MAX_PLAYERS_PAGE_SIZE)
    results: list[RustAppPlayer]
