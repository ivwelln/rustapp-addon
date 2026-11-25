from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MINUTE = 60_000


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["off", "observe", "review", "ban"] = "ban"
    version: int = Field(default=1, ge=1)


class Registration(Rule):
    tolerance_seconds: int = Field(default=60, ge=0)


class RecentBan(Rule):
    minutes: int = Field(default=60, gt=0)


class Series(Rule):
    victims: int = Field(default=3, gt=0)
    minutes: int = Field(default=30, gt=0)


class EarlyKills(Series):
    minutes: int = Field(default=10, gt=0)


class AfterBan(RecentBan):
    minutes: int = Field(default=15, gt=0)


class Distance(Series):
    meters: float = Field(default=50, ge=0)


class HeadHits(Series):
    hits: int = Field(default=2, ge=1)


class Ratio(Rule):
    minutes: int = Field(default=30, gt=0)
    kills: int = Field(default=5, gt=0)
    meters: float = Field(default=20, ge=0)
    percent: int = Field(default=80, ge=1, le=100)


class FirstHour(Series):
    age_minutes: int = Field(default=60, gt=0)


class Patterns(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    P01: Registration = Registration()
    P02: RecentBan = RecentBan()
    P03: EarlyKills = EarlyKills()
    P04: AfterBan = AfterBan()
    P05: Distance = Distance()
    P06: HeadHits = HeadHits()
    P07: Ratio = Ratio()
    P09: FirstHour = FirstHour()


class Kill(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    id: int
    created_at: int
    victim: str
    weapon: str
    head: bool
    distance: float | None
    head_hits_only: int

    @property
    def firearm(self) -> bool:
        return self.weapon.startswith(("rifle.", "smg.", "lmg.")) or self.weapon in {
            "m16a2",
            "hmlmg",
            "t1_smg",
        }


class Session(BaseModel):
    model_config = ConfigDict(strict=True)
    project_id: int
    steam_id: str
    server_id: int
    connected_at: int
    joined_at: int | None = None
    disconnected_at: int | None = None
    is_license: bool | None = None

    @model_validator(mode="after")
    def check_times(self):
        if self.joined_at is not None and self.joined_at < self.connected_at:
            # Some historical panel records contain a stale joined_at from an earlier session.
            self.joined_at = None
        return self


@dataclass(frozen=True)
class Ban:
    id: int
    steam_id: str
    at: int
    signed_at: int | None
    server_id: int | None
    pirated: bool


@dataclass(frozen=True)
class Facts:
    player: str
    session: Session
    first_seen: int
    first_join: bool
    signed_at: int | None
    private_without_date: bool
    at: int
    kills: tuple[Kill, ...]
    bans: tuple[Ban, ...]


def recent_kills(facts: Facts, minutes: int) -> list[Kill]:
    return [k for k in facts.kills if facts.at - minutes * MINUTE <= k.created_at <= facts.at]


def distinct(kills: list[Kill], minimum: int) -> dict | None:
    if len({k.victim for k in kills}) >= minimum:
        return {"kills": [k.model_dump() for k in kills]}
    return None
