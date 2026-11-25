from app.services.moderation.facts import Facts, Patterns
from app.services.moderation.patterns import (
    first_hour_kills,
    headshot_kill_ratio,
    long_range_headshot_kills,
    private_profile_after_ban,
    private_profile_early_kills,
    recent_registration_match,
    repeated_head_hits,
    steam_registration_match,
)

# Priority only resolves ties. Every combat rule and P04 may act on its own.
PRIORITY = ("P02", "P06", "P05", "P03", "P07", "P04", "P09", "P01")


def evaluate(facts: Facts, rules: Patterns, *, entry: bool) -> dict:
    if facts.session.is_license is not False:
        return {}
    if entry:
        registration = steam_registration_match.evaluate(facts, rules.P01)
        candidates = {
            "P01": registration,
            "P02": recent_registration_match.evaluate(registration, rules.P02)
            if rules.P01.action != "off"
            else None,
            "P04": private_profile_after_ban.evaluate(facts, rules.P04),
        }
    else:
        candidates = {
            "P03": private_profile_early_kills.evaluate(facts, rules.P03),
            "P05": long_range_headshot_kills.evaluate(facts, rules.P05),
            "P06": repeated_head_hits.evaluate(facts, rules.P06),
            "P07": headshot_kill_ratio.evaluate(facts, rules.P07),
            "P09": first_hour_kills.evaluate(facts, rules.P09),
        }
    results = {}
    for code, evidence in candidates.items():
        rule = getattr(rules, code)
        if evidence and rule.action != "off":
            results[code] = {"at": facts.at, "settings": rule.model_dump(), "evidence": evidence}
    return results


def outcome(matches: dict) -> tuple[str, str]:
    for action in ("ban", "review", "observe"):
        for code in PRIORITY:
            if code not in matches:
                continue
            configured = matches[code]["settings"]["action"]
            if code == "P01" and configured == "ban":
                configured = "observe"
            if (
                code == "P02"
                and configured == "ban"
                and matches["P01"]["settings"]["action"] != "ban"
            ):
                configured = "review"
            if configured == action:
                return code, action
    raise ValueError("Нет срабатываний")


def describe(code: str, match: dict) -> str:
    data = match["evidence"]
    config = match["settings"]
    if code == "P02":
        prior = data["bans"][0]
        gap = abs(data["signed_at"] - prior["signed_at"]) / 1000
        return f"Регистрация Steam в пределах {gap:g} с от аккаунта недавнего бана №{prior['id']}"
    if code == "P04":
        return (
            f"Закрытый профиль без даты Steam. Подключение в течение {config['minutes']} мин "
            f"после бана №{data['prior_bans'][-1]}"
        )
    if code == "P07":
        return (
            f"{data['heads']} из {data['total']} убийств в голову с {config['meters']:g} м "
            f"за {config['minutes']} мин"
        )
    count = len({k["victim"] for k in data["kills"]})
    if code == "P03":
        return (
            f"Закрытый профиль без даты Steam. Разных жертв за первые "
            f"{config['minutes']} мин: {count}"
        )
    if code == "P05":
        return f"Разных жертв в голову с {config['meters']:g} м за {config['minutes']} мин: {count}"
    if code == "P06":
        return (
            f"За {config['minutes']} мин у {count} жертв по {config['hits']} "
            "или больше попаданий только в голову"
        )
    if code == "P09":
        return (
            f"Разных жертв за {config['minutes']} мин: {count}. "
            f"Первые {config['age_minutes']} мин на проекте"
        )
    raise ValueError("У паттерна нет самостоятельного действия блокировки")
