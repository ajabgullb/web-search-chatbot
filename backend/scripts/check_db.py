"""Preflight check: is the configured database reachable?

Turns a forty-line asyncpg traceback into one actionable line, which is what a
developer who simply has not started the stack yet actually needs. Reads the
same typed settings the app does, so it reports the real host and port even
after POSTGRES_HOST_PORT has been remapped.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import get_settings
from app.db.session import Database


async def main() -> int:
    settings = get_settings()
    url = settings.database_url
    endpoint = f"{url.hosts()[0]['host']}:{url.hosts()[0]['port']}"

    database = Database(settings)
    try:
        reachable = await database.check_connection()
    finally:
        await database.dispose()

    if reachable:
        print(f"PostgreSQL reachable at {endpoint} (database {(url.path or '/').lstrip('/')})")
        return 0

    print(
        f"Cannot reach PostgreSQL at {endpoint}.\n"
        "\n"
        "  DATABASE_URL in .env points there. Start the database with:\n"
        "      make up-db\n"
        "\n"
        "  If you run your own PostgreSQL instead, update DATABASE_URL in .env\n"
        "  (and POSTGRES_HOST_PORT, if you use Compose) to match it.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
