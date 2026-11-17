from pydantic import BaseModel, ConfigDict, Field

MAX_PLAYERS_PAGE_SIZE = 200

class RustAppPlayer(BaseModel):
    model_config = ConfigDict(strict=True)
    
    steam_id: str = Field(min_length=1)
    project_id: int = Field(gt=0)
    
class RustAppPlayersPage(BaseModel):
    model_config = ConfigDict(strict=True)
    
    page: int = Field(ge=0)
    limit: int = Field(ge=1, le=MAX_PLAYERS_PAGE_SIZE)
    results: list[RustAppPlayer]