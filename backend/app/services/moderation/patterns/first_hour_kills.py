from app.services.moderation.facts import MINUTE, Facts, FirstHour, distinct, recent_kills


def evaluate(facts: Facts, rule: FirstHour) -> dict | None:
    if facts.first_seen <= facts.at <= facts.first_seen + rule.age_minutes * MINUTE:
        kills = [
            kill
            for kill in recent_kills(facts, rule.minutes)
            if kill.created_at >= facts.first_seen
        ]
        if evidence := distinct(kills, rule.victims):
            return {**evidence, "first_seen": facts.first_seen}
