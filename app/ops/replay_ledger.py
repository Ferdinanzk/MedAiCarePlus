"""Re-apply host deletions before allowing a restored application to start."""

import argparse
import asyncio
import json
from pathlib import Path

import asyncpg

from app import config
from app.services import deletion_ledger


async def replay_file(conn, path: Path) -> int:
    # Read and parse everything first. A missing/truncated ledger blocks restart.
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    count = await deletion_ledger.replay(conn, lines)
    await conn.execute("DELETE FROM ops_state WHERE key='restore_in_progress'")
    return count


async def run(path: Path) -> int:
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        return await replay_file(conn, path)
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    count = asyncio.run(run(args.file))
    print(f"Replayed {count} deletion ledger entries; restore marker cleared.")


if __name__ == "__main__":
    main()
