from ipaddress import ip_address

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.player_data import PlayerData, PlayerIP, PlayerReport, PlayerSession
from app.services.moderation.facts import Session


async def save_response(
    db: AsyncSession,
    project: int,
    steam_id: str,
    source: str,
    observed_at: int,
    payload: dict,
    *,
    params: dict | None = None,
) -> None:
    statement = insert(PlayerData).values(
        project_id=project,
        steam_id=steam_id,
        updated_at=observed_at,
        responses={source: {"observed_at": observed_at, "payload": payload, "params": params}},
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[PlayerData.project_id, PlayerData.steam_id],
            set_={
                "updated_at": func.greatest(PlayerData.updated_at, statement.excluded.updated_at),
                "responses": PlayerData.responses.op("||")(statement.excluded.responses),
            },
        )
    )


async def save_ip(
    db: AsyncSession, project: int, steam_id: str, address: object, observed_at: int, payload: dict
) -> None:
    if not isinstance(address, str):
        return
    try:
        normalized = str(ip_address(address))
    except ValueError:
        return
    statement = insert(PlayerIP).values(
        project_id=project,
        steam_id=steam_id,
        address=normalized,
        first_seen_at=observed_at,
        last_seen_at=observed_at,
        payload=payload,
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[PlayerIP.project_id, PlayerIP.steam_id, PlayerIP.address],
            set_={
                "first_seen_at": func.least(
                    PlayerIP.first_seen_at, statement.excluded.first_seen_at
                ),
                "last_seen_at": func.greatest(
                    PlayerIP.last_seen_at, statement.excluded.last_seen_at
                ),
                "payload": PlayerIP.payload.op("||")(statement.excluded.payload),
            },
        )
    )


async def stored_sessions(db: AsyncSession, project: int, steam_id: str) -> list[Session]:
    payloads = await db.scalars(
        select(PlayerSession.payload)
        .where(
            PlayerSession.project_id == project,
            PlayerSession.steam_id == steam_id,
        )
        .order_by(PlayerSession.connected_at)
    )
    return [Session.model_validate(payload) for payload in payloads]


async def save_reports(db: AsyncSession, project: int, steam_id: str, reports: list[dict]) -> None:
    for report in reports:
        if report.get("target_steam_id") != steam_id:
            continue
        statement = insert(PlayerReport).values(
            project_id=project,
            steam_id=steam_id,
            report_id=report["id"],
            created_at=report["created_at"],
            payload=report,
        )
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[PlayerReport.project_id, PlayerReport.report_id],
                set_={"payload": statement.excluded.payload},
            )
        )
