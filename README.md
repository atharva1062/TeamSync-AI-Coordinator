# AI Group Project Coordinator

**New: team workspace onboarding, assignment confirmations, GitHub activity,
daily 9:00 PM India check-ins, per-member percentages, and contextual AI help.**
Run `dashboard.py` and open http://127.0.0.1:5050 to create your project. See the
[team workspace guide](docs/TEAM_WORKSPACE.md) for enrollment and operation.
The original demonstration board is preserved at `/legacy`.

A Telegram bot that follows up with teammates,
classifies their replies with Gemini or Claude, and keeps a SQLite task board current.

**Current milestone: all five implementation phases are built; live acceptance is pending.**
Gemini's live 20-reply evaluation now passes 20/20 with zero unsafe completions.
The real Telegram blocker round trip is verified: task #4 moved from Pending to
Blocked with its reason and raw reply recorded in history. Live resolution,
Telegram clarification, and the recorded rehearsal remain pending.
The bot, validated AI updates, audit history, dashboard, evaluation runner,
`/risk`, and demo tools are available. Live setup and acceptance steps are in the
[demo runbook](docs/DEMO_RUNBOOK.md); phase gates are tracked in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Run locally (PowerShell)

Requires Python 3.11 or newer. From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe seed.py
.\.venv\Scripts\python.exe bot.py --preview
```

Copy `.env.example` only on first setup so you preserve existing configuration.
Edit `.env` locally and set `TELEGRAM_BOT_TOKEN` to the token from Telegram's
[@BotFather](https://t.me/BotFather). Do not paste it into chat or commit it.
Select `AI_PROVIDER=gemini` and set `GEMINI_API_KEY` to enable Gemini updates.
For Claude, select `AI_PROVIDER=claude` and set `ANTHROPIC_API_KEY`.
If the selected provider's key is blank, replies are collected without changing
the board. Configure `COORDINATOR_CHAT_ID` to receive failure alerts.

## Gemini setup (current demo provider)

The demo now supports Gemini as an alternative to the PRD's original Claude
choice. Your existing Telegram configuration, task assignments, and Claude key
can remain in place. Set these fields in your local `.env`:

```dotenv
AI_PROVIDER=gemini
GEMINI_API_KEY=your-full-google-ai-studio-key
GEMINI_MODEL=gemini-3.5-flash-lite
```

Create a key in [Google AI Studio](https://aistudio.google.com/apikey).
Gemini calls use the existing HTTPX dependency and Google's structured JSON REST
API. They use the same prompt, strict validation, blocker override, confidence
threshold, audit transaction, and clarification handling as Claude. There is no
automatic fallback to another provider and no automatic billing configuration.
Google lists a free tier for the selected model, subject to account eligibility
and quotas; a key alone does not guarantee available quota.

Run `evaluate.py --live` to validate Gemini before the live demo. Requests are
spaced 12 seconds apart by default for Gemini (`--delay SECONDS` overrides it).
The evaluator stops after an API configuration, authentication, or quota error
instead of repeating rejected requests. Check your account's own rate limits.
Reports identify `live_gemini` or `live_claude`; preflight rejects a passing report
from a different provider. Restart the bot after changing `.env`.

```powershell
.\.venv\Scripts\python.exe bot.py
```

Open your bot in Telegram, press Start, and send `/status`. Ask each teammate to
start a **private chat** with the bot and share the chat ID returned by `/start`.
Assign the correct task locally, for example:

```powershell
.\.venv\Scripts\python.exe seed.py --assign 1 123456789
```

Replace the example ID with Kabir's real private chat ID. The seeded IDs are
1 = Kabir, 2 = Asha, 3 = Rohan, 4 = Meera. No real IDs are included in source.
Teammates must initiate contact before the bot can send them private messages.
Stop polling with Ctrl+C; run only one polling instance per token.

## Phase 2: follow-ups and replies

1. Use `/start` in your own private chat to obtain the coordinator's chat ID.
2. Set `COORDINATOR_CHAT_ID` in `.env` to that ID and restart the bot.
   A blank value disables `/followup`; an invalid value prevents startup.
3. Have teammates start the bot privately and assign their real IDs with `seed.py --assign`.
4. Send `/followup` in the coordinator's private chat. The bot sends a prompt for
   every assigned open task and reports sends, skips, and failures.
5. Teammates reply directly to the relevant prompt, or send
   `/update 1 connected, but waiting for test credentials` for task 1.
6. The bot classifies the update if the AI key is configured; otherwise it
   acknowledges collection and leaves the board unchanged.

A plain unthreaded message can match a single open task. If an owner has several,
the bot asks them to resend with `/update TASK_ID their full update`. Replies to
unknown messages never fall back to a task. Replies to completed tasks and tasks
assigned to another user are rejected. Duplicate message deliveries are stored once.

Startup adds `followups` (sent prompt mapping) and `replies` (raw reply evidence)
to existing databases. Matching survives a restart and rechecks current ownership.
If Telegram accepts a message but the database write fails, the coordinator is
told it was sent but untracked; the owner can use `/update` to select the task.
There is no automatic send retry, which avoids silently sending duplicate prompts
after uncertain network outcomes. Repeating `/followup` intentionally sends new prompts.

Live acceptance: send a follow-up to an assigned teammate, restart the bot, and
reply to that prompt. Confirm the task-specific acknowledgement and a `replies`
row (the board stays unchanged when AI is disabled). Delivery failures must leave last_followup_at
unchanged. These checks require a real bot token and teammate participation.

## Phase 3: Claude updates and clarification

Add these settings to your existing local `.env` (do not overwrite your tokens):

```dotenv
AI_PROVIDER=claude
ANTHROPIC_API_KEY=your-local-api-key
CLAUDE_MODEL=claude-sonnet-4-6
```

Install updated dependencies with `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
and restart `bot.py`. The model is configurable; choose one available to your
Anthropic account that supports structured JSON output. New task replies are sent
to Claude with task title, current status, blocker, and any clarification evidence.
Telegram IDs and owner names are not included in the classification payload.

