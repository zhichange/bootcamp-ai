"""One-off helper: create blog_small and load fixtures/01_small_db.sql."""

import asyncio
import sys
from pathlib import Path

import asyncpg

HOST, PORT, USER, PASSWORD = "localhost", 5432, "postgres", "123456"


async def main() -> None:
    conn = await asyncpg.connect(
        host=HOST, port=PORT, user=USER, password=PASSWORD, database="postgres", timeout=5
    )
    exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = 'blog_small'")
    if not exists:
        await conn.execute("CREATE DATABASE blog_small")
        print("blog_small created")
    else:
        print("blog_small already exists")
    await conn.close()

    sql_path = Path(__file__).resolve().parent.parent / "fixtures" / "01_small_db.sql"
    sql = sql_path.read_text(encoding="utf-8")
    # Strip psql meta-commands (\c) and database-level statements
    # (DROP/CREATE DATABASE) — asyncpg cannot run them and we connect
    # directly to the already-created blog_small database.
    skip = ("\\", "DROP DATABASE", "CREATE DATABASE", "VACUUM")
    lines = [line for line in sql.splitlines() if not line.strip().startswith(skip)]
    sql = "\n".join(lines)

    conn = await asyncpg.connect(
        host=HOST, port=PORT, user=USER, password=PASSWORD, database="blog_small", timeout=5
    )
    await conn.execute(sql)
    n = await conn.fetchval(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
    )
    print(f"blog_small loaded, public tables: {n}")
    await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
