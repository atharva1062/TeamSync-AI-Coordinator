# Phased implementation

Based on PRD v1.0. Each phase has a reviewable exit gate.
Keep one Python service, SQLite, polling, and no frontend build pipeline.

## Phase 1 — Foundation (implemented; live Telegram verification pending)

- [x] Environment configuration and ignored local secrets/data.
- [x] SQLite tasks and status_history schema, status and blocker constraints.
- [x] Repeatable seed of the four PRD tasks without overwriting existing work.
- [x] `/start` with chat ID and `/status` with board totals.
- [x] Local chat assignment and offline board preview.
- [ ] User adds a BotFather token and verifies both commands in Telegram.

Exit gate: four tasks survive a restart and `/status` shows the same board in chat.
Chat IDs start unassigned; owners must start the bot in a private chat first.

## Phase 2 — Real messaging (implemented; live delivery verification pending)

- [x] Coordinator-triggered `/followup`; configure coordinator chat ID before exposing sends.
- [x] Send each open task's owner a task-specific prompt; skip unassigned and complete tasks.
- [x] Persist sent Telegram message IDs with task/chat IDs for restart-safe reply matching.
- [x] Record last_followup_at only after a successful send and report per-owner failures.
- [x] Match direct replies to prompts. For multiple possible tasks, ask which task;
  do not guess or modify the board. Check sender ownership as well as chat ID.
