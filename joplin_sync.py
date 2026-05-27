#!/usr/bin/env python3
"""
Joplin Scribe - Selective Bidirectional Sync
Focuses on Project Triads (AGENTS.md, ROADMAP.md, STATUS.md) and specific docs.
"""

import os
import pathlib
import json
import hashlib
import datetime as _dt
import requests
import socket
from typing import Dict, List, Optional, Tuple, Set

# Configuration
JOPLIN_BASE_URL = os.environ.get("JOPLIN_BASE_URL", "http://127.0.0.1:41184")
JOPLIN_TOKEN = os.environ.get("JOPLIN_TOKEN")
ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
STATE_FILE = pathlib.Path("~/.local/share/joplin-scribe/state_v2.json").expanduser()

HOSTNAME = socket.gethostname().lower()
ROOT_NOTEBOOK = f"Chat ({HOSTNAME})"


# Only sync these specific files + any .md files in 'notes' or 'docs' folders
TRIAD_FILES = {"AGENTS.md", "ROADMAP.md", "STATUS.md", "GEMINI.md", "README.md"}
ALLOWED_DIRS = {"notes", "docs", "todo"}

class JoplinAPI:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _req(self, method: str, path: str, **kwargs):
        params = kwargs.get("params", {})
        params["token"] = self.token
        kwargs["params"] = params
        url = f"{self.base_url}/{path.lstrip('/')}"
        res = requests.request(method, url, timeout=15, **kwargs)
        if res.status_code >= 400:
            print(f"API Error ({res.status_code}): {res.text}")
            res.raise_for_status()
        return res.json() if res.content else {}

    def get_notebooks(self):
        res = self._req("GET", "folders")
        return res.get("items", []) if isinstance(res, dict) else res

    def ensure_notebook(self, title: str, parent_id: str = None) -> str:
        folders = self.get_notebooks()
        expected_parent = parent_id or ""
        for f in folders:
            if f["title"] == title and f.get("parent_id", "") == expected_parent:
                return f["id"]
        
        data = {"title": title}
        if parent_id: data["parent_id"] = parent_id
        return self._req("POST", "folders", json=data)["id"]

    def upsert_note(self, note_id: Optional[str], title: str, body: str, parent_id: str):
        data = {"title": title, "body": body, "parent_id": parent_id}
        if note_id:
            return self._req("PUT", f"notes/{note_id}", json=data)
        return self._req("POST", "notes", json=data)

    def get_note(self, note_id: str):
        return self._req("GET", f"notes/{note_id}", params={"fields": "id,title,body,user_updated_time,parent_id"})

    def delete_note(self, note_id: str):
        return self._req("DELETE", f"notes/{note_id}")

def get_rel_path(full_path: pathlib.Path) -> str:
    return str(full_path.relative_to(ROOT_DIR))

def should_sync(file_path: pathlib.Path) -> bool:
    rel = file_path.relative_to(ROOT_DIR)
    parts = rel.parts
    
    # Exclude common ignores
    excludes = {".git", "node_modules", ".venv", ".venv_sync", "__pycache__", "archive", "repos", "artifacts"}
    if any(p in excludes or p.startswith(".") for p in parts): return False
    
    # Is it a triad file at any depth?
    if file_path.name in TRIAD_FILES: return True
    
    # Is it in an allowed top-level directory?
    if parts and parts[0] in ALLOWED_DIRS: return True
    
    # Root level MDs
    if len(parts) == 1 and file_path.suffix == ".md": return True
    
    return False

def get_notebook_hierarchy(file_path: pathlib.Path) -> List[str]:
    rel = file_path.relative_to(ROOT_DIR)
    parts = list(rel.parts[:-1]) # All directories except the filename
    return [ROOT_NOTEBOOK] + parts


def main():
    if not JOPLIN_TOKEN:
        print("Error: JOPLIN_TOKEN not set.")
        return

    api = JoplinAPI(JOPLIN_BASE_URL, JOPLIN_TOKEN)
    
    # Load state
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f: state = json.load(f)

    # Scan Files
    files_to_sync = [p for p in ROOT_DIR.rglob("*.md") if should_sync(p)]
    current_paths = set()

    print(f"Starting sync for {len(files_to_sync)} files...")

    for f_path in files_to_sync:
        rel_path = get_rel_path(f_path)
        current_paths.add(rel_path)
        
        content = f_path.read_text(encoding="utf-8")
        if not content.strip(): continue # Skip empty files
        
        # Determine Notebook
        hierarchy = get_notebook_hierarchy(f_path)
        parent_id = None
        for folder_name in hierarchy:
            parent_id = api.ensure_notebook(folder_name, parent_id)
        
        # Check State
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        note_id = state.get(rel_path, {}).get("note_id")
        last_hash = state.get(rel_path, {}).get("hash")

        # Bidirectional Check (Pull from Joplin)
        if note_id:
            try:
                remote_note = api.get_note(note_id)
                remote_updated = remote_note.get("user_updated_time", 0)
                local_mtime = int(f_path.stat().st_mtime * 1000)
                
                # If remote is newer and changed since last sync
                if remote_updated > state.get(rel_path, {}).get("last_sync", 0) + 1000:
                    if remote_note.get("body") and remote_note["body"] != content:
                        print(f"⬇️  Pulling update: {rel_path}")
                        f_path.write_text(remote_note["body"], encoding="utf-8")
                        content = remote_note["body"]
                        file_hash = hashlib.sha256(content.encode()).hexdigest()
            except Exception as e:
                print(f"Warning: Could not check remote note {note_id}: {e}")

        # Push to Joplin if local changed
        if file_hash != last_hash:
            print(f"⬆️  Pushing: {rel_path}")
            res = api.upsert_note(note_id, f_path.name, content, parent_id)
            note_id = res.get("id")
            
            if note_id:
                state[rel_path] = {
                    "note_id": note_id,
                    "hash": file_hash,
                    "last_sync": int(_dt.datetime.now().timestamp() * 1000)
                }

    # Cleanup deleted files from Joplin
    for rel_path in list(state.keys()):
        if rel_path not in current_paths:
            print(f"🗑️  Removing from Joplin: {rel_path}")
            try:
                api.delete_note(state[rel_path]["note_id"])
            except: pass
            del state[rel_path]

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    print("Sync complete.")

if __name__ == "__main__":
    main()
