from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rustapp.exceptions import RustAppResponseError
from app.clients.rustapp.moderation import is_player, parse_kill, results
from app.models import Observation, PlayerData, ResearchCase, ResearchSource
from app.services.research.playtime import DAY, daily_playtime


@dataclass(frozen=True)
class Snapshot:
    payload: dict
    params: dict | None
    observed_at: int
    origin: str


@dataclass(frozen=True)
class Sample:
    payload: dict
    features: dict | None
    covered_from: int | None = None
    covered_to: int | None = None
    complete: bool | None = None


def combat_sample(case: ResearchCase, source: ResearchSource, snap: Snapshot) -> Sample | None:
    rows = results(snap.payload)
    start, end = source.params["time_from"], source.params["time_to"]
    params = snap.params or {}
    if params.get("page", 0) != 0 or set(params) - {"page", "limit", "time_from", "time_to"}:
        return None
    limit = snap.payload.get("limit", params.get("limit", 25))
    times = [row.get("created_at") for row in rows]
    if (
        type(limit) is not int
        or not 1 <= limit
        or len(rows) > limit
        or any(type(at) is not int for at in times)
        or times != sorted(times, reverse=True)
        or any(row.get("project_id") != case.project_id for row in rows)
    ):
        raise RustAppResponseError("Некорректная боевая лента")
    left = params.get("time_from", times[-1] + 1 if times else None)
    right = params.get("time_to", times[0] if times else None)
    if left is None or right is None or left > start or right < end:
        return None
    total = snap.payload.get("total")
    if total is not None and (type(total) is not int or total < len(rows)):
        raise RustAppResponseError("Некорректный размер боевой ленты")
    truncated = len(rows) == limit if total is None else total > len(rows)
    if truncated and not times:
        raise RustAppResponseError("В боевой ленте пропущены события")
    covered = max(start, times[-1] + 1) if truncated else start
    if covered > start and (left, right) != (start, end):
        # A narrower, single-page request can recover the missing early minutes.
        return None
    events, kills, deaths = [], [], []
    for row in rows:
        if row.get("server_id") != case.server_id or not start <= row["created_at"] <= end:
            continue
        attacker = (row.get("initiator") or {}).get("steam_id")
        victim = (row.get("target") or {}).get("steam_id")
        if case.steam_id not in (attacker, victim):
            continue
        events.append(row)
        if kill := parse_kill(row, case.steam_id):
            kills.append(kill.model_dump())
        elif victim == case.steam_id and attacker != victim and is_player(attacker):
            deaths.append({"id": row["id"], "at": row["created_at"]})
    return Sample(
        {"results": events},
        {"kills": kills, "deaths": deaths},
        covered if covered <= end else None,
        end,
        covered == start,
    )


