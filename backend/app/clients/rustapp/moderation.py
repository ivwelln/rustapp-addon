from pydantic import BaseModel, ConfigDict, Field

from app.clients.rustapp.client import RustAppClient
from app.clients.rustapp.exceptions import RustAppResponseError
from app.services.moderation.facts import Kill


class Profile(BaseModel):
    model_config = ConfigDict(strict=True)
    steam_id: str
    project_id: int
    created_at: int = Field(gt=0)
    steam: dict | None = None
    steam_data: dict | None = None

    @property
    def signed_at(self) -> int | None:
        value = (self.steam_data or {}).get("signed_at")
        return value if type(value) is int and value > 0 else None

    @property
    def private_without_date(self) -> bool:
        return (
            (self.steam or {}).get("profile_private") is True
            and self.steam_data is not None
            and self.steam_data.get("visible") is False
            and self.steam_data.get("signed_at") is None
        )


def is_player(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 17
        and value.startswith("7656")
        and value.isascii()
        and value.isdigit()
    )


def parse_kill(row: dict, sid: str) -> Kill | None:
    victim = (row.get("target") or {}).get("steam_id")
    if (
        (row.get("initiator") or {}).get("steam_id") != sid
        or not is_player(victim)
        or victim == sid
    ):
        return None
    hits = row.get("hit_history")
    damaging = []
    for h in hits if isinstance(hits, list) else ():
        if not isinstance(h, dict):
            damaging = []
            break
        if h.get("attacker_steam_id") == sid and h.get("target_steam_id") == victim:
            if not all(type(h.get(key)) in (int, float) for key in ("hp_old", "hp_new")):
                damaging = []
                break
            if h["hp_old"] > h["hp_new"]:
                damaging.append(h)
    return Kill(
        id=row["id"],
        created_at=row["created_at"],
        victim=victim,
        weapon=row.get("weapon") or "",
        head=row.get("kill_bone") == "head",
        distance=row.get("distance"),
        head_hits_only=len(damaging) if all(h.get("bone") == "head" for h in damaging) else 0,
    )


async def read(client: RustAppClient, path: str, **params) -> dict:
    response = await client.get(path, params=params)
    data = response.json()
    if not isinstance(data, dict):
        raise RustAppResponseError("Неожиданный формат ответа панели")
    return data


def results(payload: dict, *, page: int = 0) -> list[dict]:
    if payload.get("page") != page or not isinstance(payload.get("results"), list):
        raise RustAppResponseError("Неожиданная страница RustApp")
    return payload["results"]
