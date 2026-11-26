import asyncio
import os
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.rustapp.client import RustAppClient, create_rustapp_http_client
from app.clients.rustapp.exceptions import ResearchDeferred, RustAppHTTPError, RustAppResponseError
from app.core.config import Settings
from app.crud.moderation import now_ms
from app.crud.presence import confirm_presence_connection, save_presence
from app.crud.rustapp_requests import account_key, record_request
from app.models import (
    CollectionJob,
    Player,
    PlayerPlaytime,
    PlaytimeDay,
    ResearchCase,
    ResearchSource,
    RustAppGate,
    RustAppRequest,
)
from app.services.moderation.eligibility import annual_ms, hours_proof_is_current
from app.services.research.cases import add_case, source_params, sync_cases
from app.services.research.playtime import DAY, HOUR, cache_days, missing_range, refresh_hours
from app.services.research.queue import claim_hours, claim_source, hydrate, store_work
from app.services.research.snapshots import Snapshot, prepare
from app.workers.import_research import save_archived

SID = "76561198000000007"
SERVER = 163
database = pytest.mark.skipif(
    not os.getenv("MODERATION_TEST_DB_URL"), reason="Требуется тестовая PostgreSQL"
)


def stats(start, end, hours=0):
    days = {
        str(day): {"date": day, "hours_played": 0}
        for day in range(start // DAY * DAY, end + 1, DAY)
    }
    days[max(days)]["hours_played"] = hours
    return {"kills": {"steam_id": SID}, "playTime": {"stats": days, "total": hours}}


def test_annual_window_validates_every_date_and_the_returned_total():
    end = 1000 * DAY + 1200
    start = end - 365 * DAY + 1
    data = stats(start, end, 2.1)
    assert annual_ms(data, SID, start, end) == 2.1 * HOUR
    data["kills"] = {}
    with pytest.raises(RustAppResponseError):
        annual_ms(data, SID, start, end)
    data["kills"]["steam_id"] = SID
    data["playTime"]["total"] = 0
    with pytest.raises(RustAppResponseError):
        annual_ms(data, SID, start, end)
    data["playTime"]["total"] = 2.1
    data["playTime"]["stats"].pop(min(data["playTime"]["stats"]))
    with pytest.raises(RustAppResponseError):
        annual_ms(data, SID, start, end)


def test_cutoff_excludes_later_undated_fields_and_truncated_early_minutes():
    at = 100 * DAY
    case = ResearchCase(project_id=1, server_id=SERVER, steam_id=SID, cutoff_at=at)
    params = source_params(at, at - 1000)
    checks = ResearchSource(source="checks", params=params["checks"])
    assert prepare(case, checks, Snapshot({"total": 3}, {}, at + 1, "cache")).features is None
    feed = ResearchSource(source="kill-feed", params=params["kill-feed"])
    victim = str(int(SID) + 1)
    row = {
        "project_id": 1,
        "server_id": SERVER,
        "id": 1,
        "created_at": at - 100,
        "initiator": {"steam_id": SID},
        "target": {"steam_id": victim},
    }
    raw = {"page": 0, "limit": 1, "results": [row]}
    wide = {**feed.params, "time_from": at - DAY}
    assert prepare(case, feed, Snapshot(raw, wide, at + 1, "cache")) is None
    sample = prepare(case, feed, Snapshot(raw, feed.params, at + 1, "rustapp"))
    assert not sample.complete and sample.covered_from == at - 99
    assert sample.features["kills"][0]["victim"] == victim


def test_send_time_rechecks_the_strict_three_hour_boundary_after_waiting():
    checked = 100 * DAY
    proof = {
        "checked_at": checked,
        "outcome": "eligible",
        "pirated": True,
        "project_ms": 3 * HOUR - 1000,
    }
    assert hours_proof_is_current(proof, checked + 999)
    assert not hours_proof_is_current(proof, checked + 1000)
    assert not hours_proof_is_current({**proof, "project_ms": 0}, checked + 30_001)
    assert not hours_proof_is_current(None, checked)


@database
async def test_day_cache_and_resumable_local_case():
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    project = uuid4().int % (2**30) + 1
    end = now_ms() // DAY * DAY
    at = end - 1
    gap = {"time_from": end - 365 * DAY, "time_to": at}
    try:
        async with sessions.begin() as db:
            data = stats(gap["time_from"], at, 2.1)
            await cache_days(db, project, SID, data, gap, end)
            data["playTime"]["stats"][str(end - DAY)]["hours_played"] = 0
            await cache_days(db, project, SID, data, gap, end - 1)
            assert (await db.get(PlaytimeDay, (project, SID, end - DAY))).played_ms == 2.1 * HOUR
            assert await missing_range(db, project, SID, end) is None
            assert await missing_range(db, project, SID, end + DAY) == {
                "time_from": end,
                "time_to": end + DAY - 1,
            }
            await add_case(
                db,
                project,
                SERVER,
                SID,
                cohort="control",
                episode=0,
                cutoff=at,
                entry=at - 10_000,
                label="control",
            )
            await add_case(
                db,
                project,
                SERVER,
                SID,
                cohort="control",
                episode=0,
                cutoff=at,
                entry=None,
                label="control",
            )
            case = await db.scalar(select(ResearchCase).where(ResearchCase.project_id == project))
            params = source_params(at, case.entry_at)
            payloads = {
                "details": {"player": {"steam_id": SID, "project_id": project}},
                "checks": {"total": 1},
                "reports": {"page": 0, "results": []},
                "kill-feed": {"page": 0, "results": []},
                "stats": stats(params["stats"]["time_from"], params["stats"]["time_to"]),
            }
            for kind, payload in payloads.items():
                assert await save_archived(db, project, SID, kind, payload, params[kind], at + 1)
                assert not await save_archived(
                    db, project, SID, kind, payload, params[kind], at + 1
                )
            assert await hydrate(db, project, SERVER) == 6
            work = await claim_source(db, project, SERVER, end)
            assert work.source == "ip-history"
        async with sessions.begin() as db:
            assert await claim_source(db, project, SERVER, end + 50) is None
            renewed = await claim_source(db, project, SERVER, end + 300_001)
            assert renewed.lease != work.lease
            await store_work(db, project, work, {"page": 0, "results": []}, end)
            assert (await db.get(ResearchSource, (work.case_id, work.source))).status == "leased"
            await store_work(db, project, renewed, {"page": 0, "results": []}, end)
            await db.flush()
            assert await db.scalar(
                text("SELECT trained FROM player_research_status WHERE project_id=:p"),
                {"p": project},
            )
            assert not await db.scalar(
                text(
                    "SELECT count(*) FROM research_dataset WHERE project_id=:p AND source='checks'"
                ),
                {"p": project},
            )
    finally:
        await engine.dispose()


@database
async def test_research_and_live_clients_share_cooldown_and_research_budget():
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    project = uuid4().int % (2**30) + 1
    token = uuid4().hex
    key = account_key(token)
    calls = []

    def reply(request):
        calls.append(request.url.path)
        return httpx2.Response(429, headers={"Retry-After": "75"})

    try:
        async with sessions() as db:
            initial = await db.scalar(select(func.count()).select_from(RustAppRequest))
        outcomes = await asyncio.gather(
            *(record_request(sessions, key, research_project=project) for _ in range(2)),
            return_exceptions=True,
        )
        assert sum(isinstance(result, ResearchDeferred) for result in outcomes) == 1
        now = now_ms()
        async with sessions.begin() as db:
            gate = await db.get(RustAppGate, key)
            gate.research_starts = [now - 500_000 - index for index in reversed(range(120))]
        with pytest.raises(ResearchDeferred, match="суточный"):
            await record_request(sessions, key, research_project=project)
        async with sessions.begin() as db:
            (await db.get(RustAppGate, key)).research_starts = []
            await confirm_presence_connection(db, project, now)
            await save_presence(db, project, {SID: "online"}, now * 1_000_000)
            db.add(
                CollectionJob(
                    project_id=project, steam_id=SID, requested_at=now - 60_000, due_at=now - 60_000
                )
            )
        with pytest.raises(ResearchDeferred, match="Приоритет"):
            await record_request(sessions, key, research_project=project)
        cfg = Settings(rustapp_access_token=token, rustapp_project_id=project)
        for research in (False, True, False):
            async with create_rustapp_http_client(
                cfg, request_log=sessions, research=research, transport=httpx2.MockTransport(reply)
            ) as http:
                with pytest.raises(RustAppHTTPError) as error:
                    await RustAppClient(http).get("/ban/browse")
                assert error.value.status_code == 429
        assert len(calls) == 1
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(RustAppRequest)) == initial + 2
    finally:
        await engine.dispose()


