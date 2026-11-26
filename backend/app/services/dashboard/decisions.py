from app.models import Decision
from app.services.moderation.evaluate import PRIORITY, describe

KILL_FIELDS = ("id", "created_at", "victim", "weapon", "head", "distance", "head_hits_only")
EVIDENCE_FIELDS = (
    "signed_at",
    "first_seen",
    "connected_at",
    "joined_at",
    "private_without_date",
    "heads",
    "total",
    "window_start",
    "prior_bans",
)
RULE_FIELDS = (
    "action",
    "version",
    "tolerance_seconds",
    "minutes",
    "victims",
    "meters",
    "hits",
    "kills",
    "percent",
    "age_minutes",
)


def reason(decision: Decision) -> str:
    if decision.primary_pattern == "P01":
        return "Совпадение даты регистрации Steam с ранее заблокированным аккаунтом"
    match = decision.matches.get(decision.primary_pattern)
    if not match:
        return ""
    if decision.reference_ban_id is None:
        if decision.primary_pattern == "P02":
            return "Близкая дата регистрации Steam и недавний бан"
        if decision.primary_pattern == "P04":
            return "Закрытый профиль без даты Steam и подключение после недавнего бана"
    try:
        return describe(decision.primary_pattern, match, reference_ban_id=decision.reference_ban_id)
    except (KeyError, ValueError, TypeError):
        return ""


def decision_summary(decision: Decision) -> dict:
    return {
        "id": decision.id,
        "steam_id": decision.steam_id,
        "state": decision.state,
        "pattern": decision.primary_pattern,
        "reason": reason(decision),
        "ban_id": decision.external_id,
        "at": decision.matches.get(decision.primary_pattern, {}).get("at"),
        "verdict": decision.verdict,
    }


def decision_details(decision: Decision) -> dict:
    rules = []
    for code in PRIORITY:
        if code not in decision.matches:
            continue
        match = decision.matches[code]
        evidence = match["evidence"]
        rules.append(
            {
                "code": code,
                "at": match["at"],
                "settings": {
                    key: match["settings"][key] for key in RULE_FIELDS if key in match["settings"]
                },
                "evidence": {
                    **{key: evidence[key] for key in EVIDENCE_FIELDS if key in evidence},
                    "kills": [
                        {key: kill.get(key) for key in KILL_FIELDS}
                        for kill in evidence.get("kills", [])
                    ],
                    "bans": [
                        {key: ban.get(key) for key in ("id", "at", "signed_at")}
                        for ban in evidence.get("bans", [])
                    ],
                },
            }
        )
    return {
        **decision_summary(decision),
        "server_id": decision.server_id,
        "joined_at": decision.joined_at
        or decision.matches.get("P03", {}).get("evidence", {}).get("joined_at"),
        "reference_ban_id": decision.reference_ban_id,
        "verdict_note": decision.verdict_note,
        "eligibility": decision.eligibility,
        "verdict_at": decision.verdict_at,
        "rules": rules,
    }
