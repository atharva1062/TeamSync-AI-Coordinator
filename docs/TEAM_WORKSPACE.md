# TeamSync project workspace

This extends the original seeded-task demo into separate team projects. Existing
tasks, status history, assignments, and live Telegram evidence are preserved.

## Start

### GitHub quota setup

The local `.env` has an empty `GITHUB_TOKEN` setting. To avoid the shared anonymous
GitHub quota, create a fine-grained token at https://github.com/settings/personal-access-tokens/new.
Limit repository access to the project repository and set Contents to read-only
(Metadata read access is included). Paste the token locally after `GITHUB_TOKEN=`;
do not put it in chat, dashboard fields, source control, or screenshots. Adding the
missing token is detected on the next request; restart services after rotating an
existing environment-loaded token. Expired/invalid tokens produce a separate message.

Verification retains the entered URL when GitHub is unavailable and shows the
retry time in India time. Each service respects GitHub's reset/Retry-After headers,
pausing API attempts until that time; a newly configured token has its own quota.
Failed requests never mark a repository as verified or alter task completion.

```powershell
.\.venv\Scripts\python.exe dashboard.py
.\.venv\Scripts\python.exe bot.py
```

Run these in separate terminals. Open http://127.0.0.1:5050. New visitors see the
project setup form; returning members see their workspace. The original board is
at `/legacy`, or run `dashboard.py --legacy` for the read-only viewer alone.
Explicit `--database FILE` keeps the viewer read-only unless `--workspace` is also
provided. No frontend build or new package installation is required.

## Team workflow

1. The leader enters the project name, team name, their own name, and total team
   size (2–6 including the leader), then chooses **Continue & get team code**.
   Member names are no longer entered by the leader.
2. The leader shares the generated 12-character team code and `/teams/join` page.
   Each teammate enters the code and their own name to join as a member. The leader
   sees the join count live. The team closes to further joins when its chosen size
   is reached. Concurrent joins cannot exceed capacity. Keep each person's private
   invitation for returning to their identity in another browser session.
3. Once everyone joins, the leader assigns tasks. Each owner reviews their tasks,
   enters a future completion deadline per task in India time, and confirms them
   together. The leader approves the plan too (and sets deadlines for their own tasks
   if assigned). Deadlines appear in the shared task table, history, and export.
   Adding a task resets the affected person's confirmation and overall leader approval,
   and pauses check-ins until the revised work and deadlines are accepted.
4. Each person opens **Connect Telegram** from their invitation and presses Start.
   The deep link binds that member to their actual private Telegram account. A
   member cannot be rebound to a different Telegram account, and two people in a
   project cannot share the same account. Names alone cannot identify recipients.
5. After everyone confirms, the leader can connect the GitHub repository. Public
   repositories work without a token. Private repositories require server-side
   `GITHUB_TOKEN` with read access. No repository write operations are performed.
6. Each person connects a **GitHub repository URL** or personal fork. GitHub must
   return a valid repository before it is saved; inaccessible/404/quota responses
   leave the previous connection intact. No username field is required. The bot
   checks visible branches approximately every 10 minutes; the website only verifies
   connections and displays the bot's stored evidence.
7. Enable daily Telegram check-ins after all accounts are linked and GitHub is
   connected. The default is **21:00 Asia/Kolkata every day**. The leader can change
   the time or pause the schedule. Each open task gets a task-specific prompt.

Team codes grant new-member access until the team is full; share only with intended teammates.
Invitation links are access capabilities; share each link only with its intended
person. This is invitation-based access, not a full account/password system.
This build is local: `127.0.0.1` links only work on the same computer. A shared
deployment with a real HTTPS address is needed for teammates on other devices.
The app has not been published or given a public domain by this change.

Existing projects and their invitations remain intact. They keep their existing roster;
new projects use team codes. Existing tasks may add deadlines during reconfirmation.

## Progress and repository activity

Task owners can report 0–100% in the dashboard or Telegram:

```text
/progress TASK_ID 50 Implemented the endpoints; tests are still pending
```

Each person's percentage is `sum(task percentage × weight) / sum(task weights)`.
Project completion uses the same formula across all project tasks. People with no
tasks show 0%. A validated Complete status is 100%; Pending is 0%; In Progress
and Blocked retain the most recently reported percentage. An open blocker prevents
reporting 100% directly; resolve it through a normal task reply first. Percentages
are owner-reported progress, not a measurement of code quality or hours worked.

