"""Local board setup and owner assignment; never requires a Telegram token."""

import argparse

from board import format_board
from config import database_path, load_environment
from db import assign_chat, initialize, list_tasks, seed_tasks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assign", nargs=2, type=int, metavar=("TASK_ID", "CHAT_ID"))
    args = parser.parse_args()
    load_environment()
    path = database_path()
    initialize(path)
    if args.assign:
        try:
            assigned = assign_chat(path, *args.assign)
        except ValueError as exc:
            parser.error(str(exc))
        if not assigned:
            parser.error("Task not found. Run python seed.py first.")
        print(f"Assigned task #{args.assign[0]}.")
    else:
        count = seed_tasks(path)
        print(f"Added {count} sample tasks." if count else "Existing board preserved.")
    print(format_board(list_tasks(path)))


if __name__ == "__main__":
    main()

