from app.services.moderation.facts import MINUTE, Facts, RecentBan


def evaluate(facts: Facts, rule: RecentBan) -> dict | None:
    if not (facts.private_without_date and facts.first_join):
        return None
    connected = facts.session.connected_at
    ids = [
        b.id
        for b in facts.bans
        if b.steam_id != facts.player
        and b.pirated
        and b.server_id == facts.session.server_id
        and 0 < connected - b.at <= rule.minutes * MINUTE
    ]
    if ids:
        return {"connected_at": connected, "prior_bans": ids, "private_without_date": True}
