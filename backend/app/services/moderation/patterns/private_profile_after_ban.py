from app.services.moderation.facts import MINUTE, Facts, RecentBan


def evaluate(facts: Facts, rule: RecentBan) -> dict | None:
    if not (facts.private_without_date and facts.first_join and facts.connection_observed):
        return None
    connected = facts.session.connected_at
    ids = [
        ban.id
        for ban in facts.bans
        if ban.steam_id != facts.player
        and ban.pirated
        and ban.server_id == facts.session.server_id
        and 0 < connected - ban.at <= rule.minutes * MINUTE
    ]
    if ids:
        return {"connected_at": connected, "prior_bans": ids, "private_without_date": True}
