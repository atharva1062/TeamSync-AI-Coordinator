"""Project workspace persistence, enrollment, confirmations, and progress."""

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re
import secrets
import sqlite3

from db import connection


def ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, team_name TEXT NOT NULL,
            leader_confirmed INTEGER NOT NULL DEFAULT 0, repo TEXT, repo_synced_at TEXT,
            checkin_time TEXT NOT NULL DEFAULT '21:00', checkins_enabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
            name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('leader','member')),
            invite_token TEXT NOT NULL UNIQUE, telegram_chat_id INTEGER, github_username TEXT,
            confirmed INTEGER NOT NULL DEFAULT 0,
            UNIQUE(project_id, telegram_chat_id)
        );
        CREATE TABLE IF NOT EXISTS project_events (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
            member_id INTEGER REFERENCES members(id), kind TEXT NOT NULL, detail TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE IF NOT EXISTS repo_activity (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
            sha TEXT NOT NULL, author TEXT, message TEXT NOT NULL, url TEXT NOT NULL,
            committed_at TEXT, UNIQUE(project_id,sha)
        );
        CREATE TABLE IF NOT EXISTS help_messages (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
            member_id INTEGER NOT NULL REFERENCES members(id), task_id INTEGER REFERENCES tasks(id),
            role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE IF NOT EXISTS checkin_runs (
            project_id INTEGER NOT NULL REFERENCES projects(id), local_date TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'claimed', detail TEXT,
            PRIMARY KEY(project_id,local_date)
        );
        CREATE TABLE IF NOT EXISTS member_repo_activity (
            member_id INTEGER NOT NULL REFERENCES members(id), repo TEXT NOT NULL,
            sha TEXT NOT NULL, author TEXT, message TEXT NOT NULL, url TEXT NOT NULL,
            committed_at TEXT, PRIMARY KEY(member_id,repo,sha)
        );
        CREATE TABLE IF NOT EXISTS missed_checkins (
            followup_id INTEGER PRIMARY KEY REFERENCES followups(id),
            outcome TEXT NOT NULL, detail TEXT NOT NULL,
            checked_at TEXT NOT NULL
        );
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(members)")}
    for name in ("github_repo", "repo_verified_at", "repo_checked_at", "repo_error"):
        if name not in columns:
            conn.execute(f"ALTER TABLE members ADD COLUMN {name} TEXT")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    for name,declaration in (("team_code","TEXT"),("team_size","INTEGER")):
        if name not in columns: conn.execute(f"ALTER TABLE projects ADD COLUMN {name} {declaration}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_team_code ON projects(team_code) WHERE team_code IS NOT NULL")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    for name, declaration in (
        ("project_id", "INTEGER REFERENCES projects(id)"),
        ("member_id", "INTEGER REFERENCES members(id)"),
        ("progress_percent", "INTEGER NOT NULL DEFAULT 0 CHECK(progress_percent BETWEEN 0 AND 100)"),
        ("weight", "INTEGER NOT NULL DEFAULT 1 CHECK(weight BETWEEN 1 AND 10)"),
        ("deadline", "TEXT"),
    ):
        if name not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
    conn.execute("UPDATE tasks SET progress_percent=100 WHERE status='Complete' AND progress_percent!=100")


def event(conn, project_id, member_id, kind, detail):
    conn.execute("INSERT INTO project_events(project_id,member_id,kind,detail) VALUES (?,?,?,?)", (project_id, member_id, kind, detail))


def create_project(path, name, team_name, names, team_size=None):
    name, team_name = name.strip(), team_name.strip()
    names = [value.strip() for value in names if value.strip()]
    if not name or not team_name or max(len(name),len(team_name)) > 120:
        raise ValueError("Project and team names are required (maximum 120 characters).")
    if not (1 if team_size is not None else 2) <= len(names) <= 6 or len({value.casefold() for value in names}) != len(names) or any(len(value)>80 for value in names):
        raise ValueError("Enter 2–6 distinct people, including the leader; names can have up to 80 characters.")
    if team_size is not None and (not 2<=team_size<=6 or len(names)!=1):
        raise ValueError("Choose a team of 2–6 people including yourself. Members join using your team code.")
    with connection(path) as conn:
        code=''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(12)) if team_size else None
        project_id = conn.execute("INSERT INTO projects(name,team_name,team_code,team_size) VALUES (?,?,?,?)", (name,team_name,code,team_size)).lastrowid
        leader = None
        for index, member_name in enumerate(names):
            member_id = conn.execute("INSERT INTO members(project_id,name,role,invite_token) VALUES (?,?,?,?)", (project_id,member_name,"leader" if index==0 else "member",secrets.token_urlsafe(24))).lastrowid
            leader = member_id if index==0 else leader
        event(conn,project_id,leader,"created",f"Project created with {len(names)} people.")
        return leader


def join_team(path,code,name):
    code=re.sub(r"[\s-]","",code).upper()
    name=name.strip()
    if not re.fullmatch(r"[A-Z2-9]{12}",code) or not name or len(name)>80:
        raise ValueError("Enter a valid team code and your name (up to 80 characters).")
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        project=conn.execute("SELECT * FROM projects WHERE team_code=?",(code,)).fetchone()
        if not project: raise ValueError("Team code not found. Check the code with your leader.")
        members=conn.execute("SELECT name FROM members WHERE project_id=?",(project["id"],)).fetchall()
        if len(members)>=project["team_size"]: raise ValueError("This team is full. Ask your leader to check your invitation.")
        if any(row[0].casefold()==name.casefold() for row in members):
            raise ValueError("That name is already in this team. Use your existing private invitation or a distinct name.")
        member_id=conn.execute("INSERT INTO members(project_id,name,role,invite_token) VALUES (?,?,'member',?)",(project["id"],name,secrets.token_urlsafe(24))).lastrowid
        event(conn,project["id"],member_id,"member_joined",f"{name} joined the team ({len(members)+1}/{project['team_size']}).")
        return member_id


def member_by_token(path, token):
    with connection(path) as conn:
        row = conn.execute("SELECT * FROM members WHERE invite_token=?",(token,)).fetchone()
        return dict(row) if row else None


def member_by_id(path, member_id):
    with connection(path) as conn:
        row = conn.execute("SELECT * FROM members WHERE id=?",(member_id,)).fetchone()
        return dict(row) if row else None


def project_snapshot(path, project_id):
    with connection(path) as conn:
        conn.execute("BEGIN")
        project = dict(conn.execute("SELECT * FROM projects WHERE id=?",(project_id,)).fetchone())
        members = [dict(row) for row in conn.execute("SELECT * FROM members WHERE project_id=? ORDER BY id",(project_id,))]
        roster_ready=not project["team_size"] or len(members)==project["team_size"]
        tasks = [dict(row) for row in conn.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY id",(project_id,))]
        history = [dict(row) for row in conn.execute("SELECT h.*,t.title FROM status_history h JOIN tasks t ON t.id=h.task_id WHERE t.project_id=? ORDER BY h.id DESC",(project_id,))]
        events = [dict(row) for row in conn.execute("SELECT * FROM project_events WHERE project_id=? ORDER BY id DESC",(project_id,))]
        activity = [dict(row) for row in conn.execute("SELECT * FROM repo_activity WHERE project_id=? ORDER BY committed_at DESC,id DESC LIMIT 100",(project_id,))]
        personal_activity = [dict(row) for row in conn.execute("SELECT a.* FROM member_repo_activity a JOIN members m ON m.id=a.member_id WHERE m.project_id=? ORDER BY a.committed_at DESC",(project_id,))]
        checks = [dict(row) for row in conn.execute("SELECT c.*,f.task_id,t.member_id FROM missed_checkins c JOIN followups f ON f.id=c.followup_id JOIN tasks t ON t.id=f.task_id WHERE t.project_id=? ORDER BY f.id DESC",(project_id,))]
        for check in checks:
            check["responded"]=bool(conn.execute("SELECT 1 FROM replies WHERE task_id=? AND julianday(created_at)>julianday(?) UNION ALL SELECT 1 FROM status_history WHERE task_id=? AND julianday(created_at)>julianday(?) LIMIT 1",(check["task_id"],check["checked_at"],check["task_id"],check["checked_at"])).fetchone())
    for member in members:
        owned = [task for task in tasks if task["member_id"]==member["id"]]
        weight = sum(task["weight"] for task in owned)
        member["progress"] = round(sum(task["weight"]*task["progress_percent"] for task in owned)/weight) if weight else 0
        member["task_count"] = len(owned)
        member["commits"] = sum(item["member_id"]==member["id"] and item["repo"]==member["github_repo"] for item in personal_activity)
        member["repo_check"] = next((item for item in checks if item["member_id"]==member["id"]),None)
    for task in tasks:
        task["deadline_local"]=(datetime.fromisoformat(task["deadline"]).astimezone(timezone(timedelta(hours=5,minutes=30))).strftime('%Y-%m-%dT%H:%M') if task["deadline"] else "")
    seen={item["url"] for item in activity}
    activity.extend(item for item in personal_activity if item["repo"]==next((m["github_repo"] for m in members if m["id"]==item["member_id"]),None) and item["url"] not in seen)
    activity=sorted({item["url"]:item for item in activity}.values(),key=lambda item:item["committed_at"] or "",reverse=True)[:100]
    weight = sum(task["weight"] for task in tasks)
    return dict(project=project,members=members,tasks=tasks,history=history,events=events,activity=activity,roster_ready=roster_ready,
                ready=roster_ready and bool(tasks) and (not project["team_size"] or all(task["deadline"] for task in tasks)) and all(member["confirmed"] and (member["task_count"] or member["role"]=='leader') for member in members) and bool(project["leader_confirmed"]),
                progress=round(sum(task["weight"]*task["progress_percent"] for task in tasks)/weight) if weight else 0)


def add_task(path, actor, title, owner_id, weight=1):
    if actor["role"]!="leader":
        raise ValueError("Only the leader can assign tasks.")
    if not title.strip() or len(title)>500 or not 1<=weight<=10:
        raise ValueError("A task needs a title (up to 500 characters) and weight from 1 to 10.")
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        project=conn.execute("SELECT * FROM projects WHERE id=?",(actor["project_id"],)).fetchone()
        count=conn.execute("SELECT count(*) FROM members WHERE project_id=?",(actor["project_id"],)).fetchone()[0]
        if project["team_size"] and count<project["team_size"]:
            raise ValueError("Wait until every team member has joined before assigning tasks.")
        owner = conn.execute("SELECT * FROM members WHERE id=? AND project_id=?",(owner_id,actor["project_id"])).fetchone()
        if not owner:
            raise ValueError("Choose an owner from this team.")
        task_id = conn.execute("INSERT INTO tasks(title,owner_name,telegram_chat_id,project_id,member_id,weight) VALUES (?,?,?,?,?,?)",(title.strip(),owner["name"],owner["telegram_chat_id"],actor["project_id"],owner_id,weight)).lastrowid
        conn.execute("UPDATE members SET confirmed=0 WHERE id=?",(owner_id,))
        conn.execute("UPDATE projects SET leader_confirmed=0,checkins_enabled=0 WHERE id=?",(actor["project_id"],))
        event(conn,actor["project_id"],actor["id"],"task_assigned",f"Task #{task_id} assigned to {owner['name']}: {title.strip()}")
        return task_id


def confirm(path, actor, deadlines=None):
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        project=conn.execute("SELECT * FROM projects WHERE id=?",(actor["project_id"],)).fetchone()
        owned=conn.execute("SELECT * FROM tasks WHERE member_id=?",(actor["id"],)).fetchall()
        if not owned and actor["role"]!='leader':
            raise ValueError("Your leader must assign you at least one task first.")
        if project["team_size"]:
            count=conn.execute("SELECT count(*) FROM members WHERE project_id=?",(actor["project_id"],)).fetchone()[0]
            if count<project["team_size"]: raise ValueError("Wait until everyone joins before confirming assignments.")
        for task in owned:
            value=(deadlines or {}).get(str(task["id"]))
            if value is None and task["deadline"]: continue
            if value is None and not project["team_size"]: continue
            try:
                deadline=datetime.strptime(value or '',"%Y-%m-%dT%H:%M").replace(tzinfo=timezone(timedelta(hours=5,minutes=30))).astimezone(timezone.utc)
            except ValueError:
                raise ValueError("Enter a completion deadline for each of your assigned tasks (India time).") from None
            if deadline<=datetime.now(timezone.utc): raise ValueError("Task deadlines must be in the future.")
            conn.execute("UPDATE tasks SET deadline=? WHERE id=?",(deadline.isoformat(),task["id"]))
            event(conn,actor["project_id"],actor["id"],"deadline",f"{actor['name']} committed to task #{task['id']} by {value.replace('T',' ')} India time.")
        conn.execute("UPDATE members SET confirmed=1 WHERE id=?",(actor["id"],))
        if actor["role"]=="leader":
            conn.execute("UPDATE projects SET leader_confirmed=1 WHERE id=?",(actor["project_id"],))
        event(conn,actor["project_id"],actor["id"],"confirmed",f"{actor['name']} confirmed the assigned tasks.")


def link_telegram(path, token, chat_id):
    with connection(path) as conn:
        member = conn.execute("SELECT * FROM members WHERE invite_token=?",(token,)).fetchone()
        if not member:
            raise ValueError("This invitation is invalid.")
        if member["telegram_chat_id"] and member["telegram_chat_id"]!=chat_id:
            raise ValueError("This invitation is already linked to another Telegram account.")
        try:
            conn.execute("UPDATE members SET telegram_chat_id=? WHERE id=?",(chat_id,member["id"]))
        except sqlite3.IntegrityError:
            raise ValueError("This Telegram account is already linked to another person in this team.") from None
        conn.execute("UPDATE tasks SET telegram_chat_id=? WHERE member_id=?",(chat_id,member["id"]))
        event(conn,member["project_id"],member["id"],"telegram_linked",f"{member['name']} linked Telegram.")
        return dict(member)


def report_progress(path, member, task_id, percent, note):
    if not 0<=percent<=100 or not note.strip() or len(note)>2000:
        raise ValueError("Use 0–100 percent and a short explanation (up to 2000 characters).")
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT * FROM tasks WHERE id=? AND member_id=?",(task_id,member["id"])).fetchone()
        if not task:
            raise ValueError("You can only update your own assigned task.")
        if task["status"]=="Blocked" and percent==100:
            raise ValueError("Resolve the blocker through a task reply before reporting 100%.")
        status = "Complete" if percent==100 else ("Blocked" if task["status"]=="Blocked" else "In Progress" if percent else "Pending")
        conn.execute("UPDATE tasks SET progress_percent=?,status=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",(percent,status,task_id))
        conn.execute("INSERT INTO status_history(task_id,old_status,new_status,blocker,raw_message) VALUES (?,?,?,?,?)",(task_id,task["status"],status,task["blocker"],f"Owner reported {percent}%: {note}"))
        event(conn,member["project_id"],member["id"],"progress",f"Task #{task_id}: {task['progress_percent']}% → {percent}%. {note}")


def visible_tasks(path, chat_id, *, followup=False, coordinator=None):
    from db import list_tasks
    with connection(path) as conn:
        memberships = [dict(row) for row in conn.execute("SELECT * FROM members WHERE telegram_chat_id=?",(chat_id,))]
    projects = {member["project_id"] for member in memberships if not followup or member["role"]=="leader"}
    tasks = list_tasks(path)
    result = [task for task in tasks if task.get("project_id") in projects]
    if not followup or chat_id==coordinator:
        result += [task for task in tasks if task.get("project_id") is None]
    if followup:
        ready = {project_id for project_id in projects if project_snapshot(path,project_id)["ready"]}
        result = [task for task in result if task.get("project_id") is None or task["project_id"] in ready]
    return result


def backup_database(path, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    source = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        source.close(); target.close()
