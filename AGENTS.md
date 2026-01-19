# Automation Scripts Guidelines
_Last updated (UTC): 2026-01-05 19:16:21Z._

## Snapshot
- Primary tool: `scripts/joplin_sync.py` (bidirectional Joplin sync with new-note import).
- Full usage + examples live in `archive/scripts/README.txt`.

## Guardrails
- Run a dry run before live sync.
- Keep credentials in `.env` and never commit them.
