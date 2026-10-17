import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from dashboard import create_app
from db import connection, initialize, list_tasks
from github_sync import connect_repository, repository_name, sync_repository
from project_worker import dispatch_due
from teamspace import add_task, confirm, create_project, join_team, link_telegram, member_by_id, project_snapshot, report_progress, visible_tasks


class WorkspaceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"board.sqlite3"; initialize(self.path)
        self.leader=member_by_id(self.path,create_project(self.path,"Campus App","Builders",["Asha","Kabir"]))
        self.members=project_snapshot(self.path,self.leader["project_id"])["members"]

    def prepare(self):
        for index,member in enumerate(self.members):
            add_task(self.path,self.leader,f"Task {index}",member["id"])
            link_telegram(self.path,member["invite_token"],101+index)
            confirm(self.path,member)
        confirm(self.path,self.leader)  # Last task assignment reset leader approval.

    def test_team_limit_and_duplicates(self):
        for names in (["Only"],[str(i) for i in range(7)],["Asha","asha"]):
            with self.assertRaises(ValueError): create_project(self.path,"App","Team",names)
        self.assertEqual(len(self.members),2)

    def test_all_members_and_leader_must_confirm_and_task_add_resets_gate(self):
        self.prepare(); self.assertTrue(project_snapshot(self.path,self.leader["project_id"])["ready"])
        add_task(self.path,self.leader,"Extra work",self.members[1]["id"])
        data=project_snapshot(self.path,self.leader["project_id"])
        self.assertFalse(data["ready"]); self.assertFalse(data["project"]["checkins_enabled"])

    def test_cross_project_assignment_and_member_assignment_forbidden(self):
        stranger=member_by_id(self.path,create_project(self.path,"Other","Other team",["One","Two"]))
        with self.assertRaises(ValueError): add_task(self.path,self.leader,"Bad",stranger["id"])
        with self.assertRaises(ValueError): add_task(self.path,self.members[1],"Bad",self.leader["id"])

    def test_telegram_enrollment_unique_and_cannot_be_taken_over(self):
        link_telegram(self.path,self.members[0]["invite_token"],101)
        with self.assertRaises(ValueError): link_telegram(self.path,self.members[0]["invite_token"],102)
        with self.assertRaises(ValueError): link_telegram(self.path,self.members[1]["invite_token"],101)

    def test_weighted_progress_and_blocker_guard(self):
        first=add_task(self.path,self.leader,"Big",self.leader["id"],3)
        add_task(self.path,self.leader,"Small",self.leader["id"],1)
        report_progress(self.path,self.leader,first,50,"Core module done")
        data=project_snapshot(self.path,self.leader["project_id"])
        self.assertEqual(data["members"][0]["progress"],38)
        with connection(self.path) as conn:
            conn.execute("UPDATE tasks SET status='Blocked',blocker='Review pending' WHERE id=?",(first,))
        with self.assertRaises(ValueError): report_progress(self.path,self.leader,first,100,"done")
        with self.assertRaises(ValueError): report_progress(self.path,self.members[1],first,60,"not mine")

    def test_bot_visibility_does_not_leak_other_project(self):
        self.prepare()
        other=member_by_id(self.path,create_project(self.path,"Private","Else",["Private leader","Private member"]))
        add_task(self.path,other,"Secret project task",other["id"])
        self.assertNotIn("Secret project task",json.dumps(visible_tasks(self.path,101)))


class WorkspaceWebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"board.sqlite3"
        self.app=create_app(self.path,workspace=True); self.client=self.app.test_client()

    def token(self,client=None):
        with (client or self.client).session_transaction() as session: return session["csrf"]

    def create(self):
        self.client.get("/projects/new")
        response=self.client.post("/projects",data={"csrf":self.token(),"name":"Project <script>x</script>","team_name":"Builders","leader":"Asha","team_size":"2"},follow_redirects=True)
        self.assertEqual(response.status_code,200)
        with self.client.session_transaction() as session: leader=member_by_id(self.path,session["member_id"])
        data=project_snapshot(self.path,leader["project_id"])
        join_team(self.path,data["project"]["team_code"],"Kabir")

    def test_setup_is_first_page_and_csrf_blocks_untrusted_post(self):
        response=self.client.get("/",follow_redirects=True)
        self.assertIn("Your next great project",response.get_data(as_text=True))
        self.assertEqual(self.client.post("/projects",data={"name":"Bad"}).status_code,400)

    def test_setup_tasks_confirmations_and_escaping(self):
        self.create(); html=self.client.get("/workspace").get_data(as_text=True)
        self.assertIn("&lt;script&gt;",html); self.assertNotIn("<script>x</script>",html)
        with self.client.session_transaction() as session: leader=member_by_id(self.path,session["member_id"])
        member=project_snapshot(self.path,leader["project_id"])["members"][1]
        self.client.post("/workspace/task",data={"csrf":self.token(),"title":"Build API","owner":member["id"],"weight":2})
        guest=self.app.test_client(); guest.get("/invite/"+member["invite_token"],follow_redirects=True)
        result=guest.post("/workspace/task",data={"csrf":self.token(guest),"title":"Unauthorized","owner":leader["id"]},follow_redirects=True)
        self.assertIn("Only the leader",result.get_data(as_text=True))
        self.assertEqual(len(list_tasks(self.path)),1)
        task=list_tasks(self.path)[0]
        guest.post("/workspace/confirm",data={"csrf":self.token(guest),f"deadline_{task['id']}":"2099-10-17T21:00"})
        self.assertTrue(member_by_id(self.path,member["id"])["confirmed"])

    def test_unenrolled_user_cannot_export_project_history(self):
        self.assertEqual(self.client.get("/workspace/history.json").status_code,403)

    def test_history_export_omits_invitation_tokens_and_chat_ids(self):
        self.create()
        response=self.client.get("/workspace/history.json")
        self.assertEqual(response.status_code,200)
        text=response.get_data(as_text=True)
        self.assertNotIn("invite_token",text); self.assertNotIn("telegram_chat_id",text)
        self.assertIn("my_ai_help",response.json)
        self.assertEqual(len(self.client.get("/workspace/version").json["version"]),4)


class WorkspaceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"board.sqlite3"; initialize(self.path)
        self.leader=member_by_id(self.path,create_project(self.path,"App","Team",["One","Two"]))
        self.project_id=self.leader["project_id"]
        self.members=project_snapshot(self.path,self.project_id)["members"]
        for index,member in enumerate(self.members):
            add_task(self.path,self.leader,"Task "+str(index),member["id"])
            link_telegram(self.path,member["invite_token"],index+101)
            confirm(self.path,member)
        confirm(self.path,self.leader)

    async def test_github_sync_is_idempotent_and_does_not_infer_completion(self):
        def respond(request):
            if request.url.path.endswith("/commits"):
                return httpx.Response(200,json=[{"sha":"a"*40,"author":{"login":"coder"},"commit":{"message":"Implement task #1","author":{"date":"2026-10-17T10:00:00Z"}}}])
            return httpx.Response(200,json={"full_name":"team/app"})
        transport=httpx.MockTransport(respond)
        await connect_repository(self.path,self.leader,"https://github.com/team/app",transport)
        self.assertEqual(await sync_repository(self.path,self.project_id,transport),1)
        self.assertEqual(await sync_repository(self.path,self.project_id,transport),0)
        data=project_snapshot(self.path,self.project_id)
        self.assertEqual(len(data["activity"]),1); self.assertEqual(data["progress"],0)
        self.assertTrue(all(task["status"]=="Pending" for task in data["tasks"]))
        with self.assertRaises(ValueError): repository_name("https://malicious.example/team/app")

    async def test_scheduler_9pm_ist_once_per_day_survives_restart(self):
        with connection(self.path) as conn:
            conn.execute("UPDATE projects SET repo='team/app',checkins_enabled=1 WHERE id=?",(self.project_id,))
        counter=100
        async def send(**kwargs):
            nonlocal counter
            counter+=1; return SimpleNamespace(message_id=counter)
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=send))
        await dispatch_due(self.path,bot,datetime(2026,10,17,15,29,tzinfo=timezone.utc))
        bot.send_message.assert_not_awaited()
        await dispatch_due(self.path,bot,datetime(2026,10,17,15,30,tzinfo=timezone.utc))
        self.assertEqual(bot.send_message.await_count,2)
        await dispatch_due(self.path,bot,datetime(2026,10,17,16,0,tzinfo=timezone.utc))
        self.assertEqual(bot.send_message.await_count,2)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM followups").fetchone()[0],2)

    async def test_ai_help_saves_context_and_does_not_change_task_progress(self):
        from ai_help import help_member
        response=httpx.Response(200,request=httpx.Request("POST","https://example.test"),json={"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"Check the import path, then restart your local process."}]}}]})
        fake=SimpleNamespace(post=AsyncMock(return_value=response))
        manager=SimpleNamespace(__aenter__=AsyncMock(return_value=fake),__aexit__=AsyncMock(return_value=False))
        from unittest.mock import MagicMock
        manager=MagicMock(); manager.__aenter__=AsyncMock(return_value=fake); manager.__aexit__=AsyncMock(return_value=False)
        task=project_snapshot(self.path,self.project_id)["tasks"][0]
        with patch("ai_help.classification_settings",return_value=("test-key","test-model")),patch("ai_help.ai_provider",return_value="gemini"),patch("ai_help.httpx.AsyncClient",return_value=manager):
            answer=await help_member(self.path,self.members[0],task["id"],"ModuleNotFoundError when importing my module")
        self.assertIn("import path",answer)
        with connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM help_messages").fetchone()[0],2)
        self.assertEqual(project_snapshot(self.path,self.project_id)["progress"],0)
