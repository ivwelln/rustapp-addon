from app.services.moderation.facts import Ban, Facts, Registration


def matches(facts: Facts, rule: Registration) -> list[Ban]:
    if facts.signed_at is None:
        return []
    return [
        ban
        for ban in facts.bans
        if ban.steam_id != facts.player
        and ban.at < facts.first_seen
        and ban.signed_at is not None
        and abs(ban.signed_at - facts.signed_at) <= rule.tolerance_seconds * 1000
    ]


def evaluate(facts: Facts, rule: Registration) -> dict | None:
    bans = matches(facts, rule)
    if bans:
        return {
            "signed_at": facts.signed_at,
            "first_seen": facts.first_seen,
            "bans": [{"id": ban.id, "at": ban.at, "signed_at": ban.signed_at} for ban in bans],
        }