@database
async def test_trusted_active_pirate_can_qualify_on_a_later_day():
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    project, now = uuid4().int % (2**30) + 1, now_ms()
    try:
        async with sessions.begin() as db:
            db.add(
                Player(
                    project_id=project,
                    steam_id=SID,
                    last_client_kind="pirated",
                    is_whitelisted=True,
                )
            )
            db.add(PlayerPlaytime(project_id=project, steam_id=SID, stats_checked_at=now - DAY))
            await confirm_presence_connection(db, project, now)
            await save_presence(db, project, {SID: "afk"}, (now - 1000) * 1_000_000)
            assert await claim_hours(db, project, SERVER, now) is None
            await save_presence(db, project, {SID: "online"}, now * 1_000_000)
            job = await claim_hours(db, project, SERVER, now)
            assert job.source == "stats" and job.params["time_to"] == now // DAY * DAY - 1
            await store_work(
                db, project, job, stats(job.params["time_from"], job.params["time_to"], 21), now
            )
            await refresh_hours(db, project, now)
            await sync_cases(db, project, SERVER, now)
            assert (
                await db.scalar(select(ResearchCase).where(ResearchCase.project_id == project))
            ).hours_source == "rustapp"
            await confirm_presence_connection(db, project, now + DAY)
            assert await claim_hours(db, project, SERVER, now + DAY) is None
    finally:
        await engine.dispose()


