"""Plain-text board shared by Telegram and the offline preview."""

from collections import Counter


def format_board(tasks: list[dict]) -> str:
    if not tasks:
        return "No tasks yet. The coordinator can run: python seed.py"
    counts = Counter(task["status"] for task in tasks)
    lines = [
        "TeamSync AI Coordinator",
        f"{len(tasks)} tasks: {counts['Complete']} done, {counts['In Progress']} in progress, "
        f"{counts['Blocked']} blocked, {counts['Pending']} pending",
    ]
    for task in tasks:
        lines.extend(["", f"#{task['id']} {task['title']}", f"Owner: {task['owner_name']} | {task['status']}"])
        if task["blocker"]:
            lines.append(f"Blocker: {task['blocker']}")
    return "\n".join(lines)


def message_chunks(text: str, limit: int = 4000) -> list[str]:
    """Use a conservative UTF-16 budget without splitting Unicode characters."""
    if limit <= 0:
        raise ValueError("Message limit must be positive")
    chunks = []
    start = used = 0
    for index, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if width > limit:
            raise ValueError("Message limit is too small for this character")
        if used + width > limit:
            chunks.append(text[start:index])
            start, used = index, 0
        used += width
    if start < len(text):
        chunks.append(text[start:])
    return chunks


def truncate_text(text: str, limit: int) -> str:
    """Return a prefix within a UTF-16 budget for a single task prompt."""
    if limit < 0:
        raise ValueError("Text limit cannot be negative")
    used = 0
    for index, character in enumerate(text):
        used += 2 if ord(character) > 0xFFFF else 1
        if used > limit:
            return text[:index]
    return text


def format_risk(tasks: list[dict]) -> str:
    """Summarize recorded blockers, without inventing deadlines or risk scores."""
    if not tasks:
        return "Project risk report\nNo tasks yet. The coordinator can run: python seed.py"
    counts = Counter(task["status"] for task in tasks)
    blocked = [task for task in tasks if task["status"] == "Blocked"]
    unassigned = [task for task in tasks if task["status"] != "Complete" and not task["telegram_chat_id"]]
    lines = [
        "Project risk report | TeamSync AI Coordinator",
        f"{counts['Complete']} of {len(tasks)} done ({round(100 * counts['Complete'] / len(tasks))}%). "
        f"{counts['In Progress']} in progress, {counts['Blocked']} blocked, {counts['Pending']} pending.",
        "", "Open blockers:" if blocked else "No blockers reported on the current board.",
    ]
    for task in blocked:
        lines.append(f"#{task['id']} {task['title']} — {task['owner_name']}: {task['blocker']}")
    if unassigned:
        lines.extend(["", f"Follow-up gap: {len(unassigned)} open task(s) have no owner chat ID configured."])
        lines.extend(f"#{task['id']} {task['title']} — {task['owner_name']}" for task in unassigned)
    if blocked:
        lines.extend(["", "Next step: help the listed owners resolve their dependencies."])
    elif counts["Complete"] == len(tasks):
        lines.extend(["", "All tasks are complete."])
    else:
        lines.extend(["", "Use /followup to request fresh updates from assigned owners."])
    return "\n".join(lines)
