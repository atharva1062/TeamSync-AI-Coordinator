import hashlib
import tempfile
import unittest
from pathlib import Path

from dashboard import board_snapshot, create_app
from db import connection, initialize, seed_tasks


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "board.sqlite3"
        seed_tasks(self.path)
        self.client = create_app(self.path).test_client()

    def test_board_summary_and_private_ids_not_exposed(self):
        with connection(self.path) as conn:
            conn.execute("UPDATE tasks SET telegram_chat_id = 987654321 WHERE id = 1")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Connect the payment API", html)
        self.assertIn("0 of 4 tasks complete", html)
        self.assertNotIn("987654321", html)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_read_requests_do_not_change_database(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.client.get("/")
        self.client.get("/?status=Pending&q=Kabir")
        self.client.get("/static/dashboard.css").close()
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(self.client.post("/", data={"status": "Complete"}).status_code, 405)

    def test_filter_and_empty_results(self):
        html = self.client.get("/?status=In+Progress").get_data(as_text=True)
        self.assertIn("Design the login page", html)
        self.assertNotIn("Connect the payment API", html)
        self.assertIn("No tasks match", self.client.get("/?q=not-a-task").get_data(as_text=True))
        self.assertEqual(self.client.get("/?status=Finished").status_code, 400)

    def test_escapes_titles_owners_blockers_history_and_query(self):
        attack = '<script>alert("x")</script>'
        with connection(self.path) as conn:
            conn.execute("UPDATE tasks SET title=?, owner_name=?, status='Blocked', blocker=? WHERE id=1", (attack, attack, attack))
            conn.execute("INSERT INTO status_history(task_id,old_status,new_status,blocker,raw_message) VALUES (1,'Pending','Blocked',?,?)", (attack, attack))
        html = self.client.get("/", query_string={"q": attack}).get_data(as_text=True)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_blocker_and_history_visible(self):
        with connection(self.path) as conn:
            conn.execute("UPDATE tasks SET status='Blocked', blocker='Waiting for credentials' WHERE id=1")
            conn.execute("INSERT INTO status_history(task_id,old_status,new_status,blocker,raw_message) VALUES (1,'Pending','Blocked','Waiting for credentials','owner: waiting')")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Waiting for credentials", html)
        self.assertIn("owner: waiting", html)
        self.assertIn("View reply evidence", html)

    def test_empty_database(self):
        path = Path(self.temp.name) / "empty.sqlite3"
        initialize(path)
        html = create_app(path).test_client().get("/").get_data(as_text=True)
        self.assertIn("0 of 0 tasks complete", html)
        self.assertIn("first tasks", html)

    def test_missing_database_gives_503_without_creating_file(self):
        path = Path(self.temp.name) / "missing" / "absent.sqlite3"
        response = create_app(path).test_client().get("/")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(path.parent.exists())

    def test_recent_history_is_bounded_and_newest_first(self):
        with connection(self.path) as conn:
            for number in range(20):
                conn.execute("INSERT INTO status_history(task_id,old_status,new_status,raw_message) VALUES (1,'Pending','Pending',?)", (str(number),))
        history = board_snapshot(self.path)["history"]
        self.assertEqual(len(history), 15)
        self.assertEqual(history[0]["raw_message"], "19")