@database
@pytest.mark.parametrize("legacy", [False, True])
async def test_control_requires_over_twenty_project_hours_and_excludes_unreviewed_ban(legacy):
    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    project, now = uuid4().int % (2**30) + 1, now_ms()
    try:
        async with sessions.begin() as db:
            db.add(Player(project_id=project, steam_id=SID, last_client_kind="pirated"))
            cache = PlayerPlaytime(
                project_id=project,
                steam_id=SID,
                annual_ms=20 * HOUR,
                annual_checked_at=now,
                project_ms=40 * HOUR,
                calculated_at=now,
            )
            db.add(cache)
            await db.flush()
            if legacy:
                await add_case(
                    db,
                    project,
                    SERVER,
                    SID,
                    cohort="control",
                    episode=0,
                    cutoff=now - DAY,
                    entry=None,
                    label="awaiting_playtime",
                    hours_source="observations",
                    qualifying_ms=24 * HOUR,
                )
            await sync_cases(db, project, SERVER, now)
            query = select(ResearchCase).where(ResearchCase.project_id == project)
            eligible = query.where(ResearchCase.label == "control")
            assert await db.scalar(eligible) is None
            cache.annual_ms += 1
            cache.annual_checked_at = now - 2 * DAY
            await sync_cases(db, project, SERVER, now)
            assert await db.scalar(eligible) is None
            cache.annual_checked_at = now
            await sync_cases(db, project, SERVER, now)
            control = (await db.scalars(query)).one()
            assert control.label == "control" and control.hours_source == "rustapp"
            assert control.qualifying_ms == 20 * HOUR + 1
            if legacy:
                assert control.cutoff_at == now - DAY and control.selected_at == now
            from app.models import ReferenceBan

            ban = ReferenceBan(
                project_id=project,
                ban_id=1,
                steam_id=SID,
                at=now,
                server_id=SERVER,
                pirated=True,
                eligible=True,
                payload={
                    "reason": "чит",
                    "computed_is_active": True,
                    "comment": "rustapp-addon:137:P05",
                },
            )
            db.add(ban)
            await sync_cases(db, project, SERVER, now)
            await db.refresh(control)
            assert control.label == "excluded"
            assert (
                await db.scalar(query.where(ResearchCase.cohort == "ban"))
            ).label == "awaiting_review"
    finally:
        await engine.dispose()


@database
@pytest.mark.parametrize("first_server", [SERVER, SERVER + 1])
async def test_project_stats_protect_and_select_regardless_of_first_server(first_server):
    from app.workers.research_players import maintain

    engine = create_async_engine(os.environ["MODERATION_TEST_DB_URL"])
    sessions = async_sessionmaker(engine)
    project, now = uuid4().int % (2**30) + 1, now_ms()
    end = now // DAY * DAY
    try:
        async with sessions.begin() as db:
            db.add(Player(project_id=project, steam_id=SID, last_client_kind="pirated"))
            await confirm_presence_connection(db, project, now)
            await save_presence(db, project, {SID: "joining"}, (now - 120_000) * 1_000_000)
            await save_presence(
                db,
                project,
                {SID: "online"},
                (now - 119_000) * 1_000_000,
                server_info={SID: (first_server, True)},
            )
            params = {"time_from": end - DAY, "time_to": end - 1}
            await cache_days(
                db, project, SID, stats(params["time_from"], params["time_to"], 21.67), params, now
            )
        await maintain(sessions, project, SERVER)
        async with sessions() as db:
            player = await db.get(Player, (project, SID))
            cache = await db.get(PlayerPlaytime, (project, SID))
            assert player.is_whitelisted and cache.project_ms == 21.67 * HOUR
            assert cache.project_days == 1 and cache.annual_ms is None
            assert (
                cache.first_join_at == now - 119_000 and cache.first_join_server_id == first_server
            )
            case = await db.scalar(select(ResearchCase).where(ResearchCase.project_id == project))
            assert case.hours_source == "rustapp" and case.qualifying_ms == 21.67 * HOUR
            assert case.entry_at == (cache.first_join_at if first_server == SERVER else None)
            assert not await db.scalar(
                text("SELECT trained FROM player_research_status WHERE project_id=:p"),
                {"p": project},
            )
    finally:
        await engine.dispose()
