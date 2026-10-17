# Five-minute demo runbook

The implementation covers the five planned phases.
Live Telegram delivery, Claude accuracy, a teammate rehearsal, and a recorded
backup remain acceptance gates until performed with real credentials and people.

## Prepare the environment

Use Python 3.11+ on one machine that can stay online during the demo. Polling needs
no public server or webhook. Keep the dashboard on the same machine; it binds to
127.0.0.1 and is intended for a local demo. There is no login or public hosting.

From the project folder, use the existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe seed.py
.\.venv\Scripts\python.exe preflight.py
```

For a first-time setup, follow README.md to create `.venv` and `.env`. The seed
command only fills an empty board; it does not reset tasks or remove history.
The preflight is offline and prints readiness flags without printing credentials.
Exit code 1 means there are TODO items; 0 means configuration checks pass, not that
the live integrations have been verified. Do not reset a working board for rehearsal.

Configure these settings in `.env`:

| Setting | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `COORDINATOR_CHAT_ID` | Coordinator's private chat ID from `/start` |
| `ANTHROPIC_API_KEY` | Claude classification |
| `AI_PROVIDER` | `gemini` for the current demo, or `claude` |
| `GEMINI_API_KEY` | Google AI Studio key for Gemini |
| `GEMINI_MODEL` | Default `gemini-3.5-flash-lite` |
| `CLAUDE_MODEL` | Model supporting structured JSON, default `claude-sonnet-4-6` |
| `DATABASE_PATH` | Board used by the bot, dashboard, preflight, and evidence capture |

Have each teammate press Start in a private chat, then assign their task locally:

```powershell
.\.venv\Scripts\python.exe seed.py --assign 1 123456789
```

Replace the example ID with the actual owner's ID. Each teammate must initiate
contact so Telegram can deliver private follow-ups. Have two repliers ready,
including a backup person/phone. Keep `.env` out of screenshots and recordings.

Run the bot and dashboard in separate PowerShell terminals:

```powershell
.\.venv\Scripts\python.exe bot.py
```

```powershell
.\.venv\Scripts\python.exe dashboard.py
```

Open [the board](http://127.0.0.1:5050). Keep only one polling process per bot token.
Stop services with Ctrl+C. SQLite persists the board across restarts; refresh
the dashboard to fetch new status updates.

## Verify live quality before presenting

```powershell
.\.venv\Scripts\python.exe evaluate.py --live
.\.venv\Scripts\python.exe preflight.py
```

The evaluation attempts 20 requests to the selected provider on synthetic examples and writes
`data/evaluation-report.json`. Require at least 18/20 correct, zero blocker replies
applied as Complete, and clarification for all vague examples. Inspect blocker
reason text as well as the score. Normal Anthropic usage applies.

Preflight accepts only a report labeled `live_gemini` or `live_claude` matching the selected provider and model,
prompt hash, and dataset hash. This checks the recorded report's consistency; it
does not independently authenticate API calls. Simulated rehearsal output is not
a passing live report. Re-run the evaluation after changing prompt/model/dataset.

## Offline rehearsal (available without credentials)

For Gemini, set `AI_PROVIDER=gemini`, `GEMINI_API_KEY`, and `GEMINI_MODEL` locally.
Gemini evaluations default to 12 seconds between requests; API quota or configuration
errors stop the run early. No paid billing is enabled by these scripts. The same
live blockers, history, and clarification acceptance checks apply to either provider.

```powershell
.\.venv\Scripts\python.exe rehearse.py
```

This exercises the actual `/status`, `/followup`, reply, clarification, update,
and `/risk` handlers using scripted adapters and a temporary SQLite database.
It does not load `.env`, contact APIs, message teammates, or touch the project board.
It verifies blocker capture, no change on a vague reply, contextual clarification,
blocker resolution, and three accepted audit events. A unique folder under
`data/rehearsal/` holds the transcript, before/blocked/final boards, risk report,
history, and a blocked-state dashboard fixture. Every run is labeled simulated.

To view that fixture, substitute the printed folder:

```powershell
.\.venv\Scripts\python.exe dashboard.py --database data/rehearsal/RUN_FOLDER/rehearsal.sqlite3 --port 5051
```

The page displays a **Simulated rehearsal** notice. Do not use that database for
the live bot or present scripted replies as a live model/integration result.
The fixture shows the intermediate Blocked state; `final_board.txt` shows the
later completed state. `--output NEW_FOLDER` chooses a different artifact folder;
existing folders are never overwritten.

## Rehearse the live five-minute presentation

Use an open, assigned task for the blocker example and a second open task for
clarification. If previous rehearsals completed a task, use an appropriate open
task; there is no automatic reopen command. Do not clear the existing database.

| Time | Action | Evidence to show |
| --- | --- | --- |
| 00:00–00:30 | Show `/status` and the dashboard | Task, owner, and current status |
| 00:30–01:30 | Coordinator sends `/followup` privately | Real prompt on teammate's phone |
| 01:30–02:30 | Owner replies “Connected, but waiting for test credentials” to the prompt | Actual reply in Telegram |
| 02:30–03:30 | Run `/status`, refresh dashboard, expand reply evidence | Blocked status, credentials reason, history transition |
| 03:30–04:15 | Run `/risk` | Completion counts, blockers, owner names, any assignment gaps |
| 04:15–05:00 | Second owner replies “Will see”, then answers the question | One clarification and unchanged board until a confident answer |

After the presentation, verify explicit resolution: “Credentials received,
integration tested and merged. Nothing pending.” The task should become Complete,
the blocker should clear, and history should show the transition.

`/risk` reports recorded board facts. No deadline, severity, or predicted failure
score is inferred. Scheduled reminders and meeting scheduling remain deferred.

## Capture evidence

Before the live reply:

```powershell
.\.venv\Scripts\python.exe capture_evidence.py --output data/evidence/before-live
```

After the blocker is saved:

```powershell
.\.venv\Scripts\python.exe capture_evidence.py --output data/evidence/after-blocker
```

Use new folder names for repeat runs. Capture reads the database without changing
it and exports tasks plus the latest 15 history entries. It omits Telegram chat ID
columns. Owner names, blocker text, and raw reply evidence remain in local files.
The JSON explicitly says a database snapshot alone does not prove live delivery
or model accuracy. It preserves simulated labeling when given a rehearsal database.

- [ ] Before/after board screenshots saved.
- [ ] Real Telegram follow-up and owner reply screenshots saved.
- [ ] Blocker and accepted history evidence captured.
- [ ] Live evaluation JSON reviewed and passing.
- [ ] Actual full rehearsal completed with teammates.
- [ ] Actual rehearsal recorded and playback checked.
- [ ] Backup phone/replier and mobile hotspot ready.

Store runtime evidence in the ignored `data/` folder. A dashboard layout screenshot
is already available at `docs/screenshots/dashboard-desktop.jpg`; it is UI evidence,
not proof that Telegram or Claude was used live.

## Recover during a demo

| Problem | Action |
| --- | --- |
| Owner receives nothing | Check they started the bot, their assignment, and `/followup` failure summary. Use the backup replier. |
| Follow-up refused | Send it from the configured coordinator's private chat; restart after changing `.env`. |
| Multiple-task ambiguity | Reply directly to the prompt or send `/update TASK_ID the full update`. |
| Claude fails or times out | Show that the old board is preserved; check the coordinator alert and retry with a new full message after recovery. |
| Bot stops after collecting a reply | Restart, inspect `/status`, then resend a new message if the board did not change. Old replies are not replayed. |
| Dashboard unavailable | Check its DATABASE_PATH, run `bot.py --preview` to initialize existing schema, then refresh. |
| Network outage | Switch to the hotspot, or play the previously recorded **real** run. Label an offline rehearsal as simulated if used. |
| Lost acknowledgement | Inspect `/status` before resending; committed updates survive a delivery error. |

Do not claim final live acceptance until the unchecked items above are completed.
