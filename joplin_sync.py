#!/usr/bin/env python3
"""
Mirror Markdown files from this repository into a dedicated Joplin notebook.

Usage:
    python scripts/joplin_sync.py
    python scripts/joplin_sync.py --dry-run

Requires the Joplin Web Clipper service to be running locally and the
JOPLIN_TOKEN environment variable to be set.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from fnmatch import fnmatch
from hashlib import sha256
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - dependency optional
    yaml = None

try:
    import requests
except ImportError as exc:  # pragma: no cover - fail fast for missing dependency
    raise SystemExit(
        "The 'requests' package is required. Install it with 'pip install requests'."
    ) from exc


DEFAULT_BASE_URL = os.environ.get("JOPLIN_BASE_URL", "http://127.0.0.1:41184")
STATE_VERSION = 1


class ConfigError(RuntimeError):
    """Raised when the sync configuration is invalid."""


def _posix(path: pathlib.PurePath) -> str:
    return path.as_posix()


def _matches_any(value: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch(value, pattern) for pattern in patterns)


def _load_config(path: pathlib.Path) -> Dict[str, object]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ConfigError(f"Config file is empty: {path}")

    if yaml is not None:
        data = yaml.safe_load(text)  # type: ignore[no-untyped-call]
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:  # pragma: no cover - validated by usage
            raise ConfigError(
                "PyYAML is not installed and config is not valid JSON. "
                "Install PyYAML or convert the config to JSON."
            ) from exc

    if not isinstance(data, dict):
        raise ConfigError("Configuration must be a mapping/object.")

    return data  # type: ignore[return-value]


def _load_env_file(path: Optional[pathlib.Path]) -> None:
    if path is None or not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _resolve_config(raw: Dict[str, object], config_path: pathlib.Path) -> Dict[str, object]:
    root = pathlib.Path(raw.get("root", "."))  # type: ignore[arg-type]
    if not root.is_absolute():
        root = (config_path.parent / root).resolve()

    notebook = raw.get("notebook", "joplin-scribe")
    if not isinstance(notebook, str) or not notebook.strip():
        raise ConfigError("'notebook' must be a non-empty string.")

    notebook_strategy_raw = raw.get("notebook_strategy", "single")
    if not isinstance(notebook_strategy_raw, str):
        raise ConfigError("'notebook_strategy' must be a string if provided.")
    notebook_strategy = notebook_strategy_raw.strip().lower().replace("-", "_")
    if notebook_strategy not in {"single", "per_folder"}:
        raise ConfigError("'notebook_strategy' must be 'single' or 'per_folder'.")

    notebook_prefix = raw.get("notebook_prefix", "")
    if not isinstance(notebook_prefix, str):
        raise ConfigError("'notebook_prefix' must be a string if provided.")

    env_file_raw = raw.get("env_file", ".env")
    env_file: Optional[pathlib.Path]
    if env_file_raw in (None, ""):
        env_file = None
    else:
        env_file_candidate = pathlib.Path(str(env_file_raw))
        if not env_file_candidate.is_absolute():
            env_file_candidate = (config_path.parent / env_file_candidate).resolve()
        env_file = env_file_candidate

    include_globs = raw.get("include_globs", ["**/*.md"])
    exclude_globs = raw.get(
        "exclude_globs",
        [
            "**/node_modules/**",
            ".git/**",
            ".venv/**",
            "__pycache__/**",
        ],
    )

    if not isinstance(include_globs, list) or not all(
        isinstance(item, str) for item in include_globs
    ):
        raise ConfigError("'include_globs' must be a list of string patterns.")

    if not isinstance(exclude_globs, list) or not all(
        isinstance(item, str) for item in exclude_globs
    ):
        raise ConfigError("'exclude_globs' must be a list of string patterns.")

    state_file = raw.get(
        "state_file",
        os.path.join("~", ".local", "share", "joplin-scribe", "state.json"),
    )

    new_note_subdir_raw = raw.get("new_note_subdir", "notes")
    if new_note_subdir_raw is None:
        new_note_subdir = ""
    elif not isinstance(new_note_subdir_raw, str):
        raise ConfigError("'new_note_subdir' must be a string if provided.")
    else:
        new_note_subdir = new_note_subdir_raw.strip()

    remove_deleted = bool(raw.get("remove_deleted", True))

    return {
        "root": root,
        "notebook": notebook,
        "notebook_strategy": notebook_strategy,
        "notebook_prefix": notebook_prefix,
        "env_file": env_file,
        "include_globs": include_globs,
        "exclude_globs": exclude_globs,
        "state_file": pathlib.Path(state_file).expanduser(),
        "new_note_subdir": new_note_subdir,
        "remove_deleted": remove_deleted,
    }


def _git_last_commit(root: pathlib.Path, rel_path: str) -> str:
    try:
        cmd = ["git", "log", "-n", "1", "--pretty=format:%H", "--", rel_path]
        result = subprocess.run(
            cmd,
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:  # pragma: no cover - git missing
        return "git-unavailable"

    if result.returncode != 0:
        return "untracked"

    commit = result.stdout.strip()
    return commit or "untracked"


def _derive_title(content: str, rel_path: pathlib.PurePosixPath) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            heading = line[2:].strip()
            if heading:
                return heading
    stem = pathlib.Path(rel_path).stem.replace("-", " ").replace("_", " ").strip()
    return stem or rel_path.name


def _slugify_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title)
    ascii_title = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = ascii_title.lower().replace("_", " ").strip()
    cleaned = re.sub(r"[^a-z0-9\\s-]", "", cleaned)
    cleaned = re.sub(r"[\\s-]+", "-", cleaned).strip("-")
    return cleaned or "note"


def _unique_note_path(
    root: pathlib.Path,
    folder: str,
    subdir: str,
    slug: str,
    note_id: str,
) -> pathlib.PurePosixPath:
    parts = [segment for segment in (folder, subdir) if segment]
    base_name = f"{slug}.md"
    candidate = pathlib.PurePosixPath(*parts, base_name) if parts else pathlib.PurePosixPath(base_name)
    if not (root / candidate).exists():
        return candidate
    suffix = note_id[:8] if note_id else "dup"
    candidate = pathlib.PurePosixPath(*parts, f"{slug}-{suffix}.md")
    counter = 1
    while (root / candidate).exists():
        candidate = pathlib.PurePosixPath(*parts, f"{slug}-{suffix}-{counter}.md")
        counter += 1
    return candidate


def _build_note_body(content: str, metadata: Dict[str, str]) -> str:
    front_matter_lines = ["---"]
    for key, value in metadata.items():
        front_matter_lines.append(f"{key}: {value}")
    front_matter_lines.append("---\n")

    return "\n".join(front_matter_lines) + content.rstrip() + "\n"


class SyncState:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.notes: Dict[str, Dict[str, object]] = {}
        self.version = STATE_VERSION
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"State file is corrupt: {self.path}") from exc

        if data.get("version") != STATE_VERSION:
            # start fresh if versions mismatch
            return

        self.notes = data.get("notes", {})
        if not isinstance(self.notes, dict):
            self.notes = {}

    def get(self, rel_path: str) -> Optional[Dict[str, object]]:
        return self.notes.get(rel_path)

    def set(self, rel_path: str, info: Dict[str, object]) -> None:
        self.notes[rel_path] = info
        self._dirty = True

    def remove(self, rel_path: str) -> Optional[Dict[str, object]]:
        removed = self.notes.pop(rel_path, None)
        if removed is not None:
            self._dirty = True
        return removed

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": STATE_VERSION, "notes": self.notes}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self._dirty = False


class JoplinClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = requests.Session()
        self.timeout = timeout

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, object]:
        params = kwargs.pop("params", {})
        params["token"] = self.token
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = self.session.request(
            method,
            url,
            params=params,
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Joplin API error {response.status_code}: {response.text.strip() or 'No message'}"
            )
        if not response.content:
            return {}
        return response.json()

    def paginate(self, endpoint: str, params: Optional[Dict[str, object]] = None) -> Iterable[Dict[str, object]]:
        page = 1
        params = dict(params or {})
        while True:
            params.update({"page": page})
            payload = self._request("GET", endpoint, params=params)
            items = payload.get("items")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        yield item
            else:
                # Fallback in case items not wrapped
                if isinstance(payload, dict):
                    yield payload
            if not payload.get("has_more"):
                break
            page += 1

    def ensure_notebook(self, title: str) -> str:
        for folder in self.paginate("folders", params={"fields": "id,title"}):
            if folder.get("title") == title:
                return str(folder["id"])
        created = self._request("POST", "folders", json={"title": title})
        if "id" not in created:
            raise RuntimeError("Failed to create or locate notebook.")
        return str(created["id"])

    def create_note(self, title: str, body: str, parent_id: str) -> Dict[str, object]:
        return self._request("POST", "notes", json={"title": title, "body": body, "parent_id": parent_id})

    def update_note(self, note_id: str, title: str, body: str, parent_id: str) -> Dict[str, object]:
        return self._request(
            "PUT",
            f"notes/{note_id}",
            json={"title": title, "body": body, "parent_id": parent_id},
        )

    def delete_note(self, note_id: str, permanent: bool = True) -> None:
        self._request("DELETE", f"notes/{note_id}", params={"permanent": int(permanent)})

    def get_note(self, note_id: str) -> Dict[str, object]:
        return self._request("GET", f"notes/{note_id}", params={"fields": "id,title,body,user_updated_time,parent_id"})

    def get_notes_in_folder(self, folder_id: str) -> Iterable[Dict[str, object]]:
        return self.paginate(f"folders/{folder_id}/notes", params={"fields": "id,title,body,user_updated_time"})


class NotebookResolver:
    """Resolve notebooks on demand and cache their IDs."""

    def __init__(
        self,
        client: JoplinClient,
        default_notebook: str,
        strategy: str,
        prefix: str,
    ) -> None:
        self._client = client
        self._default_notebook = default_notebook
        self._strategy = strategy
        self._prefix = prefix.strip()
        self._cache: Dict[str, str] = {}

    def _derive_notebook_name(self, rel_path: str) -> str:
        if self._strategy != "per_folder":
            return self._default_notebook
        if "/" not in rel_path:
            return self._default_notebook
        folder = rel_path.split("/", 1)[0]
        if self._prefix:
            return f"{self._prefix}{folder}"
        return folder

    def folder_for_notebook(self, notebook_name: str) -> str:
        if self._strategy != "per_folder":
            return ""
        if notebook_name == self._default_notebook:
            return ""
        folder = notebook_name
        if self._prefix and folder.startswith(self._prefix):
            folder = folder[len(self._prefix) :]
        return folder

    def resolve(self, rel_path: str) -> Tuple[str, str]:
        name = self._derive_notebook_name(rel_path)
        if name not in self._cache:
            self._cache[name] = self._client.ensure_notebook(name)
        return name, self._cache[name]


@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    pulled: int = 0
    conflicts: int = 0


def _hash_body(body: str) -> str:
    return sha256(body.encode("utf-8")).hexdigest()


def _split_front_matter(content: str) -> Tuple[Dict[str, str], str]:
    """Split YAML-ish front matter into metadata + body without requiring PyYAML."""
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        return {}, content

    for i in range(1, len(lines)):
        if lines[i] == "---":
            raw_meta = lines[1:i]
            body = "\n".join(lines[i + 1 :]).lstrip()
            meta: Dict[str, str] = {}
            for line in raw_meta:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                if not key:
                    continue
                meta[key] = value.strip()
            return meta, body

    return {}, content


def _parse_iso_time(time_str: str) -> int:
    """Parse ISO timestamp to milliseconds since epoch."""
    try:
        dt = _dt.datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return 0


def _collect_files(root: pathlib.Path, includes: List[str], excludes: List[str]) -> List[pathlib.Path]:
    matches: List[pathlib.Path] = []
    for file_path in root.rglob("*.md"):
        rel = file_path.relative_to(root)
        posix_path = _posix(rel)
        if excludes and _matches_any(posix_path, excludes):
            continue
        if includes and not _matches_any(posix_path, includes):
            continue
        matches.append(file_path)
    return sorted(matches)


def _normalize_rel_path(path: str) -> str:
    rel = path.strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _pull_from_joplin(
    *,
    root: pathlib.Path,
    state: SyncState,
    client: JoplinClient,
    resolver: NotebookResolver,
    new_note_subdir: str,
    excludes: List[str],
    remove_deleted: bool,
    dry_run: bool,
    stats: SyncStats,
) -> SyncStats:
    """Pull changes from Joplin back to Git."""
    now = _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    
    # Get all notebooks we're tracking
    notebooks = set()
    for rel_path, entry in state.notes.items():
        if isinstance(entry, dict):
            notebooks.add(entry.get("notebook"))
    
    if not notebooks:
        return stats
    
    # For each notebook, check all notes
    for notebook_name in notebooks:
        if not notebook_name:
            continue
            
        try:
            _, notebook_id = resolver.resolve("")  # Get default notebook ID
            folder_id = client.ensure_notebook(str(notebook_name))
            
            for note_data in client.get_notes_in_folder(folder_id):
                note_id = str(note_data.get("id", ""))
                if not note_id:
                    continue

                note_title = str(note_data.get("title", "")).strip()
                
                # Find corresponding file in state
                file_rel_path = None
                state_entry = None
                for rel_path, entry in state.notes.items():
                    if isinstance(entry, dict) and entry.get("note_id") == note_id:
                        file_rel_path = rel_path
                        state_entry = entry
                        break
                
                # Get full note details
                full_note = client.get_note(note_id)
                joplin_body = str(full_note.get("body", ""))
                joplin_updated = int(full_note.get("user_updated_time", 0))
                
                meta, clean_content = _split_front_matter(joplin_body)
                source_path_raw = str(meta.get("source_path", "")).strip()
                source_path = _normalize_rel_path(source_path_raw) if source_path_raw else ""
                synced_by = meta.get("synced_by") == "joplin-scribe"

                origin = None
                if isinstance(state_entry, dict):
                    origin = state_entry.get("origin")
                if not origin:
                    origin = "local" if synced_by or source_path else "remote"

                if not file_rel_path and source_path:
                    file_rel_path = source_path

                if file_rel_path and isinstance(state_entry, dict) and not state_entry.get("origin"):
                    if not dry_run:
                        info = dict(state_entry)
                        info["origin"] = origin
                        state.set(file_rel_path, info)

                if file_rel_path and _matches_any(file_rel_path, excludes):
                    if synced_by and remove_deleted:
                        if dry_run:
                            print(f"[dry-run] Would delete excluded note {file_rel_path} from Joplin")
                        else:
                            client.delete_note(note_id)
                            stats.deleted += 1
                            state.remove(file_rel_path)
                    continue

                if origin == "local":
                    if not file_rel_path:
                        continue
                    file_path = root / file_rel_path
                    last_sync = state_entry.get("last_sync_utc", "") if state_entry else ""
                    last_sync_ms = _parse_iso_time(last_sync)
                    if not file_path.exists():
                        if remove_deleted:
                            if dry_run:
                                print(f"[dry-run] Would delete Joplin note for removed file {file_rel_path}")
                            else:
                                client.delete_note(note_id)
                                stats.deleted += 1
                                state.remove(file_rel_path)
                        continue

                    if joplin_updated > last_sync_ms:
                        if not dry_run:
                            info = dict(state_entry or {})
                            info.update(
                                {
                                    "note_id": note_id,
                                    "title": note_title,
                                    "last_sync_utc": last_sync or now,
                                    "notebook": notebook_name,
                                    "origin": "local",
                                    "force_push": True,
                                }
                            )
                            state.set(file_rel_path, info)
                    continue

                if not file_rel_path or not state_entry:
                    folder = resolver.folder_for_notebook(str(notebook_name))
                    slug = _slugify_title(note_title or "note")
                    rel_path = _unique_note_path(root, folder, new_note_subdir, slug, note_id)
                    file_rel_path = _posix(rel_path)
                    file_path = root / file_rel_path

                    content = clean_content
                    if note_title and not content.lstrip().startswith("# "):
                        heading = f"# {note_title}\n\n"
                        content = f"{heading}{content.lstrip()}" if content.strip() else f"# {note_title}\n"

                    if dry_run:
                        print(f"[dry-run] Would pull new note '{note_title or slug}' → {file_rel_path}")
                        continue

                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(content.rstrip() + "\n", encoding="utf-8")
                    print(f"⬇️  Pulled new note: {file_rel_path}")
                    stats.pulled += 1

                    state.set(
                        file_rel_path,
                        {
                            "note_id": note_id,
                            "content_hash": _hash_body(joplin_body),
                            "title": note_title or _derive_title(content, pathlib.PurePosixPath(file_rel_path)),
                            "last_sync_utc": now,
                            "notebook": notebook_name,
                            "origin": "remote",
                        },
                    )
                    continue
                
                # Check if Joplin version is newer
                last_sync = state_entry.get("last_sync_utc", "")
                last_sync_ms = _parse_iso_time(last_sync)
                
                if joplin_updated <= last_sync_ms:
                    continue  # No changes in Joplin since last sync
                
                conflict = False
                # Check if local file was also modified
                file_path = root / file_rel_path
                if file_path.exists():
                    local_mtime_ms = int(file_path.stat().st_mtime * 1000)
                    
                    if local_mtime_ms > last_sync_ms and joplin_updated > last_sync_ms:
                        # Conflict: both modified
                        print(f"⚠️  CONFLICT: {file_rel_path} modified both locally and in Joplin")
                        print("   Keeping local version; will push to Joplin.")
                        stats.conflicts += 1
                        conflict = True

                if conflict:
                    if dry_run:
                        print(f"[dry-run] Would keep local version for {file_rel_path}")
                    continue
                
                # Pull from Joplin to Git
                if dry_run:
                    print(f"[dry-run] Would pull changes for {file_rel_path} from Joplin")
                else:
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(clean_content, encoding="utf-8")
                    print(f"⬇️  Pulled: {file_rel_path}")
                    stats.pulled += 1
                    
        except Exception as exc:
            print(f"Warning: Failed to pull from notebook '{notebook_name}': {exc}")
    
    return stats



def sync_notes(
    *,
    root: pathlib.Path,
    notebook: str,
    notebook_strategy: str,
    notebook_prefix: str,
    includes: List[str],
    excludes: List[str],
    new_note_subdir: str,
    state: SyncState,
    client: JoplinClient,
    dry_run: bool,
    full_resync: bool,
    remove_deleted: bool,
    bidirectional: bool = True,
) -> SyncStats:
    stats = SyncStats()
    resolver = NotebookResolver(client, notebook, notebook_strategy, notebook_prefix)
    now = _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    # Step 1: Pull changes from Joplin → Git (if bidirectional)
    if bidirectional:
        stats = _pull_from_joplin(
            root=root,
            state=state,
            client=client,
            resolver=resolver,
            new_note_subdir=new_note_subdir,
            excludes=excludes,
            remove_deleted=remove_deleted,
            dry_run=dry_run,
            stats=stats,
        )

    # Step 2: Push changes from Git → Joplin
    files = _collect_files(root, includes, excludes)
    current_set = set()

    for file_path in files:
        rel_path = _posix(file_path.relative_to(root))
        current_set.add(rel_path)
        content = file_path.read_text(encoding="utf-8")
        title = _derive_title(content, pathlib.PurePosixPath(rel_path))

        notebook_name, notebook_id = resolver.resolve(rel_path)

        metadata = {
            "source_path": rel_path,
            "synced_by": "joplin-scribe",
            "last_git_commit": _git_last_commit(root, rel_path),
            "last_sync_utc": now,
        }
        note_body = _build_note_body(content, metadata)
        content_hash = _hash_body(note_body)
        state_entry = state.get(rel_path)

        force_push = bool(state_entry and state_entry.get("force_push"))
        if (
            state_entry
            and not full_resync
            and state_entry.get("content_hash") == content_hash
            and not force_push
        ):
            stats.skipped += 1
            continue

        note_id = state_entry.get("note_id") if state_entry else None
        action = "update" if note_id else "create"

        if dry_run:
            print(
                f"[dry-run] Would {action} note for {rel_path} → title='{title}' "
                f"notebook='{notebook_name}'"
            )
        else:
            if note_id:
                client.update_note(note_id, title, note_body, notebook_id)
                stats.updated += 1
            else:
                created = client.create_note(title, note_body, notebook_id)
                note_id = str(created.get("id"))
                stats.created += 1

        state.set(
            rel_path,
            {
                "note_id": note_id,
                "content_hash": content_hash,
                "title": title,
                "last_sync_utc": now,
                "notebook": notebook_name,
                "origin": state_entry.get("origin") if state_entry else "local",
            },
        )

    if remove_deleted:
        to_remove = [path for path in list(state.notes.keys()) if path not in current_set]
        for rel_path in to_remove:
            entry = state.remove(rel_path)
            if not entry:
                continue
            note_id = entry.get("note_id")
            if not note_id:
                continue
            if dry_run:
                print(f"[dry-run] Would delete note for removed file {rel_path}")
            else:
                client.delete_note(str(note_id))
                stats.deleted += 1

    if not dry_run:
        state.save()

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync repository Markdown files with Joplin (bidirectional).")
    parser.add_argument(
        "--config",
        default="joplin_sync_config.yaml",
        help="Path to the sync configuration file (YAML or JSON).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned actions without modifying Joplin or the state file.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force a full resync even if nothing appears to have changed.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout for Joplin API requests in seconds.",
    )
    parser.add_argument(
        "--push-only",
        action="store_true",
        help="Only push changes from Git to Joplin (skip pulling from Joplin).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = pathlib.Path(args.config).expanduser().resolve()
    try:
        raw_config = _load_config(config_path)
        config = _resolve_config(raw_config, config_path)
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    _load_env_file(config["env_file"])  # type: ignore[arg-type]

    token = os.environ.get("JOPLIN_TOKEN")
    if not token:
        raise SystemExit("Environment variable JOPLIN_TOKEN is required.")

    state = SyncState(config["state_file"])  # type: ignore[arg-type]

    client = JoplinClient(DEFAULT_BASE_URL, token, timeout=args.timeout)

    stats = sync_notes(
        root=config["root"],  # type: ignore[arg-type]
        notebook=config["notebook"],  # type: ignore[arg-type]
        notebook_strategy=config["notebook_strategy"],  # type: ignore[arg-type]
        notebook_prefix=config["notebook_prefix"],  # type: ignore[arg-type]
        includes=config["include_globs"],  # type: ignore[arg-type]
        excludes=config["exclude_globs"],  # type: ignore[arg-type]
        new_note_subdir=config["new_note_subdir"],  # type: ignore[arg-type]
        state=state,
        client=client,
        dry_run=args.dry_run,
        full_resync=args.full,
        remove_deleted=config["remove_deleted"],  # type: ignore[arg-type]
        bidirectional=not args.push_only,
    )

    print(
        f"Sync complete: pulled={stats.pulled}, created={stats.created}, updated={stats.updated}, "
        f"skipped={stats.skipped}, deleted={stats.deleted}, conflicts={stats.conflicts}"
    )


if __name__ == "__main__":
    try:
        main()
    except ConfigError as err:
        raise SystemExit(f"Configuration error: {err}") from err
    except RuntimeError as err:
        raise SystemExit(f"Sync failed: {err}") from err
