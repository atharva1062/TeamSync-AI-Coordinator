"""Save a read-only board/history snapshot without exporting Telegram chat IDs."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from board import format_board
from config import ROOT, database_path, load_environment
from dashboard import board_snapshot


def capture(path: Path, output: Path) -> dict:
    snapshot = board_snapshot(path)
    output.mkdir(parents=True, exist_ok=False)
    payload = {
        "execution": "simulated" if snapshot["simulated"] else "database_snapshot",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "note": "Board/history snapshot only; live Telegram delivery and Claude accuracy are not established by this file.",
        **snapshot,
    }
    (output / "board.txt").write_text(format_board(snapshot["tasks"]), encoding="utf-8")
    (output / "snapshot.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "evidence" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()
    load_environment()
    try:
        payload = capture(database_path(), args.output)
    except FileExistsError:
        parser.error("Output folder already exists. Choose a new folder.")
    except sqlite3.Error:
        parser.error("Database unavailable. Check DATABASE_PATH and initialize the board first.")
    print(f"Saved {payload['execution']} ({len(payload['tasks'])} tasks, up to 15 history rows): {args.output.resolve()}")


if __name__ == "__main__":
    main()
