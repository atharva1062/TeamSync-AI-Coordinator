"""Password-protected platform administration; secrets stay server-side."""
import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import os
from pathlib import Path
import re
import secrets
import time

from dotenv import dotenv_values, set_key
from flask import abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from config import ROOT
from db import STATUSES, connection
from teamspace import event


def install(app):
    path = app.config["DATABASE_PATH"]
    app.config.setdefault("SERVER_ENV_PATH", ROOT / ".env")
    with connection(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS admin_login_attempts(client TEXT NOT NULL,attempted_at REAL NOT NULL)")

    def credentials():
        values = dotenv_values(app.config["SERVER_ENV_PATH"])
        return (
            app.config.get("ADMIN_USERNAME") or values.get("ADMIN_USERNAME") or os.environ.get("ADMIN_USERNAME", ""),
            app.config.get("ADMIN_PASSWORD_HASH") or values.get("ADMIN_PASSWORD_HASH") or os.environ.get("ADMIN_PASSWORD_HASH", ""),
        )

    def fingerprint():
        username, password_hash = credentials()
        return hashlib.sha256((username + "\0" + password_hash).encode()).hexdigest()

    def authorized():
        username, password_hash = credentials()
        return bool(username and password_hash and secrets.compare_digest(session.get("admin_auth", ""), fingerprint())
                    and time.time() - session.get("admin_login_at", 0) < 8 * 3600)

    def csrf():
        if not secrets.compare_digest(session.get("csrf", ""), request.form.get("csrf", "!")):
            abort(400)

    def gate():
        if not authorized():
            return redirect(url_for("admin_portal"))

    def admin_data():
        with connection(path) as conn:
            projects = [dict(row) for row in conn.execute(
                "SELECT p.id,p.name,p.team_name,p.repo,p.checkin_time,p.checkins_enabled,p.leader_confirmed,p.team_size,"
                "(SELECT count(*) FROM members m WHERE m.project_id=p.id) people,"
                "(SELECT count(*) FROM tasks t WHERE t.project_id=p.id) tasks,"
                "(SELECT count(*) FROM tasks t WHERE t.project_id=p.id AND t.status='Blocked') blockers "
                "FROM projects p ORDER BY p.id DESC")]
            events = [dict(row) for row in conn.execute(
                "SELECT e.detail,e.created_at,p.name FROM project_events e JOIN projects p ON p.id=e.project_id "
                "ORDER BY e.id DESC LIMIT 30")]
            for project in projects:
                project["members"] = [dict(row) for row in conn.execute(
                    "SELECT m.id,m.name,m.role,CASE WHEN m.role='leader' THEN p.leader_confirmed ELSE m.confirmed END confirmed "
                    "FROM members m JOIN projects p ON p.id=m.project_id WHERE m.project_id=? ORDER BY m.id", (project["id"],))]
                project["tasks_list"] = [dict(row) for row in conn.execute(
                    "SELECT id,title,member_id,status,blocker,progress_percent,weight,deadline FROM tasks "
                    "WHERE project_id=? ORDER BY id", (project["id"],))]
                for task in project["tasks_list"]:
                    if task["deadline"]:
                        task["deadline_local"] = datetime.fromisoformat(task["deadline"].replace("Z", "+00:00")).astimezone(
                            timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%dT%H:%M")
                    else:
                        task["deadline_local"] = ""
        from github_sync import github_token
        return projects, events, bool(github_token())

    @app.route("/admin", methods=["GET", "POST"])
    def admin_portal():
        if request.method == "POST":
            csrf()
            now = time.time()
            client = request.remote_addr or "unknown"
            with connection(path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM admin_login_attempts WHERE attempted_at<?", (now - 600,))
                attempts = conn.execute("SELECT count(*) FROM admin_login_attempts WHERE client=?", (client,)).fetchone()[0]
                if attempts >= 5:
                    flash("Too many sign-in attempts. Try again in 10 minutes.")
                    return render_template("admin_login.html"), 429
                conn.execute("INSERT INTO admin_login_attempts VALUES (?,?)", (client, now))
            username, password_hash = credentials()
            password = request.form.get("password", "")
            valid = bool(username and password_hash and len(password) <= 1024 and check_password_hash(password_hash, password))
            if not valid or not secrets.compare_digest(request.form.get("username", ""), username):
                flash("Incorrect admin username or password.")
                return render_template("admin_login.html"), 401
            with connection(path) as conn:
                conn.execute("DELETE FROM admin_login_attempts WHERE client=?", (client,))
            person = session.get("member_id")
            session.clear()
            if person:
                session["member_id"] = person
            session["csrf"] = secrets.token_urlsafe(24)
            session["admin_auth"] = fingerprint()
            session["admin_login_at"] = now
            return redirect(url_for("admin_portal"))

        if not authorized():
            return render_template("admin_login.html")
        projects, events, github_configured = admin_data()
        return render_template("admin.html", projects=projects, events=events,
                               github_configured=github_configured, admin_username=credentials()[0], statuses=STATUSES)

    @app.post("/admin/github")
    def admin_github():
        denied = gate()
        if denied:
            return denied
        csrf()
        token = request.form.get("token", "").strip()
        if not re.fullmatch(r"(?:github_pat_|ghp_)[A-Za-z0-9_]{20,250}", token):
            flash("Enter a valid fine-grained or classic GitHub access token.")
            return redirect(url_for("admin_portal"))
        env_path = Path(app.config["SERVER_ENV_PATH"])
        env_path.parent.mkdir(parents=True, exist_ok=True)
        set_key(str(env_path), "GITHUB_TOKEN", token, quote_mode="always")
        os.environ["GITHUB_TOKEN"] = token
        flash("GitHub access saved centrally. Repository links are now verified using this server token.")
        return redirect(url_for("admin_portal"))

    @app.post("/admin/projects/<int:project_id>")
    def admin_project_edit(project_id):
        denied = gate()
        if denied:
            return denied
        csrf()
        name = request.form.get("name", "").strip()
        team_name = request.form.get("team_name", "").strip()
        checkin_time = request.form.get("checkin_time", "")
        if not name or not team_name or len(name) > 120 or len(team_name) > 120:
            flash("Project and team names are required and must be 120 characters or fewer.")
            return redirect(url_for("admin_portal"))
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", checkin_time):
            flash("Enter a valid check-in time.")
            return redirect(url_for("admin_portal"))
        with connection(path) as conn:
            row = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                abort(404)
            conn.execute("UPDATE projects SET name=?,team_name=?,checkin_time=?,checkins_enabled=?,leader_confirmed=? WHERE id=?",
                         (name, team_name, checkin_time, int(request.form.get("checkins_enabled") == "yes"),
                          int(request.form.get("leader_confirmed") == "yes"), project_id))
            event(conn, project_id, None, "admin_edit", "Admin updated project settings.")
        flash("Project settings saved.")
        return redirect(url_for("admin_portal"))

    @app.post("/admin/projects/<int:project_id>/repository")
    def admin_project_repository(project_id):
        denied = gate()
        if denied:
            return denied
        csrf()
        from github_sync import verify_repository
        try:
            repo = asyncio.run(verify_repository(request.form.get("repo", "")))
        except (ValueError, RuntimeError) as exc:
            flash(str(exc))
            return redirect(url_for("admin_portal"))
        with connection(path) as conn:
            project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                abort(404)
            conn.execute("UPDATE projects SET repo=?,repo_synced_at=NULL WHERE id=?", (repo, project_id))
            event(conn, project_id, None, "admin_repo", f"Admin connected GitHub repository {repo} after verification.")
        flash("GitHub verified the repository and the project link was saved.")
        return redirect(url_for("admin_portal"))

    @app.post("/admin/members/<int:member_id>")
    def admin_member_edit(member_id):
        denied = gate()
        if denied:
            return denied
        csrf()
        name = request.form.get("name", "").strip()
        if not name or len(name) > 80:
            flash("Member names are required and must be 80 characters or fewer.")
            return redirect(url_for("admin_portal"))
        with connection(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            member = conn.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
            if not member:
                abort(404)
            duplicate = conn.execute("SELECT 1 FROM members WHERE project_id=? AND id!=? AND lower(name)=lower(?)",
                                     (member["project_id"], member_id, name)).fetchone()
            if duplicate:
                flash("Each team member needs a distinct name.")
                return redirect(url_for("admin_portal"))
            confirmed = int(request.form.get("confirmed") == "yes")
            conn.execute("UPDATE members SET name=?,confirmed=? WHERE id=?", (name, confirmed, member_id))
            conn.execute("UPDATE tasks SET owner_name=? WHERE member_id=?", (name, member_id))
            if member["role"] == "leader":
                conn.execute("UPDATE projects SET leader_confirmed=? WHERE id=?", (confirmed, member["project_id"]))
            event(conn, member["project_id"], member_id, "admin_member_edit", f"Admin updated the profile for {name}.")
        flash("Team member saved.")
        return redirect(url_for("admin_portal"))

    @app.post("/admin/tasks/<int:task_id>")
    def admin_task_edit(task_id):
        denied = gate()
        if denied:
            return denied
        csrf()
        title = request.form.get("title", "").strip()
        status = request.form.get("status", "")
        blocker = request.form.get("blocker", "").strip()
        try:
            member_id = int(request.form.get("member_id", "0"))
            progress = int(request.form.get("progress_percent", "0"))
            weight = int(request.form.get("weight", "1"))
        except ValueError:
            flash("Choose a valid task owner, progress score, and weight.")
            return redirect(url_for("admin_portal"))
        if not title or len(title) > 500 or status not in STATUSES or not 0 <= progress <= 100 or not 1 <= weight <= 10:
            flash("Enter a task title, valid status, progress from 0–100, and weight from 1–10.")
            return redirect(url_for("admin_portal"))
        if status == "Blocked" and (not blocker or len(blocker) > 2000):
            flash("Blocked tasks need a blocker reason of up to 2,000 characters.")
            return redirect(url_for("admin_portal"))
        if status != "Blocked":
            blocker = ""
        if status == "Complete":
            progress = 100
        deadline_text = request.form.get("deadline", "").strip()
        if deadline_text:
            try:
                deadline = datetime.strptime(deadline_text, "%Y-%m-%dT%H:%M").replace(
                    tzinfo=timezone(timedelta(hours=5, minutes=30))).astimezone(timezone.utc).isoformat()
            except ValueError:
                flash("Enter a valid deadline in India time.")
                return redirect(url_for("admin_portal"))
        else:
            deadline = None
        with connection(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["project_id"] is None:
                abort(404)
            owner = conn.execute("SELECT * FROM members WHERE id=? AND project_id=?", (member_id, task["project_id"])).fetchone()
            if not owner:
                flash("Choose a member from this project as the task owner.")
                return redirect(url_for("admin_portal"))
            old_status = task["status"]
            owner_changed = task["member_id"] != member_id
            conn.execute("UPDATE tasks SET title=?,owner_name=?,telegram_chat_id=?,member_id=?,status=?,blocker=?,progress_percent=?,weight=?,deadline=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                         (title, owner["name"], owner["telegram_chat_id"], member_id, status, blocker or None,
                          progress, weight, deadline, task_id))
            if old_status != status:
                conn.execute("INSERT INTO status_history(task_id,old_status,new_status,blocker,raw_message) VALUES (?,?,?,?,?)",
                             (task_id, old_status, status, blocker or None, "Task status edited by platform admin."))
            if owner_changed:
                conn.execute("UPDATE members SET confirmed=0 WHERE id=?", (member_id,))
                conn.execute("UPDATE projects SET leader_confirmed=0,checkins_enabled=0 WHERE id=?", (task["project_id"],))
            event(conn, task["project_id"], None, "admin_task_edit", f"Admin edited task #{task_id} ({title}).")
        flash("Task details saved and added to project history.")
        return redirect(url_for("admin_portal"))

    @app.post("/admin/logout")
    def admin_logout():
        denied = gate()
        if denied:
            return denied
        csrf()
        session.pop("admin_auth", None)
        session.pop("admin_login_at", None)
        return redirect(url_for("admin_portal"))
