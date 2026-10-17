"""Durable daily IST check-ins and periodic read-only repository sync."""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path

from telegram import ForceReply
from telegram.error import TelegramError

from board import truncate_text
from db import connection, record_followup
from teamspace import backup_database, event, project_snapshot

IST=timezone(timedelta(hours=5,minutes=30))
LOGGER=logging.getLogger(__name__)


def unanswered(conn,followup):
    # A reply, /progress, or dashboard task update counts as a response.
    return not conn.execute("SELECT 1 FROM replies WHERE task_id=? AND telegram_chat_id=? AND julianday(created_at)>=julianday(?) UNION ALL SELECT 1 FROM status_history WHERE task_id=? AND julianday(created_at)>=julianday(?) LIMIT 1",(followup["task_id"],followup["telegram_chat_id"],followup["created_at"],followup["task_id"],followup["created_at"])).fetchone()


async def check_missed_responses(path,now=None,transport=None):
    """Record evidence after two hours; never guess that a member forgot or finished."""
    from github_sync import repository_commits, sync_member_repository
    import httpx
    now=now or datetime.now(timezone.utc)
    with connection(path) as conn:
        pending=[dict(row) for row in conn.execute("SELECT f.*,t.project_id,t.member_id,m.name,m.github_repo FROM followups f JOIN tasks t ON t.id=f.task_id JOIN members m ON m.id=t.member_id WHERE t.status!='Complete' AND f.id=(SELECT MAX(f2.id) FROM followups f2 WHERE f2.task_id=f.task_id) AND julianday(f.created_at)<=julianday(?) AND NOT EXISTS(SELECT 1 FROM missed_checkins c WHERE c.followup_id=f.id AND (c.outcome!='unavailable' OR julianday(c.checked_at)>julianday(?)))",((now-timedelta(hours=2)).isoformat(),(now-timedelta(minutes=10)).isoformat())) if unanswered(conn,dict(row))]
    for followup in pending:
        data=project_snapshot(path,followup["project_id"])
        member=next(item for item in data["members"] if item["id"]==followup["member_id"])
        repo=member["github_repo"] or data["project"]["repo"]
        day=datetime.fromisoformat(followup["created_at"].replace("Z","+00:00")).astimezone(IST).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()
        outcome="unavailable"
        detail=f"{member['name']} · task #{followup['task_id']}: no reply after 2 hours. "
        if not repo:
            detail+="Connect a repository URL so the bot can check GitHub."
        else:
            try:
                items=await sync_member_repository(path,member,day,transport) if member["github_repo"] else await repository_commits(repo,day,transport)
                outcome="activity" if items else "no_activity"
                detail+=f"GitHub check: {len(items)} commit(s) visible on branches in {repo}, dated since the start of this check-in day. " if items else f"No visible commits dated in this check-in day in {repo}. "
                detail+="Repository evidence only; contributor identity, upload time, task completion, and reason for not replying are unconfirmed."
            except (RuntimeError,httpx.HTTPError,ValueError,KeyError,TypeError):
                detail+="GitHub check unavailable (access, quota, or incomplete response). The bot will retry; no inactivity conclusion was made."
        with connection(path) as conn:
            # A response arriving while GitHub was being read cancels the missed-reply result.
            if not unanswered(conn,followup): continue
            conn.execute("INSERT INTO missed_checkins(followup_id,outcome,detail,checked_at) VALUES (?,?,?,?) ON CONFLICT(followup_id) DO UPDATE SET outcome=excluded.outcome,detail=excluded.detail,checked_at=excluded.checked_at",(followup["id"],outcome,detail,now.isoformat()))
            event(conn,followup["project_id"],member["id"],"missed_checkin",detail)


async def dispatch_due(path,bot,now=None):
    local=(now or datetime.now(timezone.utc)).astimezone(IST)
    with connection(path) as conn:
        projects=[dict(row) for row in conn.execute("SELECT * FROM projects WHERE checkins_enabled=1")]
    for project in projects:
        if local.strftime("%H:%M")<project["checkin_time"]: continue
        data=project_snapshot(path,project["id"])
        if not data["ready"] or not project["repo"] or any(not member["telegram_chat_id"] for member in data["members"]): continue
        with connection(path) as conn:
            claimed=conn.execute("INSERT INTO checkin_runs(project_id,local_date) VALUES (?,?) ON CONFLICT DO NOTHING",(project["id"],local.date().isoformat())).rowcount
        if not claimed: continue
        sent=failed=0
        for task in data["tasks"]:
            if task["status"]=="Complete": continue
            try:
                message=await bot.send_message(chat_id=task["telegram_chat_id"],text=f"Daily check-in · {truncate_text(project['name'],200)}\nTask #{task['id']}: {truncate_text(task['title'],1800)}\nHow did today's work go? Reply here with progress and blockers. Use /progress {task['id']} PERCENT your note, or /helpme {task['id']} your error for help.",reply_markup=ForceReply())
                record_followup(path,task["id"],task["telegram_chat_id"],message.message_id)
                sent+=1
            except Exception:
                failed+=1
                LOGGER.warning("Daily task check-in failed for task %s",task["id"])
        with connection(path) as conn:
            detail=f"Daily check-in: {sent} sent and tracked, {failed} failed."
            conn.execute("UPDATE checkin_runs SET state=?,detail=? WHERE project_id=? AND local_date=?",("partial_failure" if failed else "sent",detail,project["id"],local.date().isoformat()))
            event(conn,project["id"],None,"checkin",detail)


async def run(application):
    path=application.bot_data["database_path"]
    tick=0
    while True:
        try:
            await dispatch_due(path,application.bot)
            await check_missed_responses(path)
            if tick%20==0:
                from github_sync import sync_repository
                with connection(path) as conn:
                    projects=[row[0] for row in conn.execute("SELECT id FROM projects WHERE repo IS NOT NULL")]
                for project_id in projects:
                    try:
                        await sync_repository(path,project_id)
                    except RuntimeError:
                        LOGGER.warning("Repository sync failed for project %s",project_id)
                from github_sync import sync_member_repository
                with connection(path) as conn:
                    members=[dict(row) for row in conn.execute("SELECT * FROM members WHERE github_repo IS NOT NULL")]
                since=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
                for member in members:
                    try:
                        await sync_member_repository(path,member,since)
                    except RuntimeError:
                        LOGGER.warning("Member repository check failed for member %s",member["id"])
            day=datetime.now(IST).date().isoformat()
            backup=Path(path).resolve().parent/"backups"/f"projects-{day}.sqlite3"
            if not backup.exists(): backup_database(path,backup)
        except Exception:
            LOGGER.error("Project worker operation failed; it will retry on its next cycle")
        tick+=1
        await asyncio.sleep(30)


async def start(application):
    application.bot_data["project_worker"]=asyncio.create_task(run(application))


async def stop(application):
    task=application.bot_data.pop("project_worker",None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError): await task