- [x] Receive and deduplicate text replies without claiming classification is active yet.
- [x] Add `/update TASK_ID text` for explicit selection; check current private-chat ownership.
- [x] Verify authorization, failures, restart matching, reassignment, and ambiguity offline.
- [x] Verify real Telegram delivery and reply collection in the authorized solo demo (four prompts sent, task #4 reply matched).

Exit gate: a real message reaches an owner's phone, and their reply resolves to the correct task.
Startup adds `followups` and `replies` tables without replacing the existing board.
Without an AI key replies remain `received`; with a key Phase 3 processes new replies.

## Phase 3 — Claude classification and audit (implemented; live Claude verification pending)

- [x] Anthropic SDK and configurable Claude model.
- [x] Treat reply text as untrusted data; require strict validated structured output.
- [x] Validate status, blocker, progress_note, and confidence (finite number from 0 to 1).
- [x] Reported open dependencies override completion; a blocked outcome requires a reason.
- [x] Confidence below 0.6 or ambiguous replies: ask one question, leave the task untouched.
- [x] Save task change and status_history entry in one transaction; clear blockers on resolution.
- [x] Handle API timeouts and malformed output without changing the task; notify coordinator.
- [x] Persist clarification context and retain the reply evidence used for each update.
- [x] Reject stale ownership/status snapshots and prevent duplicate classification of a message.
- [x] Verify blocker detection against live Gemini and Telegram: task #4 Pending → Blocked, reason and raw reply saved to history.
- [ ] Verify blocker resolution against the live provider and Telegram.

Exit gate: “Connected, but waiting for test credentials” becomes Blocked with its reason,
and a matching history entry exists. A later resolution clears the blocker.
The offline flow passes with simulated model responses; actual classification accuracy
remains a Phase 4 live evaluation gate. No automatic replay of older collected replies.

## Phase 4 — Quality and read-only dashboard (Gemini live accuracy gate passed)

- [x] Local Flask page with plain HTML, completion summary, color-coded task rows, blockers, and history.
- [x] Read-only SQLite connection, escaped output, filters/search, missing-database handling.
- [x] Cover unrelated chat, typos, Hinglish, ambiguous ownership, and API failures in fixtures and offline tests.
- [x] Labeled 20-reply dataset and optional live evaluation runner with JSON reports.
- [x] Test validation, atomic updates, reply matching, confidence behavior, and failures.
- [x] Browser checks at desktop/mobile widths; search verified and screenshot captured.
- [x] Run `evaluate.py --live` with Gemini and satisfy all accuracy gates: 20/20 correct, zero unsafe completions, all vague cases clarified.

Exit gate: at least 18/20 correct outcomes; zero blocker examples marked Complete;
vague replies preserve the board and prompt clarification.
`evaluate.py` without `--live` only validates the dataset; it never claims model accuracy.
The evaluator requires all labeled vague replies to clarify, even if the overall score is 90%.
Blocker reason scoring uses declared keyword alternatives and still needs human review for meaning.
The live test makes 20 Claude requests using synthetic examples and never touches the board database.
Live Gemini evaluation passed after clarifying the prompt's abstention rules for
future promises and bare completion claims on blocked tasks. The prior 18/20 run
is retained at `data/evaluation-initial-gemini.json`; the passing current report is
`data/evaluation-report.json`. This measures synthetic examples, not a Telegram
round trip or a guarantee for all future replies.

## Phase 5 — Demo readiness (implementation complete; live rehearsal and recording pending)

- [x] `/risk` stretch feature: completion counts, blocker reasons/owners, and missing assignment gaps.
- [x] Offline `preflight.py` for local configuration, database, assignments, and current evaluation report.
- [x] Isolated `rehearse.py` exercises actual handlers with scripted Telegram/Claude and exports labeled artifacts.
- [x] Read-only `capture_evidence.py` saves the board and recent history without Telegram chat ID columns.
- [x] Run instructions, five-minute script, evidence checklist, and recording/fallback plan in `docs/DEMO_RUNBOOK.md`.
- [x] Scheduled reminders and meeting availability remain deferred.
- [ ] Run the full five-minute script with real teammates.
- [ ] Capture actual before/after board, Telegram exchange, blocker, and history evidence.
- [ ] Record the actual live rehearsal and verify playback.

Exit gate: the full real integration loop works and a recorded backup is available.
The scripted rehearsal passes without credentials or API calls, using an isolated board.
It does not satisfy the real-teammate, live-model, or recorded-backup acceptance gates.
Current preflight passes with Telegram and Gemini configured, all four demo tasks
assigned to the coordinator for an explicitly requested solo demo, and the passing
live Gemini report. A real Telegram reply-to-board round trip is verified: four
follow-ups delivered and tracked, task #4 became Blocked with the credentials reason,
and history row #1 preserves the source reply. Local evidence and the supplied
Telegram screenshot are saved in `data/evidence/telegram-live-blocker-20261017/`.
Live blocker resolution, vague-reply clarification in Telegram, a multi-person
rehearsal, and a recorded backup remain pending.

## Decisions and boundaries

Provider change requested after Claude credit exhaustion: Gemini is supported as
an optional live classifier. `.env` now selects Gemini; Claude support is retained.
Both providers share validation and audit behavior. Evaluation reports and preflight
track the selected provider. Gemini's live accuracy gate now passes (20/20).

Post-implementation review: conservative Unicode message sizing, bounded follow-up
titles, clearer duplicate acknowledgements, and recovery from corrupt saved
clarification context are implemented and covered by regression tests. These fixes
do not change the pending live acceptance gates.

- Telegram follows the recommended PRD option.
- A local Flask dashboard is implemented; no React, Vite, npm, or build step.
- The original MVP excluded multiple projects. The later requested team-workspace
  extension adds separate projects and private invitation sessions; uploads,
  voice, and a native mobile app remain outside this implementation.
- Phase 1 commands expose the board to anyone who can reach the bot, matching the
  single-project/no-roles scope. Configure command authorization before Phase 2 sends.
- Never commit `.env`, tokens, real chat IDs, or the runtime database.
- Do not claim live Telegram or Claude acceptance from mocked/offline tests.

## Requested team-workspace extension

- [x] Project/team names, leader, and 2–6 people total.
- [x] Task assignment, individual confirmations, and leader approval gate.
- [x] GitHub repository connection after confirmation, author attribution, automatic
  read-only sync, and activity on the dashboard. Initial scope: default branch.
- [x] Private per-member Telegram enrollment links and linked-account checks.
- [x] Daily 21:00 Asia/Kolkata check-ins, explicitly enabled by the leader after
  confirmation, repository connection, and all Telegram links are complete.
- [x] Owner-reported task percentages, weighted member/project completion, and audit events.
- [x] Task-context AI debugging from dashboard or Telegram with durable private history.
- [x] No automatic history deletion, complete history exports, and daily local SQLite backups.
- [x] Existing live demo board backed up and preserved at `/legacy`.
- [x] Original 80 regressions and 13 workspace checks passed; desktop/mobile browser
  flow and a real Gemini help response verified with an isolated QA project.
- [ ] Verify the team's actual GitHub repository connection and account attribution.
- [ ] Verify an enabled project's real check-in at the configured time.
- [ ] Shared HTTPS deployment for members using other devices; current service is local.

Operation and boundaries: `docs/TEAM_WORKSPACE.md`. The dashboard and updated bot
have been started locally; no actual team project or repository was invented or
registered during implementation. QA data is in a separate database.


## Leader-first team enrollment and owner deadlines

- [x] Leader creates project/team with their own name and intended total size.
- [x] Secure team code, member join page, live roster, and atomic seat limits.
- [x] Assignment gate until the whole team joins; shared dashboard with separate member sessions.
- [x] Future per-task deadlines in IST, saved atomically with owner confirmation.
- [x] Deadline visibility in shared task table, audit history, and history export.
- [x] Additive migration preserves existing teams, tasks, and invitations.
