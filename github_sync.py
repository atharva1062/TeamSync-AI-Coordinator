"""Read-only GitHub activity. Commits are evidence of activity, not completion."""

from datetime import datetime, timezone, timedelta
import hashlib
import os
import re

import httpx

from db import connection
from teamspace import event, project_snapshot

_quota_pause={}


def github_token():
    """Read server config again so a locally saved token works without a restart."""
    from config import ROOT
    from dotenv import dotenv_values
    # Explicit local setting supports admin rotation without stale dotenv-loaded env values.
    values=dotenv_values(ROOT/".env")
    return (values.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN","")).strip()


class GitHubRateLimitError(RuntimeError):
    def __init__(self,reset_at):
        self.reset_at=reset_at
        local=datetime.fromtimestamp(reset_at,timezone(timedelta(hours=5,minutes=30)))
        super().__init__(f"GitHub is temporarily limiting requests. Try again after {local.strftime('%d %b, %I:%M %p')} India time, or ask the server owner to configure GitHub access. Your repository URL has been kept; it is not connected yet.")


class GitHubClient(httpx.AsyncClient):
    async def request(self,method,url,**kwargs):
        now=datetime.now(timezone.utc).timestamp()
        reset=_quota_pause.get(self.quota_key,0)
        if reset>now: raise GitHubRateLimitError(reset)
        response=await super().request(method,url,**kwargs)
        message=""
        if response.status_code in (403,429):
            try:message=str(response.json().get("message",""))
            except (ValueError,AttributeError):pass
        limited=(response.status_code==429 or (response.status_code==403 and (
            response.headers.get("x-ratelimit-remaining")=="0" or response.headers.get("retry-after") or "rate limit" in message.casefold())))
        if limited:
            candidates=[now+60]
            try:candidates.append(float(response.headers.get("x-ratelimit-reset","0")))
            except ValueError:pass
            try:candidates.append(now+float(response.headers.get("retry-after","0")))
            except ValueError:pass
            reset=max(candidates)
            _quota_pause[self.quota_key]=reset
            raise GitHubRateLimitError(reset)
        if response.status_code==403:
            raise RuntimeError("GitHub denied access to this repository. Check that it exists and that the server token has repository metadata and contents read access.")
        if response.status_code==401:
            raise RuntimeError("GitHub rejected the configured access token. Ask the server owner to replace an expired or invalid GITHUB_TOKEN. Nothing was connected.")
        return response


def repository_name(value):
    match = re.fullmatch(r"(?:https://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?",value.strip())
    if not match or match[1] in (".","..") or match[2] in (".",".."):
        raise ValueError("Use a GitHub repository URL such as https://github.com/owner/project.")
    return f"{match[1]}/{match[2]}"


def client(transport=None):
    headers={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}
    token=github_token()
    if token: headers["Authorization"]="Bearer "+token
    api=GitHubClient(base_url="https://api.github.com",headers=headers,timeout=15,transport=transport)
    api.quota_key=(hashlib.sha256(token.encode()).hexdigest(),transport)
    return api


async def verify_repository(value,transport=None):
    repo=repository_name(value)
    try:
        async with client(transport) as api:
            response=await api.get(f"/repos/{repo}")
            if response.status_code==404:
                raise RuntimeError("GitHub cannot find this repository or it is private without server access. Check the URL; private repos require a server GITHUB_TOKEN.")
            response.raise_for_status()
            metadata=response.json()
            return repository_name(metadata["full_name"])
    except (httpx.HTTPError,ValueError,KeyError,TypeError):
        raise RuntimeError("GitHub verification failed. Check access or API quota and retry. Nothing was connected.") from None


async def connect_member_repository(path,actor,value,transport=None):
    repo=await verify_repository(value,transport)
    with connection(path) as conn:
        conn.execute("UPDATE members SET github_repo=?,repo_verified_at=?,repo_checked_at=NULL,repo_error=NULL WHERE id=?",(repo,datetime.now(timezone.utc).isoformat(),actor["id"]))
        event(conn,actor["project_id"],actor["id"],"member_repo",f"{actor['name']} connected verified repository {repo}. Repository activity is not proof of individual ownership or task completion.")


async def repository_commits(repo,since,transport=None):
    """Read visible branches; fail rather than report no activity on incomplete reads."""
    found={}
    async with client(transport) as api:
        branches=await api.get(f"/repos/{repo}/branches",params={"per_page":100})
        branches.raise_for_status()
        values=branches.json()
        if not isinstance(values,list) or len(values)>=100:
            raise RuntimeError("Repository has too many branches for a complete check.")
        for branch in values:
            for page in range(1,6):
                response=await api.get(f"/repos/{repo}/commits",params={"sha":branch["name"],"since":since,"per_page":100,"page":page})
                response.raise_for_status()
                batch=response.json()
                if not isinstance(batch,list): raise RuntimeError("Invalid GitHub commit response.")
                for item in batch:
                    if re.fullmatch(r"[0-9a-fA-F]{40,64}",item.get("sha","")):
                        found[item["sha"]]=item
                if len(batch)<100: break
                if page==5: raise RuntimeError("Too much repository activity for a complete check.")
    return list(found.values())


async def sync_member_repository(path,member,since,transport=None):
    repo=member["github_repo"]
    try:
        items=await repository_commits(repo,since,transport)
    except (httpx.HTTPError,RuntimeError,ValueError,KeyError,TypeError):
        with connection(path) as conn:
            previous=conn.execute("SELECT repo_error FROM members WHERE id=?",(member["id"],)).fetchone()[0]
            conn.execute("UPDATE members SET repo_error='GitHub check unavailable; retrying automatically' WHERE id=?",(member["id"],))
            if not previous: event(conn,member["project_id"],member["id"],"github_unavailable",f"Bot could not check {member['name']}'s repository. Access or quota may be unavailable; no inactivity conclusion was made.")
        raise RuntimeError("GitHub check unavailable; no activity conclusion was made.") from None
    now=datetime.now(timezone.utc).isoformat()
    with connection(path) as conn:
        current=conn.execute("SELECT github_repo FROM members WHERE id=?",(member["id"],)).fetchone()
        if not current or current[0]!=repo: raise RuntimeError("Repository changed during the check; retrying.")
        added=0
        for item in items:
            commit=item.get("commit") or {}
            added+=conn.execute("INSERT INTO member_repo_activity(member_id,repo,sha,author,message,url,committed_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",(member["id"],repo,item["sha"],(item.get("author") or {}).get("login"),str(commit.get("message",""))[:4000],f"https://github.com/{repo}/commit/{item['sha']}",(commit.get("committer") or {}).get("date") or (commit.get("author") or {}).get("date"))).rowcount
        previous=conn.execute("SELECT repo_error,repo_checked_at FROM members WHERE id=?",(member["id"],)).fetchone()
        conn.execute("UPDATE members SET repo_checked_at=?,repo_error=NULL WHERE id=?",(now,member["id"]))
        if added or previous[0] or not previous[1]: event(conn,member["project_id"],member["id"],"member_repo_activity",f"Bot checked {member['name']}'s connected repository {repo}: {added} newly observed commits. Task completion unchanged.")
    return items


async def connect_repository(path,actor,value,transport=None):
    if actor["role"]!="leader": raise ValueError("Only the leader can connect a repository.")
    data=project_snapshot(path,actor["project_id"])
    if not data["ready"]: raise ValueError("Every team member and leader must confirm their assigned tasks first.")
    repo=await verify_repository(value,transport)
    if data["project"]["repo"] and data["project"]["repo"]!=repo:
        raise ValueError("This project already has a repository connected.")
    with connection(path) as conn:
        conn.execute("UPDATE projects SET repo=? WHERE id=?",(repo,actor["project_id"]))
        event(conn,actor["project_id"],actor["id"],"repo_connected",f"Connected GitHub repository {repo}.")


async def sync_repository(path,project_id,transport=None):
    data=project_snapshot(path,project_id)
    repo=data["project"]["repo"]
    if not repo: raise RuntimeError("Connect a repository first.")
    commits=[]
    try:
        async with client(transport) as api:
            for page in range(1,6):
                params={"per_page":100,"page":page}
                if data["project"]["repo_synced_at"]: params["since"]=data["project"]["repo_synced_at"]
                response=await api.get(f"/repos/{repo}/commits",params=params)
                if response.status_code==409: break  # Empty repository.
                response.raise_for_status()
                batch=response.json()
                if not isinstance(batch,list): raise ValueError()
                commits.extend(batch)
                if len(batch)<100: break
                if page==5:
                    raise RuntimeError("More than 500 commits since the last sync. No cursor was advanced; narrow or review the repository history before retrying.")
    except (httpx.HTTPError,ValueError):
        raise RuntimeError("GitHub sync failed. Existing activity and task progress were preserved.") from None
    with connection(path) as conn:
        added=0
        for item in commits:
            sha=item.get("sha","")
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}",sha): continue
            commit=item.get("commit") or {}
            added+=conn.execute("INSERT INTO repo_activity(project_id,sha,author,message,url,committed_at) VALUES (?,?,?,?,?,?) ON CONFLICT(project_id,sha) DO NOTHING",(project_id,sha,(item.get("author") or {}).get("login"),str(commit.get("message",""))[:4000],f"https://github.com/{repo}/commit/{sha}",(commit.get("author") or {}).get("date"))).rowcount
        # Use the newest observed commit, with a one-second overlap from inclusive 'since'.
        dates=[(item.get("commit",{}).get("author") or {}).get("date") for item in commits]
        newest=max((value for value in dates if value),default=data["project"]["repo_synced_at"])
        conn.execute("UPDATE projects SET repo_synced_at=? WHERE id=?",(newest,project_id))
        if added: event(conn,project_id,None,"github_activity",f"Synced {added} new commit(s). Task completion unchanged.")
    return added
