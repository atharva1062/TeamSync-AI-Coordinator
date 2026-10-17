"""Member enrollment, confirmations, task percentages, and integrated AI help."""

from board import message_chunks
from db import connection
from teamspace import confirm, link_telegram, report_progress


def members_for_chat(path,chat_id):
    with connection(path) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM members WHERE telegram_chat_id=?",(chat_id,))]


async def enroll(update,context):
    if not context.args or not context.args[0].startswith("member_"): return False
    if update.effective_chat.type!="private" or update.effective_chat.id!=update.effective_user.id:
        await update.effective_message.reply_text("Open your enrollment link in a private chat with this bot."); return True
    try:
        member=link_telegram(context.bot_data["database_path"],context.args[0][7:],update.effective_chat.id)
        await update.effective_message.reply_text(f"Telegram linked for {member['name']}. Review your dashboard tasks, enter a completion deadline for each, and confirm them there. /confirm can accept tasks with deadlines already saved. Use /helpme TASK_ID your error for debugging help and /progress TASK_ID PERCENT your update.")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
    return True


async def commands(update,context):
    if update.effective_chat.type!="private" or update.effective_chat.id!=update.effective_user.id: return
    path=context.bot_data["database_path"]
    members=members_for_chat(path,update.effective_chat.id)
    full_text=update.effective_message.text or ""
    command=full_text.split()[0].split("@")[0].lower()
    try:
        if command=="/confirm":
            if len(members)!=1: raise ValueError("Use your project's private dashboard invitation to confirm assignments when you belong to several projects.")
            confirm(path,members[0]); answer="Your task assignments are confirmed. The leader can connect GitHub after everyone confirms."
        elif command=="/progress":
            parts=full_text.split(maxsplit=3)
            if len(parts)!=4: raise ValueError("Use /progress TASK_ID PERCENT short explanation")
            task_id=int(parts[1])
            with connection(path) as conn:
                row=conn.execute("SELECT m.* FROM members m JOIN tasks t ON t.member_id=m.id WHERE t.id=? AND m.telegram_chat_id=?",(task_id,update.effective_chat.id)).fetchone()
            if not row: raise ValueError("This task is not assigned to your linked account.")
            member=dict(row)
            report_progress(path,member,task_id,int(parts[2]),parts[3]); answer=f"Task #{task_id}: saved your reported {parts[2]}% progress."
        elif command=="/helpme":
            from ai_help import answer_member_message, answer_unlinked_message
            query=full_text.removeprefix(command).strip()
            if not query:
                answer="Ask any question, for example: /helpme how do I connect to MongoDB? or /helpme 1 my error message"
            elif not members:
                answer=await answer_unlinked_message(query)
            else:
                member=members[0]
                task_id=None
                words=query.split(maxsplit=1)
                if words and words[0].isdigit():
                    task_id=int(words[0])
                    query=words[1] if len(words)>1 else query
                answer=await answer_member_message(path,member,query,task_id=task_id)
        else:
            answer="Available commands: /status, /risk, /confirm, /progress, /helpme, /update"
    except (ValueError,IndexError,RuntimeError) as exc:
        answer=str(exc) if str(exc) and not isinstance(exc,IndexError) else "Please check your command format or ask your question directly."
    for chunk in message_chunks(answer): await update.effective_message.reply_text(chunk)