- Claude returns status, blocker, progress_note, confidence, dependency_open,
  and clarifying_question. Local validation rejects malformed fields, extra or
  duplicate keys, invalid numbers, missing blocker reasons, and truncated/refused output.
- A reported blocker overrides Complete. A missing blocker reason for an open
  dependency rejects the result. Meaning still depends on Claude's interpretation;
  offline mocks do not establish real classification accuracy.
- Confidence below 0.6, no selected status, or an explicit clarifying question
  preserves the board and asks one question. Reply directly to that question or
  use `/update TASK_ID your answer`. The conversation survives restart. An overlong
  exchange asks for a full fresh update rather than guessing a status.
- An accepted result updates the task, inserts history, saves classification evidence,
  and marks the reply processed in one SQLite transaction. Resolving a blocker clears it.
  Accepted progress reports are logged even if the status label stays the same.
- API errors/timeouts preserve the board, store an error type, and attempt to alert
  the configured coordinator. Error text and API secrets are not sent in alerts.
  A changed task/owner during classification also prevents applying that result.
- If saved clarification data is damaged, the latest reply is kept as failed
  evidence and the coordinator is alerted. The invalid clarification context is
  cleared without changing the task, so a new full update can be processed.

Startup adds `classification_results` and `clarification_contexts` tables.
Progress notes and confidence live in the classification JSON; history retains
the owner evidence and any clarification questions that justified the transition.
The model request has a 25-second SDK timeout, a 30-second overall limit, and no
automatic retries. Reply processing is sequential, suitable for the 3–6 person MVP.
Board/risk messages use a conservative UTF-16 size budget, and follow-up titles
are truncated using the same budget so emoji-heavy text stays within message limits.
Duplicate-message notices direct owners to `/status`; they do not imply an earlier
accepted update was undone.

If a reply was collected while AI was disabled, failed processing, or the service
stopped after collecting it, send a **new message** with the full update. Existing
messages are not automatically replayed when a key is added, preventing surprise
changes from old evidence. A lost Telegram acknowledgement does not undo a committed
board update; check `/status` before resending.

Live acceptance: reply “Connected, but waiting for test credentials”, verify Blocked
and its history row, then explicitly report receipt of credentials and completed
testing to verify Complete and a cleared blocker. “Will see” should leave the board
unchanged and prompt one question. Use real Telegram and Claude for this gate.

## Phase 4: local dashboard and quality evaluation

