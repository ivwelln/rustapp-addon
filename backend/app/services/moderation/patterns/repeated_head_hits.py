from app.services.moderation.facts import Facts, HeadHits, distinct, recent_kills


def evaluate(facts: Facts, rule: HeadHits) -> dict | None:
    kills = [
        kill
        for kill in recent_kills(facts, rule.minutes)
        if kill.firearm and kill.head and kill.head_hits_only >= rule.hits
    ]
    return distinct(kills, rule.victims)
