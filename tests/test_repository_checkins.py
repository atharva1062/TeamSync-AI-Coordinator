"""Repository validation and missed-reply evidence, using GitHub/Telegram doubles."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import unittest
import httpx

from tests import test_workspace
from db import connection, record_followup, record_reply
from github_sync import connect_member_repository
from project_worker import check_missed_responses
from teamspace import member_by_id, project_snapshot


class RepositoryCheckinTests(unittest.IsolatedAsyncioTestCase):
    setUp=test_workspace.WorkspaceIntegrationTests.setUp
    def github(self,commits=True,status=200):
        def respond(request):
            if status!=200: return httpx.Response(status)
            if request.url.path.endswith('/branches'):
                return httpx.Response(200,json=[{'name':'main'},{'name':'feature'}])
            if request.url.path.endswith('/commits'):
                items=[{'sha':'b'*40,'author':{'login':'coder'},'commit':{'message':'Work on assigned module','committer':{'date':'2026-10-17T16:00:00Z'}}}] if commits and request.url.params['sha']=='feature' else []
                return httpx.Response(200,json=items)
            return httpx.Response(200,json={'full_name':'coder/app'})
        return httpx.MockTransport(respond)

    def prompt(self):
        task=project_snapshot(self.path,self.project_id)['tasks'][0]
        record_followup(self.path,task['id'],101,77)
        with connection(self.path) as conn:
            conn.execute("UPDATE followups SET created_at='2026-10-17T15:30:00Z'")
        return task

    async def connect(self):
        await connect_member_repository(self.path,self.leader,'https://github.com/coder/app',self.github())

    async def test_unknown_repo_does_not_replace_verified_url(self):
        await self.connect()
        for status in (404,403,429,500):
            with self.assertRaises(RuntimeError):
                await connect_member_repository(self.path,self.leader,'https://github.com/other/missing',self.github(status=status))
        self.assertEqual(member_by_id(self.path,self.leader['id'])['github_repo'],'coder/app')

    async def test_two_hour_delay_feature_branch_evidence_and_restart_deduplication(self):
        await self.connect(); self.prompt()
        await check_missed_responses(self.path,datetime(2026,10,17,17,29,tzinfo=timezone.utc),self.github())
        with connection(self.path) as conn: self.assertEqual(conn.execute('SELECT count(*) FROM missed_checkins').fetchone()[0],0)
        for _ in range(2):
            await check_missed_responses(self.path,datetime(2026,10,17,17,30,tzinfo=timezone.utc),self.github())
        with connection(self.path) as conn:
            self.assertEqual(conn.execute('SELECT outcome FROM missed_checkins').fetchone()[0],'activity')
            self.assertEqual(conn.execute("SELECT count(*) FROM project_events WHERE kind='missed_checkin'").fetchone()[0],1)
        data=project_snapshot(self.path,self.project_id)
        self.assertEqual(data['progress'],0)
        self.assertEqual(data['members'][0]['commits'],1)
        self.assertEqual(len(data['activity']),1)

    async def test_reply_or_progress_cancels_check(self):
        await self.connect(); task=self.prompt()
        record_reply(self.path,task['id'],101,88,'Still working')
        with connection(self.path) as conn: conn.execute("UPDATE replies SET created_at='2026-10-17T16:00:00Z'")
        with patch('github_sync.repository_commits',new_callable=AsyncMock) as api:
            await check_missed_responses(self.path,datetime(2026,10,17,18,tzinfo=timezone.utc))
            api.assert_not_awaited()

    async def test_reply_arriving_during_github_read_cancels_result(self):
        await self.connect(); task=self.prompt()
        async def fetch(*args,**kwargs):
            record_reply(self.path,task['id'],101,89,'Working on the fix')
            with connection(self.path) as conn: conn.execute("UPDATE replies SET created_at='2026-10-17T17:59:00Z'")
            return []
        with patch('github_sync.repository_commits',side_effect=fetch):
            await check_missed_responses(self.path,datetime(2026,10,17,18,tzinfo=timezone.utc))
        with connection(self.path) as conn: self.assertEqual(conn.execute('SELECT count(*) FROM missed_checkins').fetchone()[0],0)

    async def test_failed_api_is_unknown_and_retried_instead_of_marking_inactive(self):
        await self.connect(); self.prompt()
        await check_missed_responses(self.path,datetime(2026,10,17,18,tzinfo=timezone.utc),self.github(status=403))
        with connection(self.path) as conn: self.assertEqual(conn.execute('SELECT outcome FROM missed_checkins').fetchone()[0],'unavailable')
        await check_missed_responses(self.path,datetime(2026,10,17,18,11,tzinfo=timezone.utc),self.github(commits=False))
        with connection(self.path) as conn: self.assertEqual(conn.execute('SELECT outcome FROM missed_checkins').fetchone()[0],'no_activity')
