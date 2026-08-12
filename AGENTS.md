# Automation Scripts Guidelines
_Last updated (UTC): 2026-08-11 11:49:44Z._

## Snapshot
- Primary tool: `scripts/joplin_sync.py` (bidirectional Joplin sync with new-note import).
- Full usage + examples live in `archive/scripts/README.txt`.

## Workspace Todo Intake
Before changing shared automation, read these in order:
1. `../AGENTS.md`
2. `../todo/AGENTS.md`
3. `../todo/scripts.todo.md` if it exists, otherwise `../todo/_INDEX.md`
4. This file, then `STATUS.md`, then `ROADMAP.md`

Keep `STATUS.md` for short script handoffs. Use `../todo/scripts.todo.md` for larger automation changes, audits, or publishing prep.

## Guardrails
- Run a dry run before live sync.
- Keep credentials in `.env` and never commit them.
<!-- logseq:related:start (managed by brain-sync.sh — edit above this line) -->
- - -
🧠 **[[Workspace]]** › **[[scripts]]** #scripts  ·  related: [[scripts/ROADMAP]] [[scripts/STATUS]]
<!-- logseq:related:end -->
