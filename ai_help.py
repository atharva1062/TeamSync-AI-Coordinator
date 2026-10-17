"""Intelligent AI assistant supporting any question, multi-language localization, and auto dashboard sync."""

import json
import logging
import re

import httpx

from config import ai_provider, classification_settings
from db import connection

LOGGER = logging.getLogger(__name__)

ASSISTANT_SYSTEM = """You are TeamSync AI Coordinator, an intelligent, helpful, and friendly AI assistant for college and engineering project teams.
You can answer ANY question the team member asks, including:
1. Technical questions: Programming, debugging, errors, frameworks, databases, APIs, Git/GitHub, architecture.
2. Project questions: Inquiries about their assigned tasks, project progress, deadlines, teammate responsibilities.
3. General questions: Conceptual explanations, software guidance, project planning, or polite greetings.

LANGUAGE & LOCALIZATION (CRITICAL RULE):
- Detect the exact language and script of the user's message (Hindi in Devanagari, Marathi in Devanagari, Hinglish in Latin script, Telugu, Tamil, Gujarati, Spanish, French, English, etc.).
- ALWAYS reply in the EXACT SAME local language and script that the user used.
- If the user asks in Marathi, answer in fluent Marathi (मराठी).
- If the user asks in Hindi, answer in fluent Hindi (हिंदी).
- If the user writes in Hinglish, answer naturally in Hinglish.
- If the user asks in English, answer in English.
- Make your tone warm, respectful, practical, and clear.

TASK STATUS UPDATES:
- If the member explicitly states they finished, worked on, or got blocked on one of their tasks (e.g. "I completed task 1", "maine task 2 kar diya hai", "task 3 blocked on credentials", "task 1 is 80% done"), acknowledge it encouragingly and include at the very end of your response a hidden update tag in the exact format:
[TASK_STATUS: task_id, status, blocker_or_none, progress_percent]
Example: [TASK_STATUS: 1, Complete, None, 100] or [TASK_STATUS: 2, Blocked, Waiting for API credentials, 50] or [TASK_STATUS: 3, In Progress, None, 75]
Allowed statuses: Pending, In Progress, Blocked, Complete.
If they are NOT reporting a status change on a specific task, do NOT include any [TASK_STATUS: ...] tag.

RESPONSE GUIDELINES:
- Keep the response clean and under 3000 characters.
- Use plain text formatting suitable for Telegram (bold, clean code blocks).
- Never claim to have executed code on their computer.
"""


async def _generate_ai_response(prompt_payload: str) -> str:
    """Call Gemini or Claude to generate an AI response, with robust fallback models."""
    key, model = classification_settings()
    if not key:
        raise RuntimeError("AI key is not configured.")

    provider = ai_provider()
    if provider == "gemini":
        candidate_models = [model]
        for fallback in ("gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.0-flash"):
            if fallback not in candidate_models:
                candidate_models.append(fallback)

        last_error = None
        for m in candidate_models:
            try:
                async with httpx.AsyncClient(timeout=25) as client:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
                    response = await client.post(
                        url,
                        headers={"x-goog-api-key": key},
                        json={
                            "systemInstruction": {"parts": [{"text": ASSISTANT_SYSTEM}]},
                            "contents": [{"role": "user", "parts": [{"text": prompt_payload}]}],
                            "generationConfig": {"maxOutputTokens": 2048},
                        },
                    )
                    if response.status_code == 200:
                        candidates = response.json().get("candidates", [])
                        if candidates:
                            answer = "".join(
                                part.get("text", "")
                                for part in candidates[0].get("content", {}).get("parts", [])
                                if not part.get("thought")
                            )
                            if answer.strip():
                                return answer.strip()
                    last_error = f"{response.status_code}: {response.text[:200]}"
            except Exception as exc:
                last_error = str(exc)
        raise RuntimeError(f"Gemini generation failed: {last_error}")
    else:
        from anthropic import AsyncAnthropic

        async with AsyncAnthropic(api_key=key, timeout=25, max_retries=1) as client:
            response = await client.messages.create(
                model=model,
                max_tokens=1500,
                system=ASSISTANT_SYSTEM,
                messages=[{"role": "user", "content": prompt_payload}],
            )
            answer = "".join(part.text for part in response.content if part.type == "text")
            if not answer.strip():
                raise RuntimeError("Empty response from Claude")
            return answer.strip()


