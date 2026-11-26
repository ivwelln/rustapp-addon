from dataclasses import replace

import pytest

from app.clients.rustapp.moderation import Profile, parse_kill
from app.models import CollectionJob
from app.services.moderation.evaluate import evaluate, outcome
from app.services.moderation.facts import MINUTE, Ban, Facts, Kill, Patterns, Session
from app.services.moderation.kill_feed import PAGE_LIMIT, update_coverage

SID = "76561198000000000"
START = 1000 * MINUTE


def example() -> Facts:
    session = Session(
        project_id=1,
        server_id=163,
        steam_id=SID,
        is_license=False,
        connected_at=START,
        joined_at=START + 100,
    )
    return Facts(
        SID, session, START, True, 12345, True, START + MINUTE, (), (), kill_feed_covered_from=START
    )


def hits(count=5):
    return tuple(
        Kill(
            id=event_number,
            created_at=START + 1000 + event_number,
            victim=str(event_number),
            weapon="rifle.ak",
            head=True,
            distance=50,
            head_hits_only=2,
        )
        for event_number in range(count)
    )


def test_registration_requires_same_prior_ban_for_both_rules():
    ctx = replace(
        example(),
        bans=(
            Ban(1, "other", START - 2 * 60 * MINUTE, 12345, 163, True),
            Ban(2, "recent", START - 1, 999999, 163, True),
        ),
    )
    rules = Patterns()
    result = evaluate(ctx, rules, entry=True)
    assert set(result) == {"P01", "P04"}
    assert outcome({"P01": result["P01"]}) == ("P01", "observe")
    ctx = replace(ctx, bans=(Ban(3, "recent", START - 60 * MINUTE, 72345, 163, True),))
    result = evaluate(ctx, rules, entry=True)
    assert outcome(result) == ("P02", "ban")
    assert result["P02"]["evidence"]["bans"][0]["id"] == 3
    result = evaluate(ctx, Patterns.model_validate({"P01": {"action": "off"}}), entry=True)
    assert "P02" not in result


@pytest.mark.parametrize("code", ["P03", "P04", "P05", "P06", "P07", "P09"])
def test_each_selected_rule_can_be_the_only_reason(code):
    rules = Patterns.model_validate(
        {pattern: {"action": "off"} for pattern in Patterns.model_fields if pattern != code}
    )
    ctx = replace(
        example(), kills=hits(), bans=(Ban(1, "previous", START - 5 * MINUTE, None, 163, True),)
    )
    result = evaluate(ctx, rules, entry=code == "P04")
    assert outcome(result) == (code, "ban")
    assert set(result) == {code}


def test_repeated_victim_ratio_and_end_of_first_ten_minutes():
    ctx = replace(
        example(), kills=tuple(kill.model_copy(update={"victim": "same"}) for kill in hits())
    )
    assert set(evaluate(ctx, Patterns(), entry=False)) == {"P07"}
    ctx = replace(ctx, kills=hits(), at=ctx.session.joined_at + 10 * MINUTE)
    assert "P03" not in evaluate(ctx, Patterns(), entry=False)
    assert not evaluate(
        replace(ctx, session=ctx.session.model_copy(update={"is_license": None})),
        Patterns(),
        entry=False,
    )


def test_p07_rejects_truncated_window_and_extends_overlapping_pages():
    job = CollectionJob()
    latest_minute = START + MINUTE
    rows = [{"created_at": latest_minute - index} for index in range(PAGE_LIMIT)]
    update_coverage(job, {}, rows, start=START, end=latest_minute)
    assert job.kill_feed_covered_from == latest_minute - PAGE_LIMIT + 2
    victim = "same"
    shot_series = tuple(kill.model_copy(update={"victim": victim}) for kill in hits())
    facts = replace(example(), kills=shot_series, kill_feed_covered_from=job.kill_feed_covered_from)
    assert "P07" not in evaluate(facts, Patterns(), entry=False)

    update_coverage(job, {}, [], start=START, end=latest_minute + 1)
    assert "P07" in evaluate(
        replace(facts, kill_feed_covered_from=job.kill_feed_covered_from), Patterns(), entry=False
    )
    rows = [{"created_at": row["created_at"] + 2 * MINUTE} for row in rows]
    update_coverage(job, {}, rows, start=START, end=latest_minute + 2 * MINUTE)
    assert job.kill_feed_covered_from > latest_minute


def test_unknown_visibility_and_partial_damage_history():
    profile = Profile(steam_id=SID, project_id=1, created_at=1, steam={"profile_private": True})
    assert not profile.private_without_date
    victim = "76561198000000001"
    hit = {
        "attacker_steam_id": SID,
        "target_steam_id": victim,
        "hp_old": 50,
        "hp_new": 0,
        "bone": "head",
    }
    row = {
        "id": 1,
        "created_at": START,
        "initiator": {"steam_id": SID},
        "target": {"steam_id": victim},
        "hit_history": [hit, hit, {**hit, "attacker_steam_id": victim, "bone": "chest"}],
    }
    assert parse_kill(row, SID).head_hits_only == 2
    row["hit_history"].append({**hit, "hp_new": None})
    assert parse_kill(row, SID).head_hits_only == 0


def test_partial_configuration_preserves_thresholds():
    config = Patterns.model_validate({"P03": {"action": "review"}, "P04": {"version": 2}})
    assert config.P03.minutes == 10 and config.P04.minutes == 15
