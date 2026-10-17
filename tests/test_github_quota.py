from datetime import datetime,timezone
import os
import unittest
from unittest.mock import patch

import httpx

from github_sync import GitHubRateLimitError,client,github_token,verify_repository
from tests import test_workspace


class GitHubQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_pause_blocks_repeated_requests_and_token_change_can_retry(self):
        calls=[]
        reset=int(datetime.now(timezone.utc).timestamp())+300
        def respond(request):
            calls.append(request)
            if 'authorization' not in request.headers:
                return httpx.Response(403,headers={'x-ratelimit-remaining':'0','x-ratelimit-reset':str(reset)})
            return httpx.Response(200,json={'full_name':'team/project'})
        transport=httpx.MockTransport(respond)
        with patch('github_sync.github_token',return_value=''):
            for _ in range(2):
                with self.assertRaises(GitHubRateLimitError) as result:
                    await verify_repository('https://github.com/team/project',transport)
                self.assertEqual(result.exception.reset_at,reset)
                self.assertIn('India time',str(result.exception))
        self.assertEqual(len(calls),1)
        with patch('github_sync.github_token',return_value='test-only-placeholder'):
            self.assertEqual(await verify_repository('https://github.com/team/project',transport),'team/project')
        self.assertEqual(len(calls),2)

    async def test_429_retry_after_is_respected(self):
        transport=httpx.MockTransport(lambda request:httpx.Response(429,headers={'retry-after':'180'}))
        now=datetime.now(timezone.utc).timestamp()
        with self.assertRaises(GitHubRateLimitError) as result:
            await verify_repository('team/project',transport)
        self.assertGreaterEqual(result.exception.reset_at,now+180)

    async def test_secondary_rate_limit_message_is_reported_as_quota_pause(self):
        reset=int(datetime.now(timezone.utc).timestamp())+120
        transport=httpx.MockTransport(lambda request:httpx.Response(403,json={'message':'You have exceeded a secondary rate limit'},headers={'x-ratelimit-reset':str(reset)}))
        with self.assertRaises(GitHubRateLimitError) as result:
            await verify_repository('team/project',transport)
        self.assertEqual(result.exception.reset_at,reset)

    async def test_invalid_token_has_actionable_message(self):
        with self.assertRaisesRegex(RuntimeError,'expired or invalid'):
            await verify_repository('team/project',httpx.MockTransport(lambda request:httpx.Response(401)))

    def test_token_saved_locally_is_reloaded_without_exposing_it(self):
        with patch.dict(os.environ,{'GITHUB_TOKEN':''}),patch('dotenv.dotenv_values',return_value={'GITHUB_TOKEN':'local-test-token'}):
            self.assertEqual(github_token(),'local-test-token')
        with patch.dict(os.environ,{'GITHUB_TOKEN':'environment-test-token'}):
            self.assertEqual(github_token(),'environment-test-token')


class GitHubDraftTests(unittest.TestCase):
    setUp=test_workspace.WorkspaceWebTests.setUp
    token=test_workspace.WorkspaceWebTests.token
    create=test_workspace.WorkspaceWebTests.create

    def test_rate_limited_form_retains_url_but_does_not_connect_repository(self):
        self.create()
        reset=int(datetime.now(timezone.utc).timestamp())+300
        with patch('github_sync.connect_member_repository',side_effect=GitHubRateLimitError(reset)):
            response=self.client.post('/workspace/profile',data={'csrf':self.token(),'repo':'https://github.com/team/project'},follow_redirects=True)
        html=response.get_data(as_text=True)
        self.assertIn('value="https://github.com/team/project"',html)
        self.assertIn('Try again after',html)
        from db import connection
        with connection(self.path) as conn:
            self.assertIsNone(conn.execute('SELECT github_repo FROM members ORDER BY id LIMIT 1').fetchone()[0])