Run the dashboard alongside the bot in a second PowerShell terminal:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe dashboard.py
```

Open [the original board](http://127.0.0.1:5050/legacy). The main URL now opens
the editable team workspace; this legacy page remains read-only. It reads the same configured
`DATABASE_PATH` as the bot and binds to this computer only. Stop it with Ctrl+C.
There is no frontend build step or hosted deployment. It opens SQLite in read-only
mode and cannot create or seed a missing database; run `seed.py` first for a new project.

The page shows completion counts, status filters, task/owner/blocker search,
open blockers, and the latest 15 accepted history entries with expandable reply
evidence. Refresh the page to fetch new updates. Summary counts, blockers, and
history describe the whole board; filters only narrow the task table. Chat IDs
are omitted from board queries. Task text and reply evidence are HTML-escaped.
All timestamps are labeled UTC. Desktop and mobile layouts have been checked in a browser.

### Admin portal and server-side GitHub verification

Open `/admin` directly and sign in with the admin credentials configured in
`.env`. There is no admin link in the public navigation. This workspace has been initialized
with the requested username; its password is stored as a Werkzeug hash, never
as plaintext. Change the initial password before exposing the service publicly.
The admin overview lets you edit project and team settings, member names and
confirmations, task owners, status, deadlines, blockers, weights, and progress.
Repository URLs are saved only after GitHub verification. An admin can save a
read-only GitHub access token centrally; members then paste a repository URL,
which the server checks against GitHub. The token is not displayed to members.
Keep `.env` private and excluded from source control. Set `TEAMSYNC_HTTPS=1`
when serving over HTTPS so browser session cookies are marked secure.

The Flask dashboard still binds to `127.0.0.1` and is not publicly hosted. A
public deployment needs a hosting target, domain/HTTPS, and a persistent
database plus environment configuration; opening the local dashboard does not
make it available to everyone on the internet.

The labeled [20-reply dataset](evaluation/replies.json) covers dependencies,
Hinglish, typos, vague and unrelated replies, explicit blocker resolution, and
prompt injection. Validate it without making API requests:

```powershell
.\.venv\Scripts\python.exe evaluate.py
```

To measure **real model accuracy**, configure your selected provider's key locally and run:

```powershell
.\.venv\Scripts\python.exe evaluate.py --live
```

This attempts 20 selected-provider requests using synthetic examples, subject to
normal API quotas/charges, and does not read or modify the project database or send Telegram messages.
The report defaults to the ignored `data/evaluation-report.json` (change with
`--output PATH`). It records model, prompt/dataset hashes, per-case decisions,
errors, and overall results. Exit code 0 means all gates pass; 1 means a failed
gate; 2 means a setup/input error.

Passing requires at least 18/20 correct decisions, zero labeled blocker replies
applied as Complete, and every vague/unrelated case requesting clarification.
Model/API errors count as incorrect. Blocked examples also require a captured
reason matching a labeled keyword alternative; inspect the reason text manually
because keyword scoring cannot establish semantic accuracy. The score evaluates
the validated decision that production would apply, including blocker overrides
and low-confidence no-change behavior.

Offline tests simulate results to verify the scoring and update flow. They do
**not** establish model accuracy. A live Gemini report has now passed all 20 cases;
it is saved locally at `data/evaluation-report.json`. Re-run after changing the
provider, model, prompt, or dataset.

## Phase 5: risk report and demo tools

Use `/risk` in Telegram to see the completion count, each blocked task and its
owner/reason, and open tasks without a Telegram assignment. Like `/status`, it
reads board facts without changing records. `/followup` remains coordinator-only.

Run an offline preflight to see which setup items remain:

```powershell
.\.venv\Scripts\python.exe preflight.py
```

It checks dependency installation, presence of credentials, coordinator settings,
database integrity/schema, owner assignments, and a passing evaluation report
that matches the current model, prompt, and dataset. It does not print tokens,
authenticate credentials, or contact Telegram/Claude. Exit 1 means TODO items;
exit 0 means configuration is ready for a live rehearsal.

Rehearse without credentials or teammate messages:

```powershell
.\.venv\Scripts\python.exe rehearse.py
```

This runs the real handlers against scripted adapters and an isolated temporary
database. It produces labeled artifacts under `data/rehearsal/`: before/blocked/final
boards, transcript, history, risk report, and a blocked-state dashboard fixture.
No real project data or `.env` is used. It does **not** prove live integration
or Claude accuracy. The dashboard recognizes the fixture and labels it simulated:

```powershell
.\.venv\Scripts\python.exe dashboard.py --database data/rehearsal/RUN_FOLDER/rehearsal.sqlite3 --port 5051
```

Substitute the printed run folder. Capture the actual board and recent history
before/after a live reply using distinct output folders:

```powershell
.\.venv\Scripts\python.exe capture_evidence.py --output data/evidence/before-live
.\.venv\Scripts\python.exe capture_evidence.py --output data/evidence/after-blocker
```

The capture command omits Telegram chat ID columns and never changes the board.
Local exports include owner names and raw reply evidence. Capture snapshots alone
do not verify live messaging; collect the actual Telegram screenshots and video.
Both rehearsal and evidence commands refuse existing output folders.

Follow [DEMO_RUNBOOK.md](docs/DEMO_RUNBOOK.md) for the timed five-minute script,
teammate preparation, evidence checklist, recovery steps, and recording fallback.
Daily scheduling and meeting slot finding remain deferred.

## Foundation and regression verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe seed.py
.\.venv\Scripts\python.exe seed.py
.\.venv\Scripts\python.exe bot.py --preview
```

