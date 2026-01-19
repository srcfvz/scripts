# Automation Scripts Status
_Last updated (UTC): 2026-01-05 19:16:21Z._

## Snapshot
- `joplin_sync.py` keeps local changes on conflicts and skips pulling remote versions.
- New notes created in Joplin notebooks are pulled into `<project>/notes/` using a slugified title.
- Sync include globs target `.md` files; `new_note_subdir: notes` controls where new notes land.
- Detailed workflow and flags are archived in `archive/scripts/README.txt`.

## Next Actions
1. Add tests around conflict resolution, new-note import, and notebook mapping.
2. Decide if `new_note_subdir` should be empty to place notes directly under each project folder.

## Handoff Log
- _2026-01-05 18:51Z:_ Created triad docs and archived README.
