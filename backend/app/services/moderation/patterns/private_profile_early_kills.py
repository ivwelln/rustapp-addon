from app.services.moderation.facts import MINUTE, Facts, Series, distinct, recent_kills


def evaluate(facts: Facts, rule: Series) -> dict | None:
    start = facts.session.joined_at
    if facts.private_without_date and facts.first_join and start is not None:
        if start <= facts.at < start + rule.minutes * MINUTE:
            kills = [k for k in recent_kills(facts, rule.minutes) if k.created_at >= start]
            if evidence := distinct(kills, rule.victims):
                return {**evidence, "joined_at": start, "private_without_date": True}
