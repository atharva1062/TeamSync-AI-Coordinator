"""Server-rendered project onboarding and member workspace."""

import asyncio
import hashlib
from pathlib import Path
import secrets

from flask import abort, flash, redirect, render_template, request, session, url_for

from db import connection, initialize
from teamspace import add_task, confirm, create_project, event, join_team, member_by_id, member_by_token, project_snapshot, report_progress


def install(app):
    path = app.config["DATABASE_PATH"]
    initialize(path)
    database_identity=hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:12]
    secret_path = Path(path).resolve().parent / f".workspace-session-key-{database_identity}"
    try:
        with secret_path.open("x",encoding="utf-8") as handle:
            handle.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    app.secret_key = secret_path.read_text(encoding="utf-8")
    app.config.update(SESSION_COOKIE_NAME=f"teamsync_{database_identity}",SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Lax",MAX_CONTENT_LENGTH=65536)
    import os
    app.config['SESSION_COOKIE_SECURE']=os.environ.get('TEAMSYNC_HTTPS')=='1'
    from admin_portal import install as install_admin
    install_admin(app)

    def actor():
        member = member_by_id(path,session.get("member_id",-1))
        if not member:
            abort(403,description="Open your private team invitation to access this project.")
        return member

    def csrf():
        if not secrets.compare_digest(session.get("csrf",""),request.form.get("csrf","!")):
            abort(400,description="The form expired. Refresh the page and try again.")

    @app.context_processor
    def context():
        session.setdefault("csrf",secrets.token_urlsafe(24))
        return {"csrf":session["csrf"]}

    @app.get("/projects/new")
    def project_setup():
        return render_template("setup.html")

    @app.post("/projects")
    def project_create():
        csrf()
        try:
            names = [request.form.get("leader","")]
            member_id = create_project(path,request.form.get("name",""),request.form.get("team_name",""),names,team_size=int(request.form.get("team_size","2")))
        except ValueError as exc:
            flash(str(exc)); return redirect(url_for("project_setup"))
        session["member_id"] = member_id
        return redirect(url_for("workspace_home"))

    @app.get("/teams/join")
    def team_join_form():
        return render_template("join.html")

    @app.post("/teams/join")
    def team_join():
        csrf()
        try:
            member_id=join_team(path,request.form.get("code",""),request.form.get("name",""))
        except ValueError as exc:
            flash(str(exc)); return redirect(url_for("team_join_form"))
        session.clear()
        session["member_id"]=member_id
        return redirect(url_for("workspace_home"))

    @app.get("/invite/<token>")
    def accept_invite(token):
        member = member_by_token(path,token)
        if not member:
            abort(404)
        session.clear()
        session["member_id"] = member["id"]
        return redirect(url_for("workspace_home"))

    @app.get("/workspace")
    def workspace_home():
        if not session.get("member_id") or not member_by_id(path,session.get("member_id")):
            session.clear()
            return redirect(url_for("project_setup"))
        person = actor()
        data = project_snapshot(path,person["project_id"])
        # Invitation capabilities are visible only to the leader and their own member.
        for member in data["members"]:
            if person["role"]!="leader" and member["id"]!=person["id"]:
                member["invite_token"] = None
        with connection(path) as conn:
            help_history = [dict(row) for row in conn.execute("SELECT * FROM help_messages WHERE member_id=? ORDER BY id",(person["id"],))]
        history_page = max(1,min(request.args.get("page",1,type=int) or 1,100000))
        data["history_total"] = len(data["history"])
        data["history"] = data["history"][(history_page-1)*50:history_page*50]
        data["events"] = data["events"][:100]
        import os
        bot_name = os.environ.get("TELEGRAM_BOT_USERNAME","TeamSyncCoordinatorBot").lstrip("@")
        return render_template("workspace.html",**data,actor=person,help_history=help_history,page=history_page,bot_name=bot_name)

    @app.post("/workspace/task")
    def assign():
        csrf(); person=actor()
        try:
            add_task(path,person,request.form.get("title",""),int(request.form.get("owner","0")),int(request.form.get("weight","1")))
        except ValueError as exc:
            flash(str(exc))
        return redirect(url_for("workspace_home"))

    @app.post("/workspace/confirm")
    def confirmation():
        csrf()
        try:
            confirm(path,actor(),{key[9:]:value for key,value in request.form.items() if key.startswith("deadline_")}); flash("Your task assignments and completion deadlines are confirmed.")
        except ValueError as exc:
            flash(str(exc))
        return redirect(url_for("workspace_home"))

    @app.post("/workspace/profile")
    def profile():
        from github_sync import connect_member_repository
        csrf(); person=actor()
        session["repo_draft"]=request.form.get("repo","")[:300]
        try:
            asyncio.run(connect_member_repository(path,person,request.form.get("repo","")))
            session.pop("repo_draft",None)
            flash("Repository verified with GitHub. The bot will monitor its activity.")
        except (ValueError,RuntimeError) as exc:
            flash(str(exc))
        return redirect(url_for("workspace_home"))

    @app.post("/workspace/progress")
    def progress():
        csrf()
        try:
            report_progress(path,actor(),int(request.form.get("task","0")),int(request.form.get("percent","0")),request.form.get("note",""))
        except ValueError as exc:
            flash(str(exc))
        return redirect(url_for("workspace_home"))

    @app.post("/workspace/repo")
    def connect_repo():
        from github_sync import connect_repository
        csrf(); person=actor()
        session["team_repo_draft"]=request.form.get("repo","")[:300]
        try:
            asyncio.run(connect_repository(path,person,request.form.get("repo","")))
            session.pop("team_repo_draft",None)
            flash("GitHub connected. Activity syncs automatically while the bot runs.")
        except (ValueError, RuntimeError) as exc:
            flash(str(exc))
        return redirect(url_for("workspace_home"))

    @app.post("/workspace/schedule")
    def schedule():
        import re
        csrf(); person=actor()
        if person["role"]!="leader": abort(403)
        data=project_snapshot(path,person["project_id"])
        time=request.form.get("time","21:00")
        enabled=request.form.get("enabled")=="yes"
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",time):
            flash("Enter a valid check-in time.")
        elif enabled and (not data["ready"] or not data["project"]["repo"] or any(not member["telegram_chat_id"] for member in data["members"])):
            flash("Confirm every task assignment, connect GitHub, and link each person's Telegram first.")
        else:
            with connection(path) as conn:
                conn.execute("UPDATE projects SET checkin_time=?,checkins_enabled=? WHERE id=?",(time,int(enabled),person["project_id"]))
                event(conn,person["project_id"],person["id"],"schedule",f"Daily check-in {'enabled' if enabled else 'paused'} at {time} Asia/Kolkata.")
            flash("Check-in settings saved. The bot service must remain running.")
        return redirect(url_for("workspace_home"))

    @app.post("/workspace/help")
    def help_chat():
        from ai_help import help_member
        csrf(); person=actor()
        try:
            asyncio.run(help_member(path,person,int(request.form.get("task","0")),request.form.get("message","")))
        except (ValueError,RuntimeError) as exc:
            flash(str(exc))
        return redirect(url_for("workspace_home")+"#help")

    @app.get("/workspace/history.json")
    def export_history():
        person=actor(); data=project_snapshot(path,person["project_id"])
        # Export records, never enrollment capabilities or chat IDs.
        with connection(path) as conn:
            activity=[dict(row) for row in conn.execute("SELECT sha,author,message,url,committed_at FROM repo_activity WHERE project_id=? UNION ALL SELECT a.sha,a.author,a.message,a.url,a.committed_at FROM member_repo_activity a JOIN members m ON m.id=a.member_id WHERE m.project_id=?",(person["project_id"],person["project_id"]))]
            help_history=[dict(row) for row in conn.execute("SELECT task_id,role,content,created_at FROM help_messages WHERE member_id=? ORDER BY id",(person["id"],))]
        return {"project":data["project"]["name"],"tasks":[{key:task[key] for key in ("id","title","owner_name","status","progress_percent","deadline")} for task in data["tasks"]],"status_history":data["history"],"events":data["events"],"repository_activity":activity,"my_ai_help":help_history}

    @app.get("/workspace/version")
    def version():
        person=actor()
        with connection(path) as conn:
            values = conn.execute("SELECT (SELECT MAX(id) FROM project_events WHERE project_id=?), (SELECT MAX(id) FROM status_history WHERE task_id IN (SELECT id FROM tasks WHERE project_id=?)), (SELECT MAX(id) FROM repo_activity WHERE project_id=?), (SELECT MAX(id) FROM help_messages WHERE member_id=?)",(person["project_id"],person["project_id"],person["project_id"],person["id"])).fetchone()
        return {"version":list(values)}
