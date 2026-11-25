from app.services.moderation.facts import Facts, Ratio, recent_kills


def evaluate(facts: Facts, rule: Ratio) -> dict | None:
    kills = [
        k
        for k in recent_kills(facts, rule.minutes)
        if k.firearm and k.distance is not None and k.distance >= rule.meters
    ]
    heads = sum(k.head for k in kills)
    if len(kills) >= rule.kills and 100 * heads >= rule.percent * len(kills):
        return {"heads": heads, "total": len(kills), "kills": [k.model_dump() for k in kills]}
