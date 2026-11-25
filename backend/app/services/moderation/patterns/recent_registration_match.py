from app.services.moderation.facts import MINUTE, RecentBan


def evaluate(registration: dict | None, rule: RecentBan) -> dict | None:
    if registration:
        bans = [
            ban
            for ban in registration["bans"]
            if registration["first_seen"] - ban["at"] <= rule.minutes * MINUTE
        ]
        if bans:
            return {**registration, "bans": bans}
