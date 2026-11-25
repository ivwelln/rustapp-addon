from app.services.moderation.facts import Distance, Facts, distinct, recent_kills


def evaluate(facts: Facts, rule: Distance) -> dict | None:
    kills = [
        kill
        for kill in recent_kills(facts, rule.minutes)
        if kill.firearm and kill.head and kill.distance is not None and kill.distance >= rule.meters
    ]
    return distinct(kills, rule.victims)
