from app.services.moderation.facts import Facts, Ratio, recent_kills


def evaluate(facts: Facts, rule: Ratio) -> dict | None:
    kills = [
        kill
        for kill in recent_kills(facts, rule.minutes)
        if kill.firearm and kill.distance is not None and kill.distance >= rule.meters
    ]
    heads = sum(kill.head for kill in kills)
    if len(kills) >= rule.kills and 100 * heads >= rule.percent * len(kills):
        return {"heads": heads, "total": len(kills), "kills": [kill.model_dump() for kill in kills]}
