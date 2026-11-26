from app.services.moderation.facts import MINUTE, Facts, Ratio, recent_kills


def evaluate(facts: Facts, rule: Ratio) -> dict | None:
    window_start = max(facts.at - rule.minutes * MINUTE, facts.session.joined_at or 0)
    if facts.kill_feed_covered_from is None or facts.kill_feed_covered_from > window_start:
        return None
    kills = [
        kill
        for kill in recent_kills(facts, rule.minutes)
        if kill.created_at >= window_start
        and kill.firearm
        and kill.distance is not None
        and kill.distance >= rule.meters
    ]
    heads = sum(kill.head for kill in kills)
    if len(kills) >= rule.kills and 100 * heads >= rule.percent * len(kills):
        return {
            "heads": heads,
            "total": len(kills),
            "window_start": window_start,
            "kills": [kill.model_dump() for kill in kills],
        }
