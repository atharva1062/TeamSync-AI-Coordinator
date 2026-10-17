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
    command=update.effective_message.text.split()[0].split("@")[0].lower()
    try:
        if command=="/confirm":
            if len(members)!=1: raise ValueError("Use your project's private dashboard invitation to confirm assignments when you belong to several projects.")
            confirm(path,members[0]); answer="Your task assignments are confirmed. The leader can connect GitHub after everyone confirms."
        else:
            parts=update.effective_message.text.split(maxsplit=3 if command=="/progress" else 2)
            task_id=int(parts[1])
            with connection(path) as conn:
                row=conn.execute("SELECT m.* FROM members m JOIN tasks t ON t.member_id=m.id WHERE t.id=? AND m.telegram_chat_id=?",(task_id,update.effective_chat.id)).fetchone()
            if not row: raise ValueError("This task is not assigned to your linked account.")
            member=dict(row)
            if command=="/progress":
                if len(parts)!=4: raise ValueError("Use /progress TASK_ID PERCENT short explanation")
                report_progress(path,member,task_id,int(parts[2]),parts[3]); answer=f"Task #{task_id}: saved your reported {parts[2]}% progress."
            else:
                if len(parts)!=3: raise ValueError("Use /helpme TASK_ID error or question")
                from ai_help import help_member
                answer=await help_member(path,member,task_id,parts[2])
    except (ValueError,IndexError,RuntimeError) as exc:
        answer=str(exc) if str(exc) and not isinstance(exc,IndexError) else "Use /helpme TASK_ID your error, or /progress TASK_ID PERCENT explanation."
    for chunk in message_chunks(answer): await update.effective_message.reply_text(chunk)
