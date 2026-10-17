"""Coordinate model calls, persisted clarification, and Telegram acknowledgements."""

import asyncio
import json
import logging

from telegram import ForceReply
from telegram.error import TelegramError

from classifier import validate_result
from db import clarification_context, finish_classification, set_clarification_prompt

LOGGER = logging.getLogger(__name__)


class ClarificationContextError(ValueError):
    pass


def build_evidence(previous: dict | None, text: str) -> list[dict]:
    if previous is None:
        return [{"role": "owner", "text": text}]
    try:
        evidence = json.loads(previous["evidence_json"])
        if (not isinstance(evidence, list) or not evidence
                or not isinstance(previous["question"], str) or not previous["question"].strip()
                or any(not isinstance(item, dict) or set(item) != {"role", "text"}
                       or item["role"] not in ("owner", "question") or not isinstance(item["text"], str)
                       for item in evidence)):
            raise ValueError("Invalid saved clarification")
    except (ValueError, KeyError, TypeError) as exc:
        raise ClarificationContextError("Invalid saved clarification") from exc
    return [*evidence, {"role": "question", "text": previous["question"]}, {"role": "owner", "text": text}]


async def notify_coordinator(context, task_id: int, error_type: str) -> None:
    coordinator = context.bot_data.get("coordinator_chat_id")
    from db import connection
    with connection(context.bot_data["database_path"]) as conn:
        row=conn.execute("SELECT m.telegram_chat_id FROM tasks t JOIN members m ON m.project_id=t.project_id AND m.role='leader' WHERE t.id=?",(task_id,)).fetchone()
        if row: coordinator=row[0]
    if not coordinator:
        LOGGER.error("Cannot notify coordinator: COORDINATOR_CHAT_ID is unset")
        return
    try:
        await context.bot.send_message(
            chat_id=coordinator,
            text=f"Task #{task_id}: AI update failed ({error_type}). Board unchanged. Ask the owner to resend their full update.",
        )
    except TelegramError:
        LOGGER.error("Could not deliver coordinator failure alert for task %s", task_id)


async def process_reply(update, context, task: dict, text: str) -> None:
    message = update.effective_message
    path = context.bot_data["database_path"]
    chat_id = update.effective_chat.id
    classifier = context.bot_data.get("classifier")
    if classifier is None:
        await message.reply_text(
            f"Collected your update for task #{task['id']}. AI updates are disabled until "
            "the coordinator configures the selected AI provider key. The board is unchanged."
        )
        return
    # Preserve the latest raw update even if saved context cannot be decoded.
    evidence = [{"role": "owner", "text": text}]
    try:
        previous = clarification_context(path, task["id"], chat_id)
        evidence = build_evidence(previous, text)
        if sum(len(item["text"]) for item in evidence) > 24000:
            # Reset an overlong exchange with a question, never a guessed status.
            evidence = [{"role": "owner", "text": text}]
            result = validate_result(json.dumps({
                "status": None, "blocker": None, "progress_note": "", "confidence": 0,
                "dependency_open": False,
                "clarifying_question": "Please send one full update with your current progress and any remaining blockers.",
            }))
        else:
            # Bound the whole operation even if a transport does not respect its timeout.
            result = await asyncio.wait_for(classifier.classify(task, evidence), timeout=30)
        # Revalidate at the mutation boundary, including injected/mock classifiers.
        result = validate_result(result.to_json())
    except Exception as exc:
        error_type = type(exc).__name__
        LOGGER.error("Classification failed for task %s (%s)", task["id"], error_type)
        finish_classification(path, task, chat_id, message.message_id, evidence, error_type=error_type)
        await notify_coordinator(context, task["id"], error_type)
        await message.reply_text("I couldn't classify this update. The board is unchanged. Please resend your full update shortly.")
        return
    state = finish_classification(path, task, chat_id, message.message_id, evidence, result=result)
    if state == "failed":
        await notify_coordinator(context, task["id"], "StaleTask")
        await message.reply_text("The task changed while I checked your reply. Please resend your full update. The AI result was not applied.")
    elif state == "needs_clarification":
        prompt = await message.reply_text(
            f"Task #{task['id']}: {result.clarifying_question}\n"
            f"Reply here or use /update {task['id']} your answer. The board is unchanged.",
            reply_markup=ForceReply(),
        )
        set_clarification_prompt(path, task["id"], chat_id, prompt.message_id)
    elif state == "processed":
        blocker = f"\nBlocker: {result.blocker}" if result.blocker else ""
        await message.reply_text(f"Task #{task['id']} updated to {result.status}.{blocker}")
    else:
        await message.reply_text("This update was already handled. Use /status to see the board.")
