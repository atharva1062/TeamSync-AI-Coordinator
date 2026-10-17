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
        await message.reply_text("Please send task updates and questions in your private chat with this bot.")
        return
    path = context.bot_data["database_path"]
    chat_id = update.effective_chat.id
    text = message.text.strip()
    if not text:
        await message.reply_text("Please send a text message or question.")
        return

    # 1. Explicit /update command
    if text.split(maxsplit=1)[0].split("@")[0].lower() == "/update":
        parts = text.split(maxsplit=2)
        task_number = parts[1].removeprefix("#") if len(parts) > 1 else ""
        if len(parts) != 3 or not task_number.isascii() or not task_number.isdigit() or len(task_number) > 18:
            await message.reply_text("Use /update TASK_ID your full update, for example: /update 1 waiting for credentials")
            return
        task_id = int(task_number)
        task = next((t for t in list_tasks(path) if t["id"] == task_id and t["telegram_chat_id"] == chat_id), None)
        if task is None:
            await message.reply_text(f"Task #{task_id} not found under your linked account.")
            return
        if task["status"] == "Complete":
            await message.reply_text(f"Task #{task['id']} is already complete.")
            return
        saved = record_reply(path, task["id"], chat_id, message.message_id, parts[2])
        if not saved:
            await message.reply_text("This update was already recorded. Use /status to check the board.")
            return
        from processing import process_reply
        await process_reply(update, context, task, parts[2])
        return

    # 2. Reply to a specific bot follow-up prompt
    if message.reply_to_message is not None:
        task = task_for_prompt(path, chat_id, message.reply_to_message.message_id)
        if task is not None and task["status"] != "Complete":
            saved = record_reply(path, task["id"], chat_id, message.message_id, text)
            if saved:
                from processing import process_reply
                await process_reply(update, context, task, text)
                return

    # 3. For ANY question, doubt, error, project inquiry, or general conversation:
    from team_bot import members_for_chat
    from ai_help import answer_member_message, answer_unlinked_message

    members = members_for_chat(path, chat_id)
    if members:
        answer = await answer_member_message(path, members[0], text)
    else:
        answer = await answer_unlinked_message(text)

    for chunk in message_chunks(answer):
        await message.reply_text(chunk)