The second seed preserves existing data. Expect four tasks: zero done, one in
progress, zero blocked, three pending. An empty database displays instructions
to seed it. Bot startup creates tables but does not insert demo data implicitly.

Live acceptance: `/start` returns the correct chat ID, `/status` shows all four
tasks, and restarting the bot preserves the board. This requires your token and
a working connection to Telegram; offline tests do not prove live delivery.

## Files and data

| File | Responsibility |
| --- | --- |
| `bot.py` | Polling, Telegram command handlers, error notice, offline preview |
| `config.py` | Project-local .env loading and database path |
| `db.py` | Schema, queries, non-destructive seed, owner assignment |
| `board.py` | Plain text board and Telegram message splitting |
| `seed.py` | Local setup and assignment command |
| `messaging.py` | Coordinator follow-ups, owner verification, reply collection |
| `classifier.py` | Gemini/Claude structured output and shared strict local validation |
| `processing.py` | Classification, clarification, acknowledgements, failure alerts |
| `dashboard.py`, `templates/`, `static/` | Local, read-only Flask task board |
| `evaluate.py`, `evaluation/replies.json` | Opt-in live quality evaluation on 20 labeled replies |
| `preflight.py` | Offline readiness checks without printing credentials |
| `rehearse.py` | Isolated, scripted demo with clearly labeled artifacts |
| `capture_evidence.py` | Read-only board and recent history export |
| `docs/DEMO_RUNBOOK.md` | Live setup, five-minute script, evidence, and fallback |
| `tests/test_foundation.py` | Storage invariants and offline handler checks |
| `tests/test_messaging.py` | Authorization, delivery failures, reply matching, deduplication |
| `tests/test_classifier.py` | Output validation and offline Anthropic SDK transport |
| `tests/test_processing.py` | Board changes, audit rollback, clarification, failures |
| `tests/test_dashboard.py` | Escaping, read-only behavior, filters, unavailable/empty boards |
| `tests/test_evaluation.py` | Dataset validity, scoring gates, failures, key requirement |
| `tests/test_demo.py` | Risk report, preflight, rehearsal, and evidence capture |

The database defaults to `data/coordinator.sqlite3`, relative to the project
folder, and timestamps use UTC. `.env` and runtime data are ignored by Git.
The database enforces a nonempty reason for Blocked and no blocker for other
statuses. New accepted Claude results update the board and history atomically.

The polling implementation follows the
[python-telegram-bot Application documentation](https://docs.python-telegram-bot.org/en/v22.8/telegram.ext.application.html).
Task prompts use [Telegram's ForceReply interface](https://docs.python-telegram-bot.org/en/v22.8/telegram.forcereply.html).
Message sizing stays below the [Telegram Bot API text limit](https://core.telegram.org/bots/api#sendmessage).
Claude requests follow Anthropic's
[structured output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
Gemini uses Google's [structured output API](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
and a model with a listed [free tier](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.5-flash-lite).
Dashboard templates use [Flask's automatic HTML escaping](https://flask.palletsprojects.com/en/stable/templating/).