def _apply_task_update_if_present(path, member, raw_answer: str) -> tuple[str, bool]:
    """Parse [TASK_STATUS: task_id, status, blocker, percent] and persist if valid."""
    pattern = r"\[TASK_STATUS:\s*(\d+)\s*,\s*([A-Za-z ]+)\s*,\s*(None|none|null|.*?)\s*,\s*(\d+)\s*\]"
    match = re.search(pattern, raw_answer)
    if not match:
        return raw_answer, False

    clean_answer = re.sub(pattern, "", raw_answer).strip()
    try:
        task_id = int(match.group(1))
        status = match.group(2).strip()
        blocker_raw = match.group(3).strip()
        percent = max(0, min(100, int(match.group(4))))

        if status not in ("Pending", "In Progress", "Blocked", "Complete"):
            return clean_answer, False

        blocker = None if blocker_raw.lower() in ("none", "null", "") else blocker_raw
        if status == "Blocked" and not blocker:
            blocker = "Dependency or blocker reported"
        elif status != "Blocked":
            blocker = None

        with connection(path) as conn:
            task = conn.execute(
                "SELECT * FROM tasks WHERE id=? AND member_id=?", (task_id, member["id"])
            ).fetchone()
            if not task:
                # Check if task belongs to project
                task = conn.execute(
                    "SELECT * FROM tasks WHERE id=? AND project_id=?", (task_id, member["project_id"])
                ).fetchone()
            if task:
                old_status = task["status"]
                conn.execute(
                    "UPDATE tasks SET status=?, blocker=?, progress_percent=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                    (status, blocker, percent, task_id),
                )
                conn.execute(
                    "INSERT INTO status_history (task_id, old_status, new_status, blocker, raw_message) VALUES (?, ?, ?, ?, ?)",
                    (task_id, old_status, status, blocker, f"Updated via AI conversation: {clean_answer[:200]}"),
                )
                conn.execute(
                    "INSERT INTO project_events (project_id, member_id, kind, detail) VALUES (?, ?, 'status', ?)",
                    (member["project_id"], member["id"], f"Task #{task_id} updated to {status} ({percent}%)."),
                )
                return clean_answer, True
    except Exception as exc:
        LOGGER.warning("Could not apply auto task update: %s", exc)

    return clean_answer, False


async def answer_member_message(path, member: dict, text: str, task_id: int | None = None) -> str:
    """Answer any question or chat from a registered team member with full project context."""
    if not text.strip():
        return "Please ask a question or share your update."

    # Load context: project, tasks, and recent help messages
    with connection(path) as conn:
        project_row = conn.execute("SELECT * FROM projects WHERE id=?", (member["project_id"],)).fetchone()
        project_info = dict(project_row) if project_row else {}

        member_tasks = [
            dict(r)
            for r in conn.execute(
                "SELECT id, title, status, blocker, progress_percent, deadline FROM tasks WHERE member_id=? ORDER BY id",
                (member["id"],),
            )
        ]
        all_tasks = [
            dict(r)
            for r in conn.execute(
                "SELECT id, title, owner_name, status, blocker, progress_percent FROM tasks WHERE project_id=? ORDER BY id",
                (member["project_id"],),
            )
        ]
        history_rows = conn.execute(
            "SELECT role, content FROM help_messages WHERE member_id=? ORDER BY id DESC LIMIT 6",
            (member["id"],),
        ).fetchall()
        history = [dict(r) for r in reversed(history_rows)]

        # Save user message to database so dashboard history updates
        effective_task_id = task_id or (member_tasks[0]["id"] if member_tasks else None)
        conn.execute(
            "INSERT INTO help_messages (project_id, member_id, task_id, role, content) VALUES (?, ?, ?, 'user', ?)",
            (member["project_id"], member["id"], effective_task_id, text),
        )

    context_payload = {
        "member": {"name": member["name"], "role": member["role"]},
        "project": {
            "name": project_info.get("name", "College Project"),
            "team_name": project_info.get("team_name", "Team"),
            "repo": project_info.get("repo"),
            "checkin_time": project_info.get("checkin_time", "21:00"),
        },
        "assigned_tasks": member_tasks,
        "team_tasks_summary": all_tasks[:10],
        "recent_conversation": history,
        "user_message": text,
    }

    try:
        raw_answer = await _generate_ai_response(json.dumps(context_payload, ensure_ascii=False))
    except Exception as exc:
        LOGGER.error("AI response generation failed: %s", exc)
        raw_answer = (
            "I received your message, but I couldn't reach the AI service right now. "
            "Your question has been saved to your dashboard. Please try again in a moment."
        )

    clean_answer, _ = _apply_task_update_if_present(path, member, raw_answer)

    # Persist assistant reply to database
    with connection(path) as conn:
        conn.execute(
            "INSERT INTO help_messages (project_id, member_id, task_id, role, content) VALUES (?, ?, ?, 'assistant', ?)",
            (member["project_id"], member["id"], effective_task_id, clean_answer),
        )

    return clean_answer


async def answer_unlinked_message(text: str) -> str:
    """Answer questions from users who haven't linked their Telegram account yet."""
    payload = {
        "user_status": "Guest / Unlinked Telegram user",
        "user_message": text,
        "instruction": "Answer their question directly and thoroughly in their local language. Remind them gently at the end that they can link their project account using their TeamSync dashboard invite link.",
    }
    try:
        return await _generate_ai_response(json.dumps(payload, ensure_ascii=False))
    except Exception:
        return (
            "Hello! I am TeamSync AI Coordinator. I can help answer your questions about code, projects, and architecture.\n\n"
            "To connect your account to your team project, please open the private Telegram link on your TeamSync workspace dashboard."
        )


async def help_member(path, member, task_id, text):
    """Backwards-compatible helper for workspace web portal and legacy routes."""
    return await answer_member_message(path, member, text, task_id=task_id if task_id else None)
