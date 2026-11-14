"""Export a Claude Code session JSONL to a readable markdown transcript.

    python scripts/export_transcript.py <session.jsonl> <out.md> [<subagent.jsonl> ...]

Keeps: user messages, assistant text, tool calls (name + key inputs), tool results (truncated), and the automatic
context summary written when a conversation outgrows its context window (collapsed). Subagent logs, if given, are
appended at the end; their prompts come from the main session, not from the user.
Labels: messages the harness generated (hook feedback, restarts, nudges, background-task events) are marked as
system events, never as the user.
Drops: hidden reasoning blocks, system attachments, internal bookkeeping records.
Scrubs: e-mail addresses (also a bare local part such as "name@"), cloud environment ids, and the session's
scratch/upload paths.
"""
import json
import re
import sys
from pathlib import Path

MAX_RESULT = 1200
MAX_INPUT = 1500
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# an address's local part on its own, e.g. inside a search pattern such as "name@|@provider"
EMAIL_LOCAL = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._%+-]{2,}@(?![A-Za-z0-9-])")


def scrub(s: str) -> str:
    s = EMAIL.sub("<email removed>", s)
    s = EMAIL_LOCAL.sub("<email removed>", s)
    s = re.sub(r"/tmp/claude-0/[^\s'\"]*", "<scratchpad>", s)
    s = re.sub(r"/root/\.claude/uploads/[^\s'\"]*", "<uploaded file>", s)
    s = re.sub(r"\benv_[A-Za-z0-9]{16,}\b", "<environment id>", s)       # cloud environment identifiers
    return s


def trunc(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + f"\n… [{len(s) - n} more characters truncated]"


def records(path: str):
    for line in open(path):
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def tool_call(c: dict) -> str:
    inp, name = c.get("input", {}), c.get("name")
    if name == "Write":
        body = f"file: {inp.get('file_path')} ({len(inp.get('content', ''))} chars)"
    elif name == "Edit":
        body = f"file: {inp.get('file_path')}"
    elif name == "Bash":
        body = f"$ {inp.get('command', '')}\n# {inp.get('description', '')}"
    else:
        body = json.dumps(inp, ensure_ascii=False)
    return f"\n<details><summary>Tool: {name}</summary>\n\n```\n{scrub(trunc(body, MAX_INPUT))}\n```\n</details>\n"


def tool_result(c: dict) -> str:
    res = c.get("content")
    if isinstance(res, list):
        res = "\n".join(x.get("text", "[image]") if isinstance(x, dict) else str(x) for x in res)
    return f"\n<details><summary>Result</summary>\n\n```\n{scrub(trunc(str(res), MAX_RESULT))}\n```\n</details>\n"


def user_text(d: dict, txt: str, subagent: bool) -> str | None:
    """Markdown for a user-role text block, labelled by who really wrote it."""
    txt = re.sub(r"<system-reminder>.*?</system-reminder>", "", txt, flags=re.S).strip()
    if not txt or txt in ("Tool loaded.",) or txt.startswith("[Image: original"):
        return None
    if d.get("isCompactSummary"):
        return ("\n## Context summary (automatic)\n\nThe conversation outgrew its context window here. Claude Code "
                "wrote this summary and the session continued from it.\n\n<details><summary>Summary</summary>\n\n"
                f"{scrub(trunc(txt, 12000))}\n</details>\n")
    if "<task-notification>" in txt:
        m = re.search(r"<summary>(.*?)</summary>", txt, flags=re.S)
        return f"\n_(System event: {scrub(m.group(1).strip()) if m else 'background task finished'})_\n"
    if txt.startswith("This session's worker process was restarted"):
        return "\n_(System event: the session's worker process restarted; work resumed.)_\n"
    if txt.startswith("Base directory for this skill"):
        return "\n_(System: a skill's instructions were loaded here; omitted.)_\n"
    if txt.startswith("Stop hook feedback"):
        return f"\n## Hook (automated, not the user)\n\n{scrub(trunc(txt, 1000))}\n"
    if d.get("isMeta"):
        return f"\n_(System message, not the user: {scrub(trunc(txt, 300))})_\n"
    if subagent:
        return f"\n### Task from the main session\n\n{scrub(trunc(txt, 4000))}\n"
    return f"\n## User\n\n{scrub(trunc(txt, 4000))}\n"


def render(path: str, subagent: bool = False) -> list[str]:
    out = []
    for d in records(path):
        t, m = d.get("type"), d.get("message")
        att = d.get("attachment") if isinstance(d.get("attachment"), dict) else None
        if t == "attachment" and att and att.get("type") == "queued_command" and \
                (att.get("origin") or {}).get("kind") == "human" and att.get("prompt"):
            out.append(f"\n## User (sent while Claude was working)\n\n{scrub(trunc(str(att['prompt']), 4000))}\n")
            continue
        if t not in ("user", "assistant") or not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for c in content or []:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if t == "user" and ct == "text":
                md = user_text(d, c["text"], subagent)
                if md:
                    out.append(md)
            elif t == "assistant" and ct == "text" and c.get("text", "").strip():
                out.append(f"\n{'### Subagent' if subagent else '## Claude'}\n\n{scrub(c['text'].strip())}\n")
            elif t == "assistant" and ct == "tool_use":
                out.append(tool_call(c))
            elif t == "user" and ct == "tool_result":
                out.append(tool_result(c))
    return out


def span(path: str) -> tuple[str, str]:
    ts = [d["timestamp"] for d in records(path) if d.get("timestamp")]
    return min(ts)[:16].replace("T", " "), max(ts)[:16].replace("T", " ")


def main() -> None:
    src, dst, subagents = sys.argv[1], sys.argv[2], sys.argv[3:]
    first, last = span(src)
    out = ["# Claude Code session transcript\n",
           f"One Claude Code cloud session, {first} to {last} UTC. Exported from the session log: long tool "
           "outputs are truncated, internal reasoning blocks and system attachments are omitted, and personal "
           "details are removed. Messages the harness generated are labelled as system events.\n"]
    out += render(src)
    for sa in subagents:
        meta = Path(sa).with_suffix("").with_suffix(".meta.json")
        info = json.loads(meta.read_text()) if meta.exists() else {}
        out.append(f"\n---\n\n# Appendix: subagent \"{info.get('description', Path(sa).stem)}\" "
                   f"({info.get('agentType', 'agent')})\n\nStarted by the main session; its prompt was written "
                   "by Claude, not by the user.\n")
        out += render(sa, subagent=True)
    Path(dst).write_text("".join(out))
    print(f"wrote {dst}")


if __name__ == "__main__":
    main()
