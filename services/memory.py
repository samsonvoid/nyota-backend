from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


_MAX_CONTEXT_CHARS = 6_000
_memory_lock = threading.Lock()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _memory_path() -> Path:
    configured = os.getenv("NYOTA_MEMORY_FILE")
    return Path(configured).expanduser().resolve() if configured else _project_root() / "data" / "nyota_memory.json"


def _default_memory() -> dict[str, Any]:
    root = _project_root()
    configured_workspaces = [
        Path(value.strip()).expanduser().resolve()
        for value in os.getenv("NYOTA_ALLOWED_WORKSPACES", "").split(",")
        if value.strip()
    ]
    workspace_paths = configured_workspaces or [root]
    return {
        "version": 1,
        "profile": {
            "assistant_name": "Nyota Assistant",
            "owner_name": "Samson",
            "preferred_language": "en",
        },
        "workspaces": [
            {
                "name": workspace.name,
                "path": str(workspace),
                "purpose": "Selected Nyota project workspace",
            }
            for workspace in workspace_paths
        ],
        "verified_facts": [
            "The backend uses FastAPI and runs locally on Windows.",
            "The frontend uses React, TypeScript, and Vite.",
            "Local AI uses Ollama when the service is available.",
        ],
        "preferences": [],
    }


def _validate_memory(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Memory root must be an object")

    memory = _default_memory()
    profile = value.get("profile")
    if isinstance(profile, dict):
        for key in ("assistant_name", "owner_name", "preferred_language"):
            if isinstance(profile.get(key), str) and profile[key].strip():
                memory["profile"][key] = profile[key].strip()[:120]

    for key in ("workspaces", "verified_facts", "preferences"):
        items = value.get(key)
        if isinstance(items, list):
            memory[key] = items[:100]

    return memory


def load_memory() -> dict[str, Any]:
    path = _memory_path()
    try:
        with _memory_lock:
            raw = json.loads(path.read_text(encoding="utf-8"))
        return _validate_memory(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return _default_memory()


def memory_context() -> str:
    """Return bounded, non-secret context for model prompting."""
    memory = load_memory()
    context = json.dumps(memory, ensure_ascii=True, indent=2)
    return context[:_MAX_CONTEXT_CHARS]


def save_memory(memory: dict[str, Any]) -> None:
    """Save explicitly curated memory; callers must not pass secrets or raw chat history."""
    validated = _validate_memory(memory)
    path = _memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    with _memory_lock:
        temporary_path.write_text(
            json.dumps(validated, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)


def initialize_memory() -> None:
    path = _memory_path()
    if path.exists():
        return
    save_memory(_default_memory())


def list_workspaces() -> list[dict[str, str]]:
    return [
        workspace
        for workspace in load_memory().get("workspaces", [])
        if isinstance(workspace, dict)
        and isinstance(workspace.get("name"), str)
        and isinstance(workspace.get("path"), str)
    ]


def add_workspace(path: str, name: str | None = None, purpose: str | None = None) -> dict[str, str]:
    candidate = Path(path).expanduser().resolve()
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError("Workspace directory does not exist")
    if candidate.anchor and candidate == Path(candidate.anchor):
        raise ValueError("A drive root cannot be added as a workspace")
    if candidate.name.lower() in {"windows", "program files", "users"}:
        raise ValueError("This system directory cannot be added as a workspace")

    memory = load_memory()
    workspaces = list_workspaces()
    if any(Path(item["path"]).resolve() == candidate for item in workspaces):
        raise ValueError("Workspace is already registered")
    workspace = {
        "name": (name or candidate.name).strip()[:120],
        "path": str(candidate),
        "purpose": (purpose or "Selected project workspace").strip()[:240],
    }
    memory["workspaces"] = workspaces + [workspace]
    save_memory(memory)
    return workspace


def remove_workspace(path: str) -> None:
    candidate = Path(path).expanduser().resolve()
    memory = load_memory()
    remaining = [
        workspace
        for workspace in list_workspaces()
        if Path(workspace["path"]).resolve() != candidate
    ]
    if len(remaining) == len(list_workspaces()):
        raise ValueError("Workspace is not registered")
    if not remaining:
        raise ValueError("At least one workspace must remain registered")
    memory["workspaces"] = remaining
    save_memory(memory)
