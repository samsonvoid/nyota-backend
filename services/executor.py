from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections import defaultdict

import psutil

# Import hybrid memory for dynamic learning
try:
    from services.hybrid_memory import hybrid_memory
except ImportError:
    hybrid_memory = None

DEFAULT_OUTPUT_LIMIT = 12_000
DEFAULT_TIMEOUT_SECONDS = 30
MAX_RETRIES = 2  # Maximum retries for same tool failure before asking for help

# Loop detection guard - track recent tool failures
_recent_failures: dict[str, list[float]] = defaultdict(list)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    requires_approval: bool


TOOLS = {
    "list_files": ToolDefinition(
        name="list_files",
        description="List files and folders inside an approved workspace.",
        requires_approval=False,
    ),
    "git_status": ToolDefinition(
        name="git_status",
        description="Read the current Git branch and working-tree status.",
        requires_approval=False,
    ),
    "read_file": ToolDefinition(
        name="read_file",
        description="Read a text file inside an approved workspace, excluding secrets.",
        requires_approval=False,
    ),
    "system_info": ToolDefinition(
        name="system_info",
        description="Read basic CPU, memory, disk, and Python runtime information.",
        requires_approval=False,
    ),
    "installed_tools": ToolDefinition(
        name="installed_tools",
        description="Check selected development tools available on the Windows PATH.",
        requires_approval=False,
    ),
    "search_file": ToolDefinition(
        name="search_file",
        description="Search for a filename inside an approved workspace only.",
        requires_approval=False,
    ),
    "workspace_info": ToolDefinition(
        name="workspace_info",
        description="Show the approved project workspaces and their top-level entries.",
        requires_approval=False,
    ),
    "frontend_build": ToolDefinition(
        name="frontend_build",
        description="Run the frontend production build in the approved Nyota workspace.",
        requires_approval=True,
    ),
    "launch_app": ToolDefinition(
        name="launch_app",
        description="Launch an allowed desktop application available on PATH.",
        requires_approval=True,  # Inahitaji ubonyeze YES/NO kiooni kabla ya kuanza
    ),
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _allowed_roots() -> list[Path]:
    configured = os.getenv("NYOTA_ALLOWED_WORKSPACES", "")
    raw_roots = [value.strip() for value in configured.split(",") if value.strip()]
    if raw_roots:
        return [Path(value).expanduser().resolve() for value in raw_roots]
    try:
        from services.memory import list_workspaces

        roots = [Path(item["path"]).expanduser().resolve() for item in list_workspaces()]
        if roots:
            return roots
    except (KeyError, OSError, TypeError, ValueError):
        pass
    return [_project_root()]


def _resolve_workspace(path: str | None) -> Path:
    candidate = Path(path or ".").expanduser().resolve()
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError("Workspace directory does not exist")

    for root in _allowed_roots():
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue

    raise PermissionError("Workspace is outside the approved workspace roots")


def _bounded_output(output: str, limit: int = DEFAULT_OUTPUT_LIMIT) -> str:
    if len(output) <= limit:
        return output
    return f"{output[:limit]}\n...[output truncated at {limit} characters]"


def _audit(tool: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
    audit_path = Path(os.getenv("NYOTA_AUDIT_LOG", _project_root() / "data" / "executor_audit.jsonl"))
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool,
            "arguments": arguments,
            "success": result["success"],
            "duration_ms": result["duration_ms"],
            "output": result["output"],
        }
        with audit_path.open("a", encoding="utf-8") as audit_file:
            audit_file.write(json.dumps(record, ensure_ascii=True) + "\n")
    except OSError as error:
        print(f"[EXECUTOR]: Could not write audit log: {error}")


def _finish(tool: str, arguments: dict[str, Any], started_at: float, success: bool, output: str) -> dict[str, Any]:
    result = {
        "success": success,
        "tool": tool,
        "output": _bounded_output(output.strip() or "Command completed without text output."),
        "duration_ms": round((time.perf_counter() - started_at) * 1000),
        "requires_approval": TOOLS[tool].requires_approval,
    }
    _audit(tool, arguments, result)
    
    # Track failures for loop detection
    failure_key = f"{tool}:{str(arguments)}"
    now = time.time()
    
    if not success:
        _recent_failures[failure_key].append(now)
        # Clean up old failures (older than 5 minutes)
        _recent_failures[failure_key] = [t for t in _recent_failures[failure_key] if now - t < 300]
        
        # Record failure in hybrid memory for learning
        if hybrid_memory and hybrid_memory.local_conn:
            try:
                error_msg = output[:500] if output else "Unknown error"
                # Run in thread to avoid blocking
                import threading
                threading.Thread(
                    target=lambda: hybrid_memory._execute_query(
                        "INSERT INTO tool_failures (tool_name, error_message, last_seen_at, occurrence_count) VALUES (%s, %s, now(), 1) ON CONFLICT (tool_name, error_message) DO UPDATE SET occurrence_count = tool_failures.occurrence_count + 1, last_seen_at = now()",
                        (tool, error_msg)
                    ),
                    daemon=True
                ).start()
            except Exception as e:
                print(f"[EXECUTOR]: Failed to record failure in hybrid memory: {e}")
    else:
        # Clear failures on success
        if failure_key in _recent_failures:
            del _recent_failures[failure_key]
    
    return result


def _check_loop_detection(tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    """Check if this tool+arguments combination is in a failure loop"""
    failure_key = f"{tool}:{str(arguments)}"
    now = time.time()
    
    # Clean up old failures
    _recent_failures[failure_key] = [t for t in _recent_failures[failure_key] if now - t < 300]
    
    recent_count = len(_recent_failures[failure_key])
    if recent_count >= MAX_RETRIES:
        return True, f"Samson, nimejaribu kutumia '{tool}' mara {recent_count} bila mafanikio. Unaweza kunielekeza njia sahihi au kunipa maelekezo jinsi ya kufanya hili?"
    
    return False, ""


def _get_learned_path(app_name: str) -> str | None:
    """Check hybrid memory for previously learned application path"""
    if not hybrid_memory or not hybrid_memory.local_conn:
        return None
    
    try:
        rows = hybrid_memory._execute_query(
            "SELECT resolved_path FROM learned_paths WHERE app_name = %s ORDER BY confidence_score DESC, last_used_at DESC LIMIT 1",
            (app_name,)
        )
        if rows and rows[0][0]:
            path = rows[0][0]
            # Update last_used_at
            hybrid_memory._execute_query(
                "UPDATE learned_paths SET last_used_at = now() WHERE app_name = %s AND resolved_path = %s",
                (app_name, path)
            )
            return path
    except Exception as e:
        print(f"[EXECUTOR]: Failed to get learned path: {e}")
    
    return None


def _learn_path(app_name: str, resolved_path: str, confidence: int = 1) -> None:
    """Store a learned application path in hybrid memory"""
    if not hybrid_memory or not hybrid_memory.local_conn:
        return
    
    try:
        hybrid_memory._execute_query(
            "INSERT INTO learned_paths (app_name, resolved_path, confidence_score, last_used_at) VALUES (%s, %s, %s, now()) ON CONFLICT (app_name, resolved_path) DO UPDATE SET confidence_score = GREATEST(learned_paths.confidence_score, %s), last_used_at = now()",
            (app_name, resolved_path, confidence, confidence)
        )
        print(f"[EXECUTOR]: Learned path for '{app_name}' -> {resolved_path}")
    except Exception as e:
        print(f"[EXECUTOR]: Failed to learn path: {e}")


def list_files(path: str | None = None, recursive: bool = False, limit: int = 200) -> dict[str, Any]:
    """List an approved workspace without executing a shell command."""
    started_at = time.perf_counter()
    arguments = {"path": path, "recursive": recursive, "limit": limit}
    try:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        workspace = _resolve_workspace(path)
        entries: list[str] = []
        iterator = workspace.rglob("*") if recursive else workspace.iterdir()
        for entry in iterator:
            if any(part in {".git", "node_modules", "__pycache__", ".venv"} for part in entry.parts):
                continue
            relative = entry.relative_to(workspace)
            entries.append(str(relative) + ("/" if entry.is_dir() else ""))
            if len(entries) >= limit:
                break
        entries.sort()
        return _finish("list_files", arguments, started_at, True, "\n".join(entries))
    except (OSError, PermissionError, ValueError) as error:
        return _finish("list_files", arguments, started_at, False, f"List files error: {error}")


def git_status(path: str | None = None) -> dict[str, Any]:
    """Read Git status for an approved workspace."""
    started_at = time.perf_counter()
    arguments = {"path": path}
    try:
        workspace = _resolve_workspace(path)
        completed = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
        output = completed.stdout or completed.stderr
        return _finish("git_status", arguments, started_at, completed.returncode == 0, output)
    except (OSError, PermissionError, subprocess.TimeoutExpired) as error:
        return _finish("git_status", arguments, started_at, False, f"Git status error: {error}")


def read_file(path: str) -> dict[str, Any]:
    """Read a bounded text file from an approved workspace without exposing secrets."""
    started_at = time.perf_counter()
    arguments = {"path": path}
    try:
        if not path:
            raise ValueError("File path is required")
        file_path = Path(path).expanduser().resolve()
        workspace = _resolve_workspace(str(file_path.parent))
        file_path.relative_to(workspace)
        if not file_path.is_file():
            raise ValueError("File does not exist")
        if file_path.name.lower() in {".env", ".env.local", "credentials.md"} or file_path.suffix.lower() in {".key", ".pem"}:
            raise PermissionError("Reading secret or credential files is not allowed")
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return _finish("read_file", arguments, started_at, True, content)
    except (OSError, PermissionError, ValueError) as error:
        return _finish("read_file", arguments, started_at, False, f"Read file error: {error}")


def system_info() -> dict[str, Any]:
    """Collect basic local machine metrics without executing a command."""
    started_at = time.perf_counter()
    arguments: dict[str, Any] = {}
    try:
        root_path = Path(_project_root().anchor or os.sep)
        info = {
            "platform": os.name,
            "python_version": f"{os.sys.version_info.major}.{os.sys.version_info.minor}.{os.sys.version_info.micro}",
            "cpu_count": psutil.cpu_count(logical=True),
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_percent": psutil.virtual_memory().percent,
            "memory_available_mb": round(psutil.virtual_memory().available / 1024 / 1024),
            "disk_percent": psutil.disk_usage(str(root_path)).percent,
        }
        return _finish("system_info", arguments, started_at, True, json.dumps(info, ensure_ascii=True))
    except (OSError, ValueError) as error:
        return _finish("system_info", arguments, started_at, False, f"System info error: {error}")


def installed_tools() -> dict[str, Any]:
    """Check development tools without scanning the whole Windows filesystem."""
    started_at = time.perf_counter()
    arguments: dict[str, Any] = {}
    try:
        tool_names = ["git", "node", "python", "php", "docker", "code", "ollama", "npm"]
        status = {
            tool: shutil.which(tool) or "Not installed / not in PATH"
            for tool in tool_names
        }
        return _finish("installed_tools", arguments, started_at, True, json.dumps(status, ensure_ascii=True))
    except OSError as error:
        return _finish("installed_tools", arguments, started_at, False, f"Installed tools error: {error}")


def search_file(name: str, path: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Find matching filenames under one approved workspace."""
    started_at = time.perf_counter()
    arguments = {"name": name, "path": path, "limit": limit}
    try:
        if not name or len(name) > 160:
            raise ValueError("A filename or pattern is required")
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        workspace = _resolve_workspace(path)
        matches: list[str] = []
        for entry in workspace.rglob(name):
            if any(part in {".git", "node_modules", "__pycache__", ".venv"} for part in entry.parts):
                continue
            matches.append(str(entry.relative_to(workspace)))
            if len(matches) >= limit:
                break
        matches.sort()
        output = "\n".join(matches) if matches else "No matching files found."
        return _finish("search_file", arguments, started_at, True, output)
    except (OSError, PermissionError, ValueError) as error:
        return _finish("search_file", arguments, started_at, False, f"File search error: {error}")


def workspace_info() -> dict[str, Any]:
    """Show selected workspaces without scanning outside their boundaries."""
    started_at = time.perf_counter()
    arguments: dict[str, Any] = {}
    try:
        workspaces = []
        for root in _allowed_roots():
            entries = sorted(
                entry.name
                for entry in root.iterdir()
                if entry.name not in {".git", "node_modules", "__pycache__", ".venv"}
            )[:100]
            workspaces.append({"path": str(root), "entries": entries})
        return _finish("workspace_info", arguments, started_at, True, json.dumps(workspaces, ensure_ascii=True))
    except (OSError, PermissionError) as error:
        return _finish("workspace_info", arguments, started_at, False, f"Workspace info error: {error}")


def frontend_build(path: str | None = None) -> dict[str, Any]:
    """Run the fixed frontend build command after approval."""
    started_at = time.perf_counter()
    arguments = {"path": path}
    try:
        workspace = _resolve_workspace(path)
        frontend_dir = workspace / "frontend"
        if not frontend_dir.is_dir():
            raise ValueError("Approved workspace does not contain a frontend directory")
        npm = "npm.cmd" if os.name == "nt" else "npm"
        completed = subprocess.run(
            [npm, "run", "build"],
            cwd=frontend_dir,
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
            check=False,
        )
        output = completed.stdout or completed.stderr
        return _finish("frontend_build", arguments, started_at, completed.returncode == 0, output)
    except (OSError, PermissionError, ValueError, subprocess.TimeoutExpired) as error:
        return _finish("frontend_build", arguments, started_at, False, f"Frontend build error: {error}")


def _find_windows_app(app_name: str) -> str | None:
    """Tafuta executable path kwenye PATH, App Paths za Registry, na Folders kuu za Windows."""
    # 0. First check hybrid memory for learned path
    learned_path = _get_learned_path(app_name)
    if learned_path and os.path.exists(learned_path):
        print(f"[EXECUTOR]: Using learned path for '{app_name}': {learned_path}")
        return learned_path

    # 1. Jaribu kupata kwenye PATH ya mfumo
    found_path = shutil.which(app_name)
    if found_path:
        return found_path

    # 2. Majina mbadala ya kawaida (Aliases)
    aliases = {
        "vscode": "code",
        "chrome": "chrome.exe",
        "browser": "chrome.exe",
        "notepad": "notepad.exe",
    }
    executable_name = aliases.get(app_name.lower(), app_name)
    if not executable_name.endswith(".exe"):
        executable_name += ".exe"

    # 3. Angalia kwenye Windows Registry (HKEY_LOCAL_MACHINE & HKEY_CURRENT_USER)
    if os.name == "nt":
        import winreg
        registry_paths = [
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}",
            rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}"
        ]
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for reg_path in registry_paths:
                try:
                    with winreg.OpenKey(root, reg_path) as key:
                        val, _ = winreg.QueryValueEx(key, "")
                        if val and os.path.exists(val):
                            return val
                except OSError:
                    continue

    # 4. Search kwenye Maeneo Makuu ya Windows (Standard Folders)
    possible_roots = [
        os.getenv("LOCALAPPDATA", ""),
        os.getenv("PROGRAMFILES", ""),
        os.getenv("PROGRAMFILES(X86)", ""),
        r"C:\Windows\System32",
    ]
    for root_dir in possible_roots:
        if not root_dir or not os.path.exists(root_dir):
            continue
        for dirpath, _, filenames in os.walk(root_dir):
            if dirpath.count(os.sep) - root_dir.count(os.sep) > 3:
                continue
            if executable_name.lower() in [f.lower() for f in filenames]:
                return os.path.join(dirpath, executable_name)

    return None


def launch_app(app_name: str) -> dict[str, Any]:
    """Launch an application dynamic lookup without hardcoding paths."""
    started_at = time.perf_counter()
    arguments = {"app_name": app_name}
    try:
        if not app_name:
            raise ValueError("Application name is required")

        target_path = _find_windows_app(app_name)

        if not target_path or not os.path.exists(target_path):
            raise ValueError(f"Could not locate '{app_name}' on this machine. Make sure it is installed.")

        subprocess.Popen([target_path], creationflags=subprocess.DETACHED_PROCESS if os.name == "nt" else 0)

        return _finish("launch_app", arguments, started_at, True, f"Successfully launched {app_name} from {target_path}.")
    except (OSError, ValueError) as error:
        return _finish("launch_app", arguments, started_at, False, f"Launch app error: {error}")


def execute_tool(
    tool: str,
    arguments: dict[str, Any] | None = None,
    approved: bool = False,
) -> dict[str, Any]:
    """Dispatch a validated tool request; arbitrary shell commands are unsupported."""
    arguments = arguments or {}
    if tool not in TOOLS:
        return {
            "success": False,
            "tool": tool,
            "output": f"Unknown or unsupported tool: {tool}",
            "duration_ms": 0,
            "requires_approval": False,
        }

    if TOOLS[tool].requires_approval and not approved:
        return {
            "success": False,
            "tool": tool,
            "output": "Approval required before this tool can execute.",
            "duration_ms": 0,
            "requires_approval": True,
        }

    # Check for loop detection before executing
    is_looping, loop_message = _check_loop_detection(tool, arguments)
    if is_looping:
        return {
            "success": False,
            "tool": tool,
            "output": loop_message,
            "duration_ms": 0,
            "requires_approval": TOOLS[tool].requires_approval,
            "is_loop_detected": True,
        }

    try:
        if tool == "list_files":
            return list_files(**arguments)
        if tool == "git_status":
            return git_status(**arguments)
        if tool == "read_file":
            return read_file(**arguments)
        if tool == "system_info":
            return system_info()
        if tool == "installed_tools":
            return installed_tools()
        if tool == "search_file":
            return search_file(**arguments)
        if tool == "workspace_info":
            return workspace_info()
        if tool == "frontend_build":
            return frontend_build(**arguments)
        if tool == "launch_app":
            result = launch_app(**arguments)
            # If successful, learn the path for future use
            if result["success"] and "target_path" in str(result.get("output", "")):
                # Extract path from output and learn it
                import re
                path_match = re.search(r'[A-Z]:\\[^"]+\.exe', result["output"])
                if path_match:
                    _learn_path(arguments.get("app_name", ""), path_match.group(0))
            return result
    except TypeError as error:
        return {
            "success": False,
            "tool": tool,
            "output": f"Invalid arguments: {error}",
            "duration_ms": 0,
            "requires_approval": TOOLS[tool].requires_approval,
        }
    raise RuntimeError(f"Tool registry is missing an implementation for {tool}")


def available_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": definition.name,
            "description": definition.description,
            "requires_approval": definition.requires_approval,
        }
        for definition in TOOLS.values()
    ]