from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections import defaultdict

import psutil
import winreg

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
    "installed_apps": ToolDefinition(
        name="installed_apps",
        description="List installed desktop applications and programs on this Windows PC.",
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
        description="Launch a desktop application like msedge, chrome, code, notepad, or calc.",
        requires_approval=False,
    ),
    "close_app": ToolDefinition(
        name="close_app",
        description="Close a running desktop application like msedge, chrome, code, notepad, or calc.",
        requires_approval=False,
    ),
    "close_window": ToolDefinition(
        name="close_window",
        description="Close the currently active window or a specified application window.",
        requires_approval=False,
    ),
    "write_file": ToolDefinition(
        name="write_file",
        description="Write or create a script or text file inside an approved workspace.",
        requires_approval=False,
    ),
    "run_command": ToolDefinition(
        name="run_command",
        description="Execute a shell command or script on Windows. Requires approval before running.",
        requires_approval=True,
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
        result = _finish("read_file", arguments, started_at, True, content)
        result["file_path"] = str(file_path)
        result["filename"] = file_path.name
        return result
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
        installed = [t for t in tool_names if shutil.which(t)]
        missing = [t for t in tool_names if not shutil.which(t)]
        
        display_names = {
            "git": "Git", "node": "Node.js", "python": "Python",
            "php": "PHP", "docker": "Docker", "code": "VS Code",
            "ollama": "Ollama", "npm": "NPM"
        }
        installed_str = ", ".join(display_names.get(t, t) for t in installed)
        output = f"Available tools found on your PC: {installed_str}."
        if missing:
            missing_str = ", ".join(display_names.get(t, t) for t in missing)
            output += f" Not detected: {missing_str}."
        return _finish("installed_tools", arguments, started_at, True, output)
    except OSError as error:
        return _finish("installed_tools", arguments, started_at, False, f"Installed tools error: {error}")


def installed_apps() -> dict[str, Any]:
    """List installed user-facing desktop applications on this Windows PC without repeating names."""
    started_at = time.perf_counter()
    arguments: dict[str, Any] = {}
    try:
        common_priority = [
            ("Google Chrome", ["chrome"]),
            ("Visual Studio Code", ["code", "vscode"]),
            ("Microsoft Edge", ["msedge", "edge"]),
            ("Mozilla Firefox", ["firefox"]),
            ("Brave Browser", ["brave"]),
            ("Microsoft Store", ["microsoft store", "store"]),
            ("Notepad", ["notepad"]),
            ("Command Prompt", ["cmd"]),
            ("Windows PowerShell", ["powershell"]),
            ("Windows Terminal", ["terminal", "wt"]),
            ("Calculator", ["calc", "calculator"]),
            ("VLC Media Player", ["vlc"]),
            ("Spotify", ["spotify"]),
            ("Discord", ["discord"]),
            ("Telegram", ["telegram"]),
            ("WhatsApp", ["whatsapp"]),
            ("Microsoft Word", ["word", "winword"]),
            ("Microsoft Excel", ["excel"]),
            ("Microsoft PowerPoint", ["powerpoint", "powerpnt"]),
            ("MS Paint", ["paint", "mspaint"]),
            ("File Explorer", ["explorer"]),
            ("Burp Suite", ["burp", "burpsuite"]),
            ("Antigravity", ["antigravity"]),
            ("Settings", ["settings"]),
            ("Camera", ["camera"]),
            ("Photos", ["photos"]),
        ]

        found_names: list[str] = []
        for display_name, keys in common_priority:
            for k in keys:
                if k in DYNAMIC_APP_MAP or k in UWP_APPS or shutil.which(k):
                    if display_name not in found_names:
                        found_names.append(display_name)
                    break

        # Also add any clean discovered apps from DYNAMIC_APP_MAP
        garbage = {"17.0.17+10", "add", "addin", "additional", "address", "application", "audio", "bang", "bootstrap", "build", "changer", "cli", "clicktorun", "c++"}
        for k in sorted(DYNAMIC_APP_MAP.keys()):
            if len(k) >= 4 and k not in garbage and not any(k in keys for _, keys in common_priority):
                clean_title = k.replace("_", " ").replace("-", " ").title()
                if clean_title not in found_names and len(found_names) < 30:
                    found_names.append(clean_title)

        summary = f"Installed applications found on your PC ({len(found_names)}): {', '.join(found_names)}."
        return _finish("installed_apps", arguments, started_at, True, summary)
    except Exception as error:
        return _finish("installed_apps", arguments, started_at, False, f"Installed apps error: {error}")


def search_file(name: str = "", path: str | None = None, limit: int = 50, **kwargs: Any) -> dict[str, Any]:
    """Find matching filenames under one approved workspace."""
    started_at = time.perf_counter()
    search_query = (
        name
        or kwargs.get("file_name")
        or kwargs.get("filename")
        or kwargs.get("query")
        or kwargs.get("pattern")
        or kwargs.get("search_term")
        or ""
    ).strip()
    arguments = {"name": search_query, "path": path, "limit": limit}
    try:
        if not search_query:
            raise ValueError("A filename or search query is required")
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        workspace = _resolve_workspace(path)
        matches: list[str] = []
        lowered_query = search_query.lower()
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}]
            for fn in files:
                if lowered_query in fn.lower():
                    rel = os.path.relpath(os.path.join(root, fn), workspace)
                    matches.append(rel)
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break
        matches.sort()
        if matches:
            top_names = [os.path.basename(m) for m in matches[:5]]
            output = f"Found {len(matches)} matching file(s): {', '.join(top_names)}."
            if len(matches) > 5:
                output += f" and {len(matches) - 5} more."
        else:
            output = f"No matching files found for '{search_query}' in the workspace."
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


