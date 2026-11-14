# AI usage

| File | What it is |
|---|---|
| [`claude_code_session.md`](claude_code_session.md) | Transcript of the Claude Code session that built this repository |
| [`links.md`](links.md) | Links to any other AI conversations (ChatGPT share links, Claude.ai exports) |

How the transcript was produced: `python scripts/export_transcript.py <session.jsonl> ai_usage/claude_code_session.md`
converts the session log to markdown.
* **Kept:** every user message, every Claude message, and every tool call and result. Long outputs are truncated.
  Answers to Claude's multiple-choice questions appear in the "Result" blocks right after the question.
* **Left out:** Claude's internal reasoning blocks and system attachments.
* **Relabelled:** automated events (background-task completions, a worker restart, a stop hook) are marked as
  system events, not user messages.
* **Scrubbed:** e-mail addresses and local scratch and upload paths.
* **Timing:** it was exported near the end of the session, so the final export-and-push steps are not in it.

The README's ["How AI was used"](../README.md#how-ai-was-used) section summarises where a first version was wrong
and how that was caught.