def prepare(case: ResearchCase, source: ResearchSource, snap: Snapshot) -> Sample | None:
    """A source can be recorded after the cutoff. Only dated evidence enters features."""
    raw, kind, cutoff = snap.payload, source.source, case.cutoff_at
    if not isinstance(raw, dict):
        raise RustAppResponseError("Некорректный ответ для среза")
    if "http_status" in raw:
        return None
    if kind == "kill-feed":
        return combat_sample(case, source, snap)
    if kind == "stats":
        if snap.params != source.params:
            return None
        days = daily_playtime(raw, case.steam_id)
        start = source.params["time_from"]
        eligible = {
            str(day): value
            for day, value in days.items()
            if start <= day and day + DAY - 1 <= cutoff
        }
        expected = range((start + DAY - 1) // DAY * DAY, cutoff - DAY + 2, DAY)
        return Sample(
            raw,
            {"played_ms_by_day": eligible},
            expected.start if expected else None,
            expected[-1] + DAY - 1 if expected else None,
            bool(expected) and all(day in days for day in expected),
        )
    if kind == "details":
        profile = raw.get("player")
        if not isinstance(profile, dict) or (
            profile.get("project_id"),
            profile.get("steam_id"),
        ) != (
            case.project_id,
            case.steam_id,
        ):
            raise RustAppResponseError("Получен чужой профиль")
        features = None
        if snap.observed_at <= cutoff:
            steam = profile.get("steam_data") or {}
            network = profile.get("ip_details") or {}
            features = {
                "profile_created_at": profile.get("created_at"),
                "signed_at": steam.get("signed_at"),
                "steam_visible": steam.get("visible"),
                "language": profile.get("last_language"),
                "country": network.get("country_code"),
                "provider": network.get("provider"),
            }
        return Sample({"player": profile}, features)
    if kind == "checks":
        count = raw.get("total")
        if type(count) is not int or count < 0:
            raise RustAppResponseError("Нет количества проверок")
        return Sample({"total": count}, {"total": count} if snap.observed_at <= cutoff else None)

    rows = results(raw)
    limit = raw.get("limit", 20)
    total = raw.get("total")
    if (
        type(limit) is not int
        or limit <= 0
        or len(rows) > limit
        or (total is not None and (type(total) is not int or total < len(rows)))
    ):
        raise RustAppResponseError("Некорректная страница истории")
    complete = total == len(rows) if total is not None else len(rows) < limit
    records, features = [], []
    for row in rows:
        if kind == "reports":
            if (
                row.get("target_steam_id") != case.steam_id
                or row.get("server_id") != case.server_id
            ):
                continue
            records.append(row)
            at = row.get("created_at")
            if type(at) is int and at <= cutoff:
                features.append(
                    {
                        key: row.get(key)
                        for key in ("id", "created_at", "reason", "initiator_steam_id")
                    }
                )
        elif kind == "ip-history":
            if (
                row.get("project_id", case.project_id) != case.project_id
                or row.get("steam_id", case.steam_id) != case.steam_id
            ):
                continue
            records.append(row)
            at = row.get("last_seen_at") or row.get("created_at")
            if type(at) is int and at <= cutoff:
                features.append({"ip": row.get("ip"), "at": at})
        else:
            raise ValueError("Неизвестный раздел среза")
    # Preserve later report/IP payloads in the sample only as explicitly dated raw data.
    return Sample(
        {"results": records, "source_total": total},
        {"events": features},
        None,
        min(cutoff, snap.observed_at),
        complete,
    )


async def cached_sample(
    db: AsyncSession,
    case: ResearchCase,
    source: ResearchSource,
) -> tuple[Snapshot, Sample] | None:
    kind = source.source
    names = [kind, "research/" + kind]
    if kind == "details":
        names.extend(("browse-full", "research/browse-full"))
    query = select(Observation).where(
        Observation.project_id == case.project_id,
        Observation.steam_id == case.steam_id,
        Observation.source.in_(names),
    )
    cache = await db.get(PlayerData, (case.project_id, case.steam_id))
    candidates = []
    if cache:
        for name in names:
            value = cache.responses.get(name)
            if value:
                payload = value["payload"]
                if name.endswith("browse-full"):
                    payload = {"player": payload}
                candidates.append(
                    Snapshot(payload, value.get("params"), value["observed_at"], "cache")
                )
    queries = [
        query.where(Observation.observed_at <= case.cutoff_at).order_by(
            Observation.observed_at.desc()
        ),
        query.where(Observation.observed_at > case.cutoff_at).order_by(Observation.observed_at),
    ]
    if kind in ("kill-feed", "stats"):
        queries.insert(
            0,
            query.where(
                Observation.request_params["time_to"].astext == str(source.params["time_to"])
            ).order_by(Observation.observed_at),
        )
    for lookup in queries:
        for record in await db.scalars(lookup.limit(8)):
            payload = record.payload
            if record.source.endswith("browse-full"):
                payload = {"player": payload}
            candidates.append(
                Snapshot(
                    payload,
                    record.request_params,
                    record.observed_at,
                    "archive" if record.source.startswith("research/") else "cache",
                )
            )
    candidates.sort(
        key=lambda item: (item.observed_at > case.cutoff_at, abs(item.observed_at - case.cutoff_at))
    )
    for candidate in candidates:
        try:
            sample = prepare(case, source, candidate)
        except (KeyError, TypeError, AttributeError, ValueError, RustAppResponseError):
            continue
        if sample is not None:
            return candidate, sample
    return None


def finish(source: ResearchSource, snapshot: Snapshot, sample: Sample) -> None:
    source.status = "ready"
    source.lease = source.error = None
    source.origin = snapshot.origin
    source.observed_at = snapshot.observed_at
    source.payload, source.features = sample.payload, sample.features
    source.covered_from, source.covered_to, source.complete = (
        sample.covered_from,
        sample.covered_to,
        sample.complete,
    )