def write_file(path: str, content: str, **kwargs: Any) -> dict[str, Any]:
    """Write or create a script/text file inside an approved workspace."""
    started_at = time.perf_counter()
    # Accept common argument aliases from Ollama/Gemini
    path = path or kwargs.get("file_path") or kwargs.get("filename") or kwargs.get("name") or ""
    content = content or kwargs.get("code") or kwargs.get("text") or kwargs.get("body") or ""
    arguments = {"path": path, "content_length": len(content)}
    try:
        if not path:
            raise ValueError("A file path is required")
        if not content:
            raise ValueError("Content to write cannot be empty")

        file_path = Path(path).expanduser()
        # If not absolute, place it in the first allowed workspace root
        if not file_path.is_absolute():
            file_path = _allowed_roots()[0] / file_path

        file_path = file_path.resolve()
        # Validate against allowed workspace roots
        allowed = False
        for root in _allowed_roots():
            try:
                file_path.relative_to(root)
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            raise PermissionError(f"Path '{file_path}' is outside approved workspace roots")

        # Block overwriting secret files
        if file_path.name.lower() in {".env", ".env.local", "credentials.md"} or file_path.suffix.lower() in {".key", ".pem"}:
            raise PermissionError("Writing to secret or credential files is not allowed")

        # Create parent directories if needed
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

        short_name = file_path.name
        lines = content.count("\n") + 1
        result = _finish("write_file", arguments, started_at, True,
                         f"Successfully wrote {lines} line(s) to '{short_name}'. File saved at: {file_path}")
        result["file_path"] = str(file_path)
        result["filename"] = short_name
        return result
    except (OSError, PermissionError, ValueError) as error:
        return _finish("write_file", arguments, started_at, False, f"Write file error: {error}")


