"""Send follow-ups, match task replies, and dispatch classification."""

import logging
import sqlite3

from telegram import ForceReply
from telegram.error import TelegramError

from board import message_chunks, truncate_text
from db import list_tasks, record_followup, record_reply, task_for_prompt

LOGGER = logging.getLogger(__name__)


def is_private_owner(update) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    return bool(chat and user and chat.type == "private" and chat.id == user.id)


async def followup(update, context) -> None:
    message = update.effective_message
    if message is None:
        return
    coordinator = context.bot_data.get("coordinator_chat_id")
    from team_bot import members_for_chat
    project_leader=is_private_owner(update) and any(member["role"]=="leader" for member in members_for_chat(context.bot_data["database_path"],update.effective_chat.id))
    if coordinator is None and not project_leader:
        await message.reply_text("Follow-ups are disabled until a coordinator or linked project leader is configured.")
        return
    if not is_private_owner(update) or (update.effective_user.id != coordinator and not project_leader):
        await message.reply_text("Only the coordinator can run /followup in their private chat.")
        return

    path = context.bot_data["database_path"]
    sent = skipped = failed = 0
    details = []
    from teamspace import visible_tasks
    for task in visible_tasks(path,update.effective_chat.id,followup=True,coordinator=coordinator):
        if task["status"] == "Complete" or not task["telegram_chat_id"]:
            skipped += 1
            continue
        try:
            prompt = await context.bot.send_message(
                chat_id=task["telegram_chat_id"],
                text=(f"Update for task #{task['id']}: {truncate_text(task['title'], 2500)}\n"
                      "How is it going? Reply to this message with progress and any blockers.\n"
                      "I will check your update before changing the board."),
                reply_markup=ForceReply(),
            )
        except TelegramError as exc:
            failed += 1
            details.append(f"Task #{task['id']}: delivery failed ({type(exc).__name__}).")
            continue
        try:
            record_followup(path, task["id"], task["telegram_chat_id"], prompt.message_id)
        except sqlite3.Error:
            failed += 1
            LOGGER.error("Could not persist follow-up for task %s", task["id"])
            details.append(f"Task #{task['id']}: message sent, but tracking failed. Ask owner to use /update {task['id']} their update.")
            continue
        sent += 1
    summary = f"Follow-ups: {sent} sent and tracked, {skipped} skipped (complete or unassigned), {failed} failed."
    for chunk in message_chunks("\n".join([summary, *details])):
        await message.reply_text(chunk)


async def receive_reply(update, context) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    if not is_private_owner(update):
        await message.reply_text("Please send task updates in your private chat with this bot.")
        return
    path = context.bot_data["database_path"]
    chat_id = update.effective_chat.id
    task = None
    text = message.text
    if not text.strip():
        await message.reply_text("Please send a text update about your task.")
        return
    # Explicit task selection resubmits the full update, so ambiguity never loses evidence.
    if text.split(maxsplit=1)[0].split("@")[0].lower() == "/update":
        parts = text.split(maxsplit=2)
        task_number = parts[1].removeprefix("#") if len(parts) > 1 else ""
        if len(parts) != 3 or not task_number.isascii() or not task_number.isdigit() or len(task_number) > 18:
            await message.reply_text("Use /update TASK_ID your full update, for example: /update 1 waiting for credentials")
            return
        task_id = int(task_number)
        task = next((t for t in list_tasks(path) if t["id"] == task_id and t["telegram_chat_id"] == chat_id), None)
        text = parts[2]
    elif message.reply_to_message is not None:
        task = task_for_prompt(path, chat_id, message.reply_to_message.message_id)
        # Unknown reply targets must never fall back to an arbitrary open task.
    else:
        tasks = [t for t in list_tasks(path) if t["telegram_chat_id"] == chat_id and t["status"] != "Complete"]
        if len(tasks) > 1:
            choices = "\n".join(f"#{t['id']} {t['title'][:150]}" for t in tasks)
            for chunk in message_chunks(f"Which task is this about?\n{choices}\nResend as /update TASK_ID your full update."):
                await message.reply_text(chunk)
            return
        task = tasks[0] if tasks else None
    if task is None:
        await message.reply_text("I couldn't match this to your task. Reply to a task follow-up or use /update TASK_ID your update. Ask the coordinator to check your assignment if needed.")
        return
    if task["status"] == "Complete":
        await message.reply_text(f"Task #{task['id']} is already complete. Ask the coordinator if it needs reopening.")
        return
    saved = record_reply(path, task["id"], chat_id, message.message_id, text)
    if not saved:
        await message.reply_text("This message was already collected, or the task assignment changed. Use /status to check the current board before resending.")
        return
    from processing import process_reply

    await process_reply(update, context, task, text)