The team repository feed reads its default branch. Connected member repositories
are checked across visible branches for commits from the last seven days, deduplicated
by repository and SHA. Counts are observed repository activity, not verified individual
contributions. A URL proves neither the member's GitHub identity nor repository ownership.
In a shared repository the system cannot determine who worked from the URL alone.
GitHub activity never silently changes task completion. Stored activity is exportable.

After **two hours** without a response to the latest task prompt, the bot checks the
connected member repository (falling back to the shared project repository). It records
visible commits dated since midnight IST of the check-in day, no visible commits, or
an unavailable check. A task reply, `/progress`, or dashboard progress update cancels
the check; a response during an API request is rechecked before recording a result.
Failures are retried after ten minutes. Successful checks are persisted per prompt
and survive restarts. Results and later responses are visible on the member card and
in project history. Commit dates do not establish when code was pushed, and the bot
does not assume the member forgot, completed the task, or did no work.

## Debugging help in context

Select your own task in **My AI debugging assistant**, paste the error and what
you tried, and ask for help. The task title/status/blocker and your last six
conversation messages for that task are included automatically. From Telegram:

```text
/helpme TASK_ID ModuleNotFoundError when importing flask; I am using a virtual environment
```

Both channels save the conversation to the same per-member project history. Other
members cannot read your private AI chat through the dashboard. AI responses are
advice: the system does not execute shell commands, change files, or verify a fix.
Debugging chat never completes a task. Gemini quota still applies; failed questions
remain saved and can be retried after API access is restored.

## History, live updates, and operations

- Status history, reported percentages, project events, synced repository activity,
  and AI conversations have **no automatic deletion**. History export includes all
  saved status/events/repository activity and the signed-in member's AI conversation.
- The dashboard checks for changes every 20 seconds. It avoids replacing a dirty
  form or an open details panel; use Refresh when you want the newest view there.
- The bot worker creates one full SQLite backup per India-calendar day in the
  database folder's `backups/` directory. Retain the database, backup files, and
  `.workspace-session-key-*` file. Local storage cannot guarantee data survives forever:
  use separate/off-device backup storage for durable retention.
- Scheduled jobs require the bot process and computer to stay running. A daily
  dispatch is claimed once per project/date, preventing repeated sends after a
  restart. A crash or partial delivery is not automatically retried, avoiding
  duplicate prompts after uncertain sends. Check-in outcomes are saved in events.
- GitHub/network/API errors preserve existing task progress and history. More than
  500 new commits in a sync is flagged rather than silently advancing the cursor.
- The schema upgrade is additive. A pre-upgrade copy of the existing board was
  written to `data/backups/before-workspace-*.sqlite3` before migration.

## Verification

Team-code onboarding: all 103 tests passed, including concurrent last-seat joins,
invalid/duplicate enrollment, shared member sessions, roster assignment gates,
future per-task deadlines, and atomic confirmation. Browser QA in the separate QA
database verified leader creation, member code entry, leader assignment, member
deadline confirmation, and shared readiness. Existing live projects were backed up
before the additive migration and were not recreated.

Repository/check-in update: the full 97-test suite passed, followed by five focused
repository tests including the additional reply-during-fetch race (98 unique checks
in total). Live GitHub validation encountered HTTP 403 with zero anonymous API quota;
no live repository was connected during verification. A server read-only GITHUB_TOKEN
or an anonymous quota reset is needed for a live verification attempt on this network.

The original 80 regression checks passed. Thirteen workspace checks passed for
member limits, invitation binding, confirmation gates, weighted progress, access
isolation, CSRF, history export, GitHub deduplication, scheduled dispatch, and
AI-help storage. GitHub and scheduling tests use simulated responses/time; they do
not prove access to your repository or delivery at the next actual 21:00 check-in.

Browser verification used a separate `data/qa-workspace.sqlite3` project: creating
a project, assigning two tasks, leader confirmation, and reporting 50% produced
50% for that owner and 25% overall. A real Gemini request answered a synthetic
Flask error and saved it to that isolated project's AI history. No test project or
AI conversation was inserted into the existing live project board.
