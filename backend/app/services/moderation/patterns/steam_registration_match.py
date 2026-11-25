from app.services.moderation.facts import Ban, Facts, Registration


def matches(facts: Facts, rule: Registration) -> list[Ban]:
    if facts.signed_at is None:
        return []
    return [
        b
        for b in facts.bans
        if b.steam_id != facts.player
        and b.at < facts.first_seen
        and b.signed_at is not None
        and abs(b.signed_at - facts.signed_at) <= rule.tolerance_seconds * 1000
    ]


def evaluate(facts: Facts, rule: Registration) -> dict | None:
    bans = matches(facts, rule)
    if bans:
        return {
            "signed_at": facts.signed_at,
            "first_seen": facts.first_seen,
            "bans": [{"id": b.id, "at": b.at, "signed_at": b.signed_at} for b in bans],
        }
