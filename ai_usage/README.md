# AI usage

| File | What it is |
|---|---|
| [`claude_code_session.md`](claude_code_session.md) | Full transcript of the Claude Code session that built this repository (one cloud session on 14 Nov 2026; start and end times are in its header) |
| [`links.md`](links.md) | Links to any other AI conversations (ChatGPT share links, Claude.ai exports) |

## How the session went

Everything was built in one Claude Code session. The candidate's requests and choices, in order (times UTC):

| Time | Request or choice |
|---|---|
| 10:23 | Uploaded the assessment brief (PDF) |
| 11:50 | Answered Claude's setup questions: push to a repository the candidate creates; export this session's transcript into `ai_usage/` |
| 12:06 | Gave the repository URL |
| 12:35 | "ping me when the final push is done" |
| 12:49 | Reported README images returning 404 |
| 13:02 | Asked to host the dashboard so reviewers can try it; chose the recorded GitHub Pages replay over a live server |
| 15:16 | Reported the replay link returning 404 (twice) |
| 15:21 | Asked for a demo where you upload belt photos and get the outcome (the inspection page) |
| 15:57 | Reported the inspection page not working on GitHub Pages (cause: Pages publishes the repository root, so the links needed `/docs/`) |
| 16:13 | Asked whether the assignment is complete, or whether the simulator should be made more realistic |
| 16:17 | Asked for a final consistency check and the full session log in this folder |

## How the transcript was produced

`python scripts/export_transcript.py <session.jsonl> ai_usage/claude_code_session.md <subagent.jsonl>` converts
the session log to markdown.
* **Kept:** every user message, every Claude message, and every tool call and result. Long outputs are truncated.
  Answers to Claude's multiple-choice questions appear in the "Result" blocks right after the question.
* **Both context windows:** the session outgrew its context once. The summary Claude Code wrote at that point is
  included, collapsed, under "Context summary (automatic)".
* **Subagent:** one helper agent ran (a web fetch of the BeltCrack dataset page, which the network blocked). Its
  log is appended at the end; its prompt was written by Claude, not by the candidate.
* **Left out:** Claude's internal reasoning blocks and system attachments.
* **Relabelled:** automated events (background-task completions, a worker restart, stop hooks, a "no visible
  output" nudge) are marked as system events, never as user messages.
* **Scrubbed:** e-mail addresses (including fragments such as a bare "name@"), and local scratch and upload paths.
* **Timing:** it was exported at the end of the session, so the last commit-and-push step is not in it.

The README's ["How AI was used"](../README.md#how-ai-was-used) section summarises where a first version was wrong
and how that was caught.
