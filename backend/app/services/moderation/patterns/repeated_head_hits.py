from app.services.moderation.facts import Facts, HeadHits, distinct, recent_kills


def evaluate(facts: Facts, rule: HeadHits) -> dict | None:
    kills = [
        k
        for k in recent_kills(facts, rule.minutes)
        if k.firearm and k.head and k.head_hits_only >= rule.hits
    ]
    return distinct(kills, rule.victims)
