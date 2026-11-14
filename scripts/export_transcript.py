"""Export a Claude Code session JSONL to a readable markdown transcript.

Keeps: user messages, assistant text, tool calls (name + key inputs), tool results (truncated).
Drops: hidden reasoning blocks, system attachments, internal bookkeeping records.
Scrubs: e-mail addresses and the session's home/upload paths.
"""
import json
import re
import sys

src, dst = sys.argv[1], sys.argv[2]
MAX_RESULT = 1200
MAX_INPUT = 1500
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def scrub(s: str) -> str:
    s = EMAIL.sub("<email removed>", s)
    s = re.sub(r"/tmp/claude-0/[^\s'\"]*", "<scratchpad>", s)
    s = re.sub(r"/root/\.claude/uploads/[^\s'\"]*", "<uploaded file>", s)
    return s


def trunc(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + f"\n… [{len(s) - n} more characters truncated]"


out = ["# Claude Code session transcript\n",
       "Exported from the session log. Long tool outputs are truncated, internal reasoning blocks and system "
       "attachments are omitted, personal details are removed.\n"]
for line in open(src):
    d = json.loads(line)
    t = d.get("type")
    m = d.get("message")
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
            txt = c["text"]
            if "<system-reminder>" in txt or txt.startswith("<"):
                txt = re.sub(r"<system-reminder>.*?</system-reminder>", "", txt, flags=re.S).strip()
                if not txt:
                    continue
            if "<task-notification>" in txt:
                m = re.search(r"<summary>(.*?)</summary>", txt, flags=re.S)
                out.append(f"\n_(System event: {scrub(m.group(1).strip()) if m else 'background task finished'})_\n")
                continue
            if txt.startswith("This session's worker process was restarted"):
                out.append("\n_(System event: the session's worker process restarted; work resumed.)_\n")
                continue
            if txt.startswith("Base directory for this skill"):
                out.append("\n_(System: a skill's instructions were loaded here; omitted.)_\n")
                continue
            if txt.startswith("[Image: original"):
                continue
            if txt.startswith("Stop hook feedback"):
                out.append(f"\n## Hook (automated, not the user)\n\n{scrub(trunc(txt, 1000))}\n")
                continue
            if txt.strip() in ("Tool loaded.",):
                continue
            out.append(f"\n## User\n\n{scrub(trunc(txt, 4000))}\n")
        elif t == "assistant" and ct == "text" and c.get("text", "").strip():
            out.append(f"\n## Claude\n\n{scrub(c['text'].strip())}\n")
        elif t == "assistant" and ct == "tool_use":
            inp = c.get("input", {})
            name = c.get("name")
            if name == "Write":
                body = f"file: {inp.get('file_path')} ({len(inp.get('content', ''))} chars)"
            elif name == "Edit":
                body = f"file: {inp.get('file_path')}"
            elif name == "Bash":
                body = f"$ {inp.get('command', '')}\n# {inp.get('description', '')}"
            else:
                body = json.dumps(inp, ensure_ascii=False)
            out.append(f"\n<details><summary>Tool: {name}</summary>\n\n```\n{scrub(trunc(body, MAX_INPUT))}\n```\n</details>\n")
        elif t == "user" and ct == "tool_result":
            res = c.get("content")
            if isinstance(res, list):
                res = "\n".join(x.get("text", "[image]") if isinstance(x, dict) else str(x) for x in res)
            out.append(f"\n<details><summary>Result</summary>\n\n```\n{scrub(trunc(str(res), MAX_RESULT))}\n```\n</details>\n")
open(dst, "w").write("".join(out))
print(f"wrote {dst}")
