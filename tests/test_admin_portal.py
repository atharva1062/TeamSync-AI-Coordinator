from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dotenv import dotenv_values
from werkzeug.security import generate_password_hash

from dashboard import create_app
from db import connection, initialize
from teamspace import add_task, create_project, member_by_id


class AdminPortalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "admin.sqlite3"
        initialize(self.path)
        self.leader_id = create_project(self.path, "Campus App", "Builders", ["Asha", "Kabir"])
        self.leader = member_by_id(self.path, self.leader_id)
        with connection(self.path) as conn:
            self.members = [dict(row) for row in conn.execute(
                "SELECT * FROM members WHERE project_id=? ORDER BY id", (self.leader["project_id"],))]
        self.task_id = add_task(self.path, self.leader, "Build sign-in", self.members[1]["id"])
        self.app = create_app(self.path, workspace=True)
        self.app.config.update(ADMIN_USERNAME="rootadmin", ADMIN_PASSWORD_HASH=generate_password_hash("local-test-password"))
        self.client = self.app.test_client()

    def csrf(self):
        with self.client.session_transaction() as session:
            return session["csrf"]

    def sign_in(self):
        self.client.get("/admin")
        response = self.client.post("/admin", data={
            "csrf": self.csrf(), "username": "rootadmin", "password": "local-test-password",
        })
        self.assertEqual(response.status_code, 302)

    def test_admin_is_only_entered_through_admin_path_and_edit_forms_render(self):
        self.assertEqual(self.client.get("/admin/login").status_code, 404)
        response = self.client.get("/admin")
        self.assertIn(b"SECURE SIGN-IN", response.data)
        self.sign_in()
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Campus App", response.data)
        self.assertIn(b"Edit tasks", response.data)
        self.assertIn(b"Task title", response.data)

    def test_admin_can_update_project_member_and_task_with_history(self):
        self.sign_in()
        csrf = self.csrf()
        project_id = self.leader["project_id"]
        response = self.client.post(f"/admin/projects/{project_id}", data={
            "csrf": csrf, "name": "Campus Portal", "team_name": "Launch Crew", "checkin_time": "20:30",
            "leader_confirmed": "yes", "checkins_enabled": "yes",
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.post(f"/admin/members/{self.members[1]['id']}", data={
            "csrf": csrf, "name": "Kabir S", "confirmed": "yes",
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.post(f"/admin/tasks/{self.task_id}", data={
            "csrf": csrf, "title": "Build secure sign-in", "member_id": self.members[0]["id"],
            "status": "Blocked", "blocker": "Waiting for test credentials", "progress_percent": "40",
            "weight": "3", "deadline": "2027-02-01T21:00",
        })
        self.assertEqual(response.status_code, 302)
        with connection(self.path) as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            member = conn.execute("SELECT * FROM members WHERE id=?", (self.members[1]["id"],)).fetchone()
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (self.task_id,)).fetchone()
            history = conn.execute("SELECT * FROM status_history WHERE task_id=?", (self.task_id,)).fetchall()
        self.assertEqual((project["name"], project["team_name"], project["checkin_time"]), ("Campus Portal", "Launch Crew", "20:30"))
        self.assertEqual(member["name"], "Kabir S")
        self.assertEqual((task["title"], task["member_id"], task["status"], task["blocker"], task["progress_percent"], task["weight"]),
                         ("Build secure sign-in", self.members[0]["id"], "Blocked", "Waiting for test credentials", 40, 3))
        self.assertEqual(len(history), 1)

    def test_github_token_is_saved_for_restart_and_used_immediately(self):
        import os
        self.sign_in()
        env_path = Path(self.temp.name) / "server.env"
        self.app.config["SERVER_ENV_PATH"] = env_path
        token = "ghp_" + "a" * 36
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            response = self.client.post("/admin/github", data={"csrf": self.csrf(), "token": token})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(os.environ["GITHUB_TOKEN"], token)
        self.assertEqual(dotenv_values(env_path)["GITHUB_TOKEN"], token)


if __name__ == "__main__":
    unittest.main()
