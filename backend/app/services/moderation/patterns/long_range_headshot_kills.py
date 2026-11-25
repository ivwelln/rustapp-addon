from app.services.moderation.facts import Distance, Facts, distinct, recent_kills


def evaluate(facts: Facts, rule: Distance) -> dict | None:
    kills = [
        k
        for k in recent_kills(facts, rule.minutes)
        if k.firearm and k.head and k.distance is not None and k.distance >= rule.meters
    ]
    return distinct(kills, rule.victims)
