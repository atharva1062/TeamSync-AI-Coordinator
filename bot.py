"""Telegram polling service. Run with python bot.py."""

import argparse
import asyncio
import logging

from board import format_board, format_risk, message_chunks
from config import bot_token, classification_settings, coordinator_chat_id, database_path, load_environment
from db import initialize, list_tasks


async def start(update, context) -> None:
    if update.effective_message is None or update.effective_chat is None:
        return
    from team_bot import enroll
    if getattr(context,"args",None) and await enroll(update,context):
        return
    await update.effective_message.reply_text(
        "Welcome to TeamSync AI Coordinator.\n"
        "Use /status to see the project board or /risk for blockers and follow-up gaps.\n"
        f"Your chat ID: {update.effective_chat.id}\n"
        "Send this ID to your coordinator for task assignment.\n"
        "Reply to follow-ups with progress and blockers, or use /update TASK_ID your update.\n"
        "The coordinator can use /followup. AI updates require a configured provider key."
    )


async def status(update, context) -> None:
    if update.effective_message is None:
        return
    from teamspace import visible_tasks
    chat_id=update.effective_chat.id if getattr(update,"effective_chat",None) else None
    board = format_board(visible_tasks(context.bot_data["database_path"],chat_id))
    for chunk in message_chunks(board):
        await update.effective_message.reply_text(chunk)


async def risk(update, context) -> None:
    if update.effective_message is None:
        return
    from teamspace import visible_tasks
    chat_id=update.effective_chat.id if getattr(update,"effective_chat",None) else None
    report = format_risk(visible_tasks(context.bot_data["database_path"],chat_id))
    for chunk in message_chunks(report):
        await update.effective_message.reply_text(chunk)


async def handle_error(update, context) -> None:
    # Avoid logging update payloads or exception URLs, which may contain secrets.
    logging.getLogger(__name__).error("Telegram operation failed (%s)", type(context.error).__name__)
    if update is not None and update.effective_message is not None:
        try:
            await update.effective_message.reply_text("Something went wrong. Please try again shortly.")
        except Exception:
            logging.getLogger(__name__).error("Could not deliver error notice")


async def close_classifier(application):
    classifier = application.bot_data.get("classifier")
    if classifier:
        await classifier.close()


def build_application(token, path, coordinator=None, classifier=None):
    from telegram.ext import Application, CommandHandler, MessageHandler, filters
    from messaging import followup, receive_reply
    from team_bot import commands
    from project_worker import start as start_worker, stop as stop_worker

    application = Application.builder().token(token).post_init(start_worker).post_stop(stop_worker).post_shutdown(close_classifier).build()
    application.bot_data["database_path"] = path
    application.bot_data["coordinator_chat_id"] = coordinator
    application.bot_data["classifier"] = classifier
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("risk", risk))
    application.add_handler(CommandHandler("followup", followup))
    application.add_handler(CommandHandler("update", receive_reply))
    application.add_handler(CommandHandler(["confirm","progress","helpme"],commands))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_reply))
    application.add_error_handler(handle_error)
    return application


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="Print the board without connecting to Telegram")
    args = parser.parse_args()
    load_environment()
    path = database_path()
    if args.preview:
        initialize(path)
        print(format_board(list_tasks(path)))
        return
    try:
        token = bot_token()
        coordinator = coordinator_chat_id()
    except ValueError as exc:
        parser.error(str(exc))
    initialize(path)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Python 3.14 no longer creates an event loop implicitly.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from classifier import create_classifier

        classifier = create_classifier()
        application = build_application(token, path, coordinator, classifier)
        print("Coordinator running. Stop with Ctrl+C.")
        application.run_polling(allowed_updates=["message"])
    finally:
        if not loop.is_closed():
            loop.close()
        asyncio.set_event_loop(None)


if __name__ == "__main__":
    main()
