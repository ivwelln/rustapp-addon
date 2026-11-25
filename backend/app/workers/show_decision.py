import argparse
import asyncio
import json

from app.core.config import DatabaseSettings
from app.db.session import open_database_sessions
from app.models import Decision


async def show(decision_id: int) -> None:
    async with open_database_sessions(DatabaseSettings()) as sessions, sessions() as db:
        row = await db.get(Decision, decision_id)
        if row is None:
            raise SystemExit("Решение не найдено")
        print(
            json.dumps(
                {
                    "decision_id": row.id,
                    "project_id": row.project_id,
                    "server_id": row.server_id,
                    "steam_id": row.steam_id,
                    "action": row.action,
                    "state": row.state,
                    "primary_pattern": row.primary_pattern,
                    "rustapp_ban_id": row.external_id,
                    "patterns": row.matches,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Показать основания решения о модерации")
    parser.add_argument("decision_id", type=int)
    asyncio.run(show(parser.parse_args().decision_id))
