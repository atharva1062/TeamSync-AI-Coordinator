from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from dashboard import create_app
from db import connection,initialize
from teamspace import add_task,confirm,create_project,join_team,member_by_id,project_snapshot


class TeamJoinTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'teams.sqlite3';initialize(self.path)
        self.leader=member_by_id(self.path,create_project(self.path,'Campus App','Builders',['Asha'],team_size=2))
        self.project_id=self.leader['project_id']
        self.code=project_snapshot(self.path,self.project_id)['project']['team_code']

    def join(self):
        return member_by_id(self.path,join_team(self.path,self.code,'Kabir'))

    def test_leader_only_create_then_roster_gate_and_full_team(self):
        self.assertEqual(len(project_snapshot(self.path,self.project_id)['members']),1)
        with self.assertRaises(ValueError):add_task(self.path,self.leader,'Too early',self.leader['id'])
        member=self.join()
        self.assertTrue(project_snapshot(self.path,self.project_id)['roster_ready'])
        self.assertEqual(member['role'],'member')
        with self.assertRaises(ValueError):join_team(self.path,self.code,'Third')
        add_task(self.path,self.leader,'Build API',member['id'])

    def test_invalid_codes_duplicates_and_no_identity_takeover(self):
        for code in ('','wrong','Z'*12):
            with self.assertRaises(ValueError):join_team(self.path,code,'Kabir')
        with self.assertRaises(ValueError):join_team(self.path,self.code,'asha')
        self.join()
        self.assertEqual(len(project_snapshot(self.path,self.project_id)['members']),2)

    def test_concurrent_joins_cannot_exceed_capacity(self):
        def attempt(name):
            try:return join_team(self.path,self.code,name)
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,['Kabir','Meera']))
        self.assertEqual(sum(value is not None for value in results),1)
        self.assertEqual(len(project_snapshot(self.path,self.project_id)['members']),2)

    def test_confirmation_requires_future_deadline_for_every_owned_task_atomically(self):
        member=self.join()
        first=add_task(self.path,self.leader,'API',member['id'])
        second=add_task(self.path,self.leader,'Tests',member['id'])
        for deadlines in (None,{str(first):'2099-01-01T18:00'},{str(first):'2000-01-01T18:00',str(second):'2099-01-01T18:00'}):
            with self.assertRaises(ValueError):confirm(self.path,member,deadlines)
            self.assertFalse(member_by_id(self.path,member['id'])['confirmed'])
            self.assertTrue(all(task['deadline'] is None for task in project_snapshot(self.path,self.project_id)['tasks']))
        confirm(self.path,member,{str(first):'2099-01-01T18:00',str(second):'2099-01-02T20:00'})
        confirm(self.path,self.leader)
        data=project_snapshot(self.path,self.project_id)
        self.assertTrue(data['ready'])
        self.assertEqual(data['tasks'][0]['deadline'],'2099-01-01T12:30:00+00:00')
        self.assertEqual(data['tasks'][0]['deadline_local'],'2099-01-01T18:00')
        third=add_task(self.path,self.leader,'New work',member['id'])
        self.assertFalse(project_snapshot(self.path,self.project_id)['ready'])
        with self.assertRaises(ValueError):confirm(self.path,member)

    def test_web_join_separate_sessions_shared_dashboard_and_deadline_access(self):
        app=create_app(self.path,workspace=True);leader=app.test_client();member=app.test_client()
        leader.get('/invite/'+self.leader['invite_token'])
        member.get('/teams/join')
        self.assertEqual(member.post('/teams/join',data={'code':self.code,'name':'Kabir'}).status_code,400)
        with member.session_transaction() as session:csrf=session['csrf']
        response=member.post('/teams/join',data={'csrf':csrf,'code':self.code.lower(),'name':'Kabir'},follow_redirects=True)
        self.assertIn('Campus App',response.get_data(as_text=True))
        self.assertNotIn(self.code,response.get_data(as_text=True))
        with member.session_transaction() as session:person=member_by_id(self.path,session['member_id'])
        task=add_task(self.path,self.leader,'API',person['id'])
        with member.session_transaction() as session:csrf=session['csrf']
        response=member.post('/workspace/confirm',data={'csrf':csrf,f'deadline_{task}':'2099-01-01T18:00'},follow_redirects=True)
        self.assertIn('2099-01-01',response.get_data(as_text=True))
        self.assertIn('2099-01-01',leader.get('/workspace').get_data(as_text=True))
        exported=member.get('/workspace/history.json').json
        self.assertIsNotNone(exported['tasks'][0]['deadline'])
        self.assertNotIn('team_code',str(exported))