def run_command(command: str, path: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Execute a shell command on Windows after approval. Requires requires_approval=True."""
    started_at = time.perf_counter()
    command = command or kwargs.get("cmd") or kwargs.get("shell") or kwargs.get("script") or ""
    arguments = {"command": command, "path": path}
    try:
        if not command:
            raise ValueError("A command string is required")
        if len(command) > 500:
            raise ValueError("Command is too long (max 500 chars)")

        # Resolve optional working directory
        cwd = None
        if path:
            try:
                cwd = str(_resolve_workspace(path))
            except (ValueError, PermissionError):
                cwd = None

        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            cwd=cwd,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        success = completed.returncode == 0
        return _finish("run_command", arguments, started_at, success,
                       output or f"Command exited with code {completed.returncode}")
    except subprocess.TimeoutExpired:
        return _finish("run_command", arguments, started_at, False, "Command timed out after 30 seconds")
    except (OSError, PermissionError, ValueError) as error:
        return _finish("run_command", arguments, started_at, False, f"Run command error: {error}")


APP_ALIASES: dict[str, list[str]] = {
    "vscode": ["code.cmd", "code.exe", "Code.exe"],
    "code": ["code.cmd", "code.exe", "Code.exe"],
    "visual studio code": ["code.cmd", "code.exe", "Code.exe"],
    "vs code": ["code.cmd", "code.exe", "Code.exe"],
    "chrome": ["chrome.exe"],
    "google chrome": ["chrome.exe"],
    "browser": ["msedge.exe", "chrome.exe", "brave.exe", "firefox.exe"],
    "msedge": ["msedge.exe"],
    "edge": ["msedge.exe"],
    "microsoft edge": ["msedge.exe"],
    "brave": ["brave.exe"],
    "firefox": ["firefox.exe"],
    "notepad": ["notepad.exe"],
    "notipadi": ["notepad.exe"],
    "calculator": ["calc.exe"],
    "kikokotoo": ["calc.exe"],
    "calc": ["calc.exe"],
    "word": ["WINWORD.EXE", "winword.exe"],
    "ms word": ["WINWORD.EXE", "winword.exe"],
    "microsoft word": ["WINWORD.EXE", "winword.exe"],
    "excel": ["EXCEL.EXE", "excel.exe"],
    "ms excel": ["EXCEL.EXE", "excel.exe"],
    "microsoft excel": ["EXCEL.EXE", "excel.exe"],
    "powerpoint": ["POWERPNT.EXE", "powerpnt.exe"],
    "ppt": ["POWERPNT.EXE", "powerpnt.exe"],
    "spotify": [
        "Spotify.exe",
        "spotify.exe",
        os.path.join(os.getenv("APPDATA", ""), r"Spotify\Spotify.exe"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), r"Microsoft\WindowsApps\Spotify.exe"),
    ],
    "discord": ["Discord.exe", "discord.exe"],
    "telegram": ["Telegram.exe", "telegram.exe"],
    "whatsapp": ["WhatsApp.exe", "whatsapp.exe"],
    "vlc": ["vlc.exe"],
    "paint": ["mspaint.exe"],
    "explorer": ["explorer.exe"],
    "file explorer": ["explorer.exe"],
    "terminal": ["wt.exe", "powershell.exe", "cmd.exe"],
    "cmd": ["cmd.exe"],
    "command prompt": ["cmd.exe"],
    "powershell": ["powershell.exe"],
}


def _clean_registry_path(raw_val: str) -> str | None:
    if not raw_val or not isinstance(raw_val, str):
        return None
    cleaned = raw_val.strip().strip('"')
    if os.path.exists(cleaned):
        return cleaned
    if ".exe" in cleaned.lower():
        idx = cleaned.lower().find(".exe") + 4
        exe_path = cleaned[:idx].strip().strip('"')
        if os.path.exists(exe_path):
            return exe_path
    return None


def _find_start_menu_shortcut(app_name: str) -> str | None:
    if os.name != "nt":
        return None
    start_menu_dirs = [
        os.path.join(os.getenv("PROGRAMDATA", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.getenv("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    query_clean = app_name.lower().replace(" ", "")
    for start_dir in start_menu_dirs:
        if not start_dir or not os.path.exists(start_dir):
            continue
        for root_dir, dirnames, filenames in os.walk(start_dir):
            for fn in filenames:
                if fn.lower().endswith(".lnk"):
                    shortcut_name = fn[:-4].lower().replace(" ", "")
                    if query_clean in shortcut_name or shortcut_name in query_clean:
                        return os.path.join(root_dir, fn)
    return None


UWP_APPS: dict[str, str] = {
    "camera": "microsoft.windows.camera:",
    "webcam": "microsoft.windows.camera:",
    "picha": "microsoft.windows.camera:",
    "settings": "ms-settings:",
    "windows settings": "ms-settings:",
    "store": "ms-windows-store:",
    "microsoft store": "ms-windows-store:",
    "windows store": "ms-windows-store:",
    "duka": "ms-windows-store:",          # Swahili for store
    "photos": "ms-photos:",
    "picha gallery": "ms-photos:",
    "clock": "ms-clock:",
    "alarm": "ms-clock:",
    "alarms": "ms-clock:",
    "maps": "bingmaps:",
    "weather": "bingweather:",
    "news": "bingnews:",
    "calendar": "outlookcal:",
    "mail": "outlookmail:",
    "calculator app": "ms-calculator:",
}


def scan_installed_apps() -> dict[str, list[str]]:
    """Quick scan for installed apps via registry + PATH. Cached to JSON for speed."""
    import json, time
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_path = os.path.join(base_dir, ".app_discovery_cache.json")
    cache_age = 3600

    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            if time.time() - cached.get("_timestamp", 0) < cache_age:
                app_map = {k: v for k, v in cached.items() if k != "_timestamp"}
                print(f"[APP DISCOVERY]: Loaded {len(app_map)} apps from cache")
                return app_map
        except Exception:
            pass

    app_map: dict[str, list[str]] = {}
    GARBAGE = lambda k: k.startswith("(") or "x64" in k or len(k) <= 2 or re.match(r"^[0-9][0-9.]*$", k) or "." in k and k.replace(".", "").isdigit()

    def add_app(key: str, exe_path: str):
        k = key.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
        if len(k) < 2 or GARBAGE(k):
            return
        if k not in app_map or not app_map[k]:
            app_map.setdefault(k, [])
            if exe_path not in app_map[k]:
                app_map[k].append(exe_path)

    if os.name == "nt":
        reg_paths = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ]
        for root in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
            for reg_path in reg_paths:
                try:
                    with winreg.OpenKey(root, reg_path) as key:
                        idx = 0
                        while True:
                            try:
                                subkey_name = winreg.EnumKey(key, idx)
                                idx += 1
                                with winreg.OpenKey(root, f"{reg_path}\\{subkey_name}") as subkey:
                                    try:
                                        dn = winreg.QueryValueEx(subkey, "DisplayName")[0]
                                    except OSError:
                                        continue
                                    try:
                                        loc = winreg.QueryValueEx(subkey, "InstallLocation")[0]
                                    except OSError:
                                        loc = ""
                                    if not isinstance(dn, str) or not dn.strip():
                                        continue
                                    for word in dn.strip().split():
                                        w = word.strip(".,_-").lower()
                                        if len(w) >= 2 and not GARBAGE(w):
                                            add_app(w, w)
                                    if loc and os.path.isdir(loc):
                                        for f in os.listdir(loc):
                                            if any(f.lower().endswith(e) for e in (".exe", ".cmd")):
                                                add_app(dn.strip(), os.path.join(loc, f))
                                                break
                            except OSError:
                                break
                except OSError:
                    pass

    known_apps = ["chrome", "msedge", "edge", "firefox", "brave", "vscode", "code",
                   "notepad", "calc", "winword", "excel", "powerpnt", "paint", "mspaint",
                   "vlc", "spotify", "discord", "telegram", "whatsapp", "teams", "slack",
                   "zoom", "skype", "itunes", "onenote", "obs", "ffmpeg", "git", "node",
                   "python", "javaw", "outlook", "onedrive", "explorer", "cmd",
                   "powershell", "terminal", "wt", "access", "publisher"]
    # Also add msedge explicitly since it's not in PATH
    msedge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for mp in msedge_paths:
        if os.path.exists(mp):
            add_app("msedge", mp)
            add_app("edge", mp)
            break

    for app in known_apps:
        exe = shutil.which(app) or shutil.which(f"{app}.exe") or shutil.which(f"{app}.cmd")
        if exe and os.path.exists(exe):
            add_app(app.lower(), exe)
        else:
            add_app(app.lower(), app)

    try:
        to_save = {**app_map, "_timestamp": time.time()}
        with open(cache_path, "w") as f:
            json.dump(to_save, f)
    except Exception:
        pass

    print(f"[APP DISCOVERY]: Found {len(app_map)} apps on this PC")
    return app_map


DYNAMIC_APP_MAP: dict[str, list[str]] = scan_installed_apps()


def _find_windows_app(app_name: str) -> str | None:
    """Tafuta executable au shortcut path au UWP protocol kwa njia ya haraka na ya uhakika."""
    clean_name = app_name.strip().lower()

    # 0. Check UWP protocol schemes first (e.g. camera, settings)
    if clean_name in UWP_APPS:
        return UWP_APPS[clean_name]

    # 1. Check hybrid memory for previously learned path
    learned_path = _get_learned_path(clean_name)
    if learned_path and os.path.exists(learned_path):
        print(f"[EXECUTOR]: Using learned path for '{clean_name}': {learned_path}")
        return learned_path

    # Candidate binary names from alias mapping or raw input
    candidates = APP_ALIASES.get(clean_name, [])
    if not candidates:
        candidate_exe = clean_name if clean_name.endswith(".exe") else f"{clean_name}.exe"
        candidates = [clean_name, candidate_exe]

    # 2. PATH search or direct path check
    for candidate in candidates:
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
        found_path = shutil.which(candidate)
        if found_path:
            return found_path

    # 3. Windows Registry App Paths lookup
    if os.name == "nt":
        import winreg
        for candidate in candidates:
            exe_key = candidate if candidate.endswith(".exe") else f"{candidate}.exe"
            registry_paths = [
                rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_key}",
                rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{exe_key}",
            ]
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for reg_path in registry_paths:
                    try:
                        with winreg.OpenKey(root, reg_path) as key:
                            val, _ = winreg.QueryValueEx(key, "")
                            resolved = _clean_registry_path(val)
                            if resolved:
                                return resolved
                    except OSError:
                        continue

    # 4. Start Menu Shortcuts (.lnk) lookup
    shortcut = _find_start_menu_shortcut(clean_name)
    if shortcut:
        return shortcut

    # 5. Shallow search in target Windows program directories (max depth 3 with pruning)
    local_appdata = os.getenv("LOCALAPPDATA", "")
    possible_roots = [
        os.path.join(local_appdata, "Programs") if local_appdata else "",
        os.getenv("PROGRAMFILES", r"C:\Program Files"),
        os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        r"C:\Windows\System32",
        r"C:\Windows",
    ]
    candidate_lowers = set(c.lower() for c in candidates)
    for root_dir in possible_roots:
        if not root_dir or not os.path.exists(root_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir):
            depth = dirpath.count(os.sep) - root_dir.count(os.sep)
            if depth >= 3:
                dirnames.clear()
            for fn in filenames:
                if fn.lower() in candidate_lowers:
                    return os.path.join(dirpath, fn)

    return None


def launch_app(app_name: str = "", **kwargs: Any) -> dict[str, Any]:
    """Launch an application, website, or UWP app with clean natural speech response."""
    started_at = time.perf_counter()
    target_app = (app_name or kwargs.get("name") or kwargs.get("target") or kwargs.get("app") or "").strip()
    arguments = {"app_name": target_app}
    try:
        if not target_app:
            raise ValueError("Application name is required")

        clean_lower = target_app.lower()

        # 1. URL / Website opening support
        if clean_lower.startswith(("http://", "https://", "www.")) or any(clean_lower.endswith(ext) for ext in [".com", ".org", ".net", ".io", ".dev", ".app", ".co"]):
            import webbrowser
            url = target_app if target_app.startswith("http") else f"https://{target_app}"
            webbrowser.open(url)
            return _finish("launch_app", arguments, started_at, True, f"Opening {clean_lower} in your browser.")

        # 2. Music / Media fallback (if Spotify not installed, open YouTube Music)
        if clean_lower in {"spotify", "music", "song", "songs", "muziki", "media"}:
            found_music = _find_windows_app("spotify")
            if not found_music:
                import webbrowser
                webbrowser.open("https://music.youtube.com")
                return _finish("launch_app", arguments, started_at, True, "Opening YouTube Music in your browser.")

        target_path = _find_windows_app(target_app)
        if not target_path:
            raise ValueError(f"Could not locate '{target_app}' on this machine. Make sure it is installed.")

        # Format clean display name for voice
        display_names = {
            "vscode": "VS Code", "code": "VS Code", "visual studio code": "VS Code",
            "msedge": "Microsoft Edge", "edge": "Microsoft Edge",
            "chrome": "Google Chrome", "calc": "Calculator", "calculator": "Calculator",
            "notepad": "Notepad", "camera": "Camera", "webcam": "Camera",
            "settings": "Settings", "word": "Word", "excel": "Excel", "spotify": "Spotify",
            "vlc": "VLC Media Player", "music": "Music"
        }
        app_display = display_names.get(clean_lower, target_app.title())

        # Handle UWP Protocol URI (e.g. microsoft.windows.camera:, ms-settings:)
        if target_path.endswith(":") or target_path.startswith(("microsoft.", "ms-")):
            if os.name == "nt":
                try:
                    os.startfile(target_path)
                except Exception:
                    subprocess.Popen(f"start {target_path}", shell=True)
            return _finish("launch_app", arguments, started_at, True, f"Opening {app_display}.")

        if not os.path.exists(target_path):
            raise ValueError(f"Could not locate '{target_app}' on this machine. Make sure it is installed.")

        if os.name == "nt":
            try:
                os.startfile(target_path)
            except Exception:
                subprocess.Popen(target_path, shell=True)
        else:
            subprocess.Popen([target_path])

        return _finish("launch_app", arguments, started_at, True, f"Opening {app_display}.")
    except (OSError, ValueError) as error:
        return _finish("launch_app", arguments, started_at, False, f"Could not launch {target_app}. {error}")


def close_window(app_name: str = "", **kwargs: Any) -> dict[str, Any]:
    """Close the active foreground window or a specified application window."""
    started_at = time.perf_counter()
    target = (app_name or kwargs.get("window_name") or kwargs.get("name") or kwargs.get("target") or "").strip()
    arguments = {"app_name": target}

    if target and target.lower() not in {"window", "the window", "active window", "current window", "dirisha"}:
        return close_app(target)

    try:
        if os.name == "nt":
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                length = user32.GetWindowTextLengthW(hwnd)
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value.strip() or "active window"
                WM_CLOSE = 0x0010
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return _finish("close_window", arguments, started_at, True, f"Closed {title}.")
            else:
                return _finish("close_window", arguments, started_at, False, "No active window detected to close.")
        else:
            return _finish("close_window", arguments, started_at, False, "Window closing is only supported on Windows.")
    except Exception as error:
        return _finish("close_window", arguments, started_at, False, f"Could not close active window: {error}")


def close_app(app_name: str = "", **kwargs: Any) -> dict[str, Any]:
    """Close a running application using psutil to find and terminate its processes."""
    started_at = time.perf_counter()
    target_app = (app_name or kwargs.get("name") or kwargs.get("target") or kwargs.get("app") or "").strip()
    arguments = {"app_name": target_app}
    try:
        if not target_app:
            return close_window()

        clean_name = target_app.strip().lower()
        if clean_name in {"window", "the window", "active window", "current window", "dirisha"}:
            return close_window()
        
        # Map app names to process names
        process_names = {
            "chrome": "chrome.exe",
            "google chrome": "chrome.exe",
            "msedge": "msedge.exe",
            "edge": "msedge.exe",
            "microsoft edge": "msedge.exe",
            "firefox": "firefox.exe",
            "brave": "brave.exe",
            "vscode": "Code.exe",
            "code": "Code.exe",
            "visual studio code": "Code.exe",
            "vs code": "Code.exe",
            "notepad": "notepad.exe",
            "calculator": "calc.exe",
            "calc": "calc.exe",
            "word": "WINWORD.EXE",
            "excel": "EXCEL.EXE",
            "powerpoint": "POWERPNT.EXE",
            "spotify": "Spotify.exe",
            "discord": "Discord.exe",
            "telegram": "Telegram.exe",
            "whatsapp": "WhatsApp.exe",
            "vlc": "vlc.exe",
            "cmd": "cmd.exe",
        }

        # Get process name from mapping or use the input
        target_process = process_names.get(clean_name)
        if not target_process:
            target_process = clean_name if clean_name.endswith(".exe") else f"{clean_name}.exe"

        terminated_count = 0
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if proc.info['name'] and proc.info['name'].lower() == target_process.lower():
                    proc.terminate()
                    terminated_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if terminated_count > 0:
            display_names = {
                "vscode": "VS Code", "code": "VS Code", "visual studio code": "VS Code",
                "msedge": "Microsoft Edge", "edge": "Microsoft Edge",
                "chrome": "Google Chrome", "calc": "Calculator", "calculator": "Calculator",
                "notepad": "Notepad", "word": "Word", "excel": "Excel", "spotify": "Spotify",
                "cmd": "Command Prompt"
            }
            app_display = display_names.get(clean_name, target_app.title())
            return _finish("close_app", arguments, started_at, True, f"Closed {app_display}.")
        else:
            return _finish("close_app", arguments, started_at, False, f"No running instances of '{target_app}' found to close.")
    except Exception as error:
        return _finish("close_app", arguments, started_at, False, f"Could not close {target_app}. {error}")


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
        if tool == "installed_apps":
            return installed_apps()
        if tool == "search_file":
            query_name = (
                arguments.get("name")
                or arguments.get("file_name")
                or arguments.get("filename")
                or arguments.get("query")
                or arguments.get("pattern")
                or arguments.get("search_term")
                or ""
            )
            target_path = arguments.get("path")
            limit = arguments.get("limit", 50)
            return search_file(name=query_name, path=target_path, limit=limit)
        if tool == "workspace_info":
            return workspace_info()
        if tool == "frontend_build":
            return frontend_build(**arguments)
        if tool == "launch_app":
            app = arguments.get("app_name") or arguments.get("name") or arguments.get("target") or ""
            result = launch_app(app_name=app)
            if result["success"] and "target_path" in str(result.get("output", "")):
                path_match = re.search(r'[A-Z]:\\[^"]+\.exe', result["output"])
                if path_match:
                    _learn_path(app, path_match.group(0))
            return result
        if tool == "close_window":
            return close_window(**arguments)
        if tool == "close_app":
            return close_app(**arguments)
        if tool == "write_file":
            file_path = arguments.get("path") or arguments.get("file_path") or arguments.get("filename") or arguments.get("name") or ""
            content = arguments.get("content") or arguments.get("code") or arguments.get("text") or arguments.get("body") or ""
            return write_file(path=file_path, content=content, **{k: v for k, v in arguments.items() if k not in ("path", "content")})
        if tool == "run_command":
            cmd = arguments.get("command") or arguments.get("cmd") or arguments.get("shell") or arguments.get("script") or ""
            cwd = arguments.get("path")
            return run_command(command=cmd, path=cwd)
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