import os
import json
import re
import tempfile
import asyncio
import time
import ollama
import pyaudio
import threading
from typing import Any
from dataclasses import dataclass
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from middleware.security import validate_api_key
from middleware.rate_limit import limiter
from services.gemini_service import model, query_nyota, parse_local_command
from services.executor import available_tools, execute_tool
from services.approvals import approval_store
from services.memory import memory_context
from services.tts import nyota_tts, speak_async

router = APIRouter(prefix="/api", tags=["chat"])

tts_engine = nyota_tts

def check_mode_change(text: str) -> str | None:
    text_lower = text.lower().strip()
    natural_keywords = [
        "switch to premium voice", "use premium voice", "premium voice", "switch to premium",
        "switch to natural voice", "use natural voice", "natural voice", "switch to natural",
        "use neural voice", "switch to neural", "neural voice", "speak in natural mode", "speak in premium mode",
        "badili sauti kuwa ya asili", "tumia sauti ya asili", "sauti ya asili"
    ]
    fast_keywords = [
        "switch to fast voice", "use fast voice", "fast voice", "switch to fast",
        "switch to standard voice", "use standard voice", "standard voice", "switch to standard",
        "switch to sapi5", "use sapi5", "sapi5 voice", "speak in fast mode", "speak in standard mode",
        "badili sauti kuwa ya haraka", "tumia sauti ya haraka", "sauti ya haraka"
    ]
    if any(k in text_lower for k in natural_keywords):
        return tts_engine.set_mode("natural")
    elif any(k in text_lower for k in fast_keywords):
        return tts_engine.set_mode("fast")
    return None


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    conversation_id: str | None = None
    language: str | None = "en"


class ChatResponse(BaseModel):
    reply: str
    conversation_id: str
    approval_id: str | None = None
    approval_status: str | None = None


class VoiceChatRequest(BaseModel):
    conversation_id: str | None = None
    language: str | None = "en"


class VoiceChatResponse(BaseModel):
    user_query: str
    reply: str
    conversation_id: str
    is_silence: bool = False
    is_rate_limited: bool = False
    approval_id: str | None = None
    approval_status: str | None = None


@dataclass(frozen=True)
class LocalQueryResult:
    reply: str
    approval_id: str | None = None

def _extract_launch_app(message: str) -> str | None:
    """Detect direct app launch requests like 'open msedge', 'please camera for me', 'launch chrome', 'fungua vscode', 'play music'."""
    clean = message.strip().lower()
    clean = re.sub(r'[?!.,;:_#@*()\-+]', ' ', clean).strip()

    # Strip conversational prefixes/suffixes
    clean = re.sub(r'^(?:please|can you|could you|help me|kindly|tafadhali|naomba)\s+', '', clean).strip()
    clean = re.sub(r'\s+(?:for me|please|now|haraka|sasa)$', '', clean).strip()

    if clean in {"camera", "the camera", "webcam", "video camera", "kamera"}:
        return "camera"

    if clean in {"play music", "play some music", "play song", "play songs", "cheza muziki", "open music", "open spotify", "music", "spotify"}:
        return "music"

    match = re.match(
        r'^(?:launch|open|start|run|fire up|fungua|washa|anzisha|play)\s+([a-zA-Z0-9_\- ]+)$',
        clean
    )
    if match:
        target = match.group(1).strip()
        non_apps = {"a file", "the door", "project", "folder", "browser", "internet"}
        if target in {"browser", "internet"}:
            return "msedge"
        if target in {"folder", "project"}:
            return "explorer"
        if target in {"music", "song", "songs", "muziki"}:
            return "music"
        if target in {"camera", "the camera", "webcam", "video camera", "kamera"}:
            return "camera"
        if target and len(target.split()) <= 3 and target not in non_apps:
            return target
    return None


def _extract_close_app(message: str) -> tuple[str, dict[str, Any]] | None:
    """Detect direct app/window close requests like 'close window', 'close edge', 'close camera', 'funga chrome'."""
    clean = message.strip().lower()
    clean = re.sub(r'[?!.,;:_#@*()\-+]', ' ', clean).strip()

    # Check for PID specification e.g. "PID of 13588" or "pid 13588"
    pid_match = re.search(r'\b(?:pid|process id)\s*(?:of|is|:)?\s*(\d+)\b', clean)
    if pid_match and any(w in clean for w in ["close", "kill", "terminate", "funga", "zima", "shut down"]):
        return ("close_app", {"pid": int(pid_match.group(1))})

    # Strip conversational prefixes/suffixes
    clean = re.sub(r'^(?:please|can you|could you|help me|kindly|tafadhali|naomba)\s+', '', clean).strip()
    clean = re.sub(r'\s+(?:for me|please|now|haraka|sasa)$', '', clean).strip()

    if clean in {"close window", "close the window", "close active window", "close current window", "funga dirisha", "funga window", "close it", "funga hii"}:
        return ("close_window", {})

    if clean in {"close camera", "funga camera", "zima camera", "kill camera", "close webcam"}:
        return ("close_app", {"app_name": "camera"})

    match = re.match(
        r'^(?:close|exit|terminate|kill|shut down|funga|zima)\s+([a-zA-Z0-9_\- ]+)$',
        clean
    )
    if match:
        target = match.group(1).strip()
        if target in {"window", "the window", "active window", "current window", "dirisha"}:
            return ("close_window", {})
        if target in {"camera", "the camera", "webcam", "kamera"}:
            return ("close_app", {"app_name": "camera"})
        if target and len(target.split()) <= 3:
            return ("close_app", {"app_name": target})
    return None


def _check_direct_tool(message: str) -> str | None:
    """Detect direct queries for system tools, dev environment, or hardware info."""
    clean = message.strip().lower()
    clean = re.sub(r'[?!.,;:_#@*()\-+]', ' ', clean).strip()

    # 1. Check for installed applications / desktop programs query
    app_query_words = ["program", "programu", "app", "application", "software"]
    action_words = ["list", "available", "avaible", "installed", "what", "show", "all", "orodha", "zilizopo"]
    if any(w in clean for w in app_query_words) and any(a in clean for a in action_words):
        if not any(w in clean for w in ["write", "create", "install", "download", "delete", "close", "kill"]):
            res = execute_tool("installed_apps", {})
            return res.get("output", "Could not query installed applications.")

    # 2. Check for dev tools query (git, node, python, docker)
    tool_keywords = [
        "list all tools", "list tools", "available tools", "installed tools",
        "what tools do i have", "dev tools", "developer tools", "programming tools"
    ]
    if any(kw in clean for kw in tool_keywords):
        res = execute_tool("installed_tools", {})
        return res.get("output", "Could not query installed tools.")

    # 3. Check for system security status
    sec_keywords = [
        "security status", "ecurity sttus", "ecurity status", "inspect security", "check security",
        "security report", "inspect the security", "hali ya usalama", "hali ya ulinzi", "defender", "antivirus"
    ]
    if any(kw in clean for kw in sec_keywords):
        res = execute_tool("security_status", {})
        return res.get("output", "Could not query security status.")

    # 4. Check for system hardware stats
    sys_keywords = ["system info", "system status", "pc info", "hardware info", "hali ya kompyuta"]
    if any(kw in clean for kw in sys_keywords):
        res = execute_tool("system_info", {})
        return res.get("output", "Could not query system info.")

    return None


def _parse_tool_request(content: str) -> dict[str, object] | None:
    """Parse JSON tool requests, extracting JSON even if surrounded by text or markdown."""
    # 1. Regex search for JSON object with "action": "execute"
    match = re.search(r'\{[^{}]*"action"\s*:\s*"execute"[^{}]*\}', content, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict) and parsed.get("action") == "execute":
                return parsed
        except json.JSONDecodeError:
            pass

    # 2. Markdown or trimmed candidate
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict) and parsed.get("action") == "execute":
            return parsed
    except Exception:
        pass
    return None


def query_ollama_local(message: str, language: str = "en") -> LocalQueryResult | None:
    """Query Ollama and execute validated local tools from services.executor."""
    try:
        # Short-circuit: check local offline commands instantly before hitting Ollama
        local_reply = parse_local_command(message, language)
        if local_reply:
            print(f"[LOCAL CMD]: Short-circuited via parse_local_command: '{message}'")
            return LocalQueryResult(reply=local_reply)

        sys_prompt = (
            "You are Nyota, an autonomous AI assistant built specifically for Samson Mwamloso running directly on his Windows PC.\n"
            "Samson Mwamloso is your creator, engineer, and boss. When asked who Samson is, state clearly that Samson Mwamloso is your creator, engineer, and boss. NEVER refer to the user in third person or say 'your creator' when talking to Samson. You were NOT created by OpenAI, Microsoft, or Google.\n"
            "You have REAL tools that execute on this computer. NEVER say 'as an AI I don't have access' — "
            "you DO have access through your tools.\n"
            "NEVER refuse a coding, scripting, or file request — use write_file to save scripts to disk.\n\n"
            "TOOL USAGE RULES:\n"
            "Output ONLY raw JSON when calling a tool. No markdown, no explanation.\n\n"
            "Tool: launch_app — open any app or website\n"
            '{"action":"execute","tool":"launch_app","arguments":{"app_name":"<app>"}}\n\n'
            "Tool: close_app — close a named running app\n"
            '{"action":"execute","tool":"close_app","arguments":{"app_name":"<app>"}}\n\n'
            "Tool: close_window — close the currently active/foreground window\n"
            '{"action":"execute","tool":"close_window","arguments":{}}\n\n'
            "Tool: security_status — inspect PC security, Windows Defender, Firewall, and system health\n"
            '{"action":"execute","tool":"security_status","arguments":{}}\n\n'
            "Tool: installed_apps — list installed desktop programs and apps on Samson's PC\n"
            '{"action":"execute","tool":"installed_apps","arguments":{}}\n\n'
            "Tool: search_file — find files by name inside workspace folders\n"
            '{"action":"execute","tool":"search_file","arguments":{"name":"<filename>"}}\n\n'
            "Tool: list_files — list contents of a folder (use path like C:\\\\Users\\\\samson)\n"
            '{"action":"execute","tool":"list_files","arguments":{"path":"<folder_path>"}}\n\n'
            "Tool: write_file — WRITE and SAVE a script or file to disk\n"
            '{"action":"execute","tool":"write_file","arguments":{"path":"<file_path>","content":"<full_code_here>"}}\n\n'
            "Tool: run_command — run a shell/PowerShell command (requires user approval)\n"
            '{"action":"execute","tool":"run_command","arguments":{"command":"<cmd>"}}\n\n'
            "EXAMPLES:\n"
            'User: "list available programs" -> {"action":"execute","tool":"installed_apps","arguments":{}}\n'
            'User: "who is your boss" -> Plain text: "Samson Mwamloso is my creator and boss. I was built specifically to assist him."\n'
            'User: "write a bubble sort script" -> {"action":"execute","tool":"write_file","arguments":{"path":"bubble_sort.py","content":"def bubble_sort(arr):\\n    ..."}}\n'
            'User: "list my user folder" -> {"action":"execute","tool":"list_files","arguments":{"path":"C:\\\\Users\\\\samson"}}\n'
            'User: "find microsoft store" -> {"action":"execute","tool":"launch_app","arguments":{"app_name":"microsoft store"}}\n'
            'User: "open notepad" -> {"action":"execute","tool":"launch_app","arguments":{"app_name":"notepad"}}\n'
            'User: "close edge" -> {"action":"execute","tool":"close_app","arguments":{"app_name":"msedge"}}\n'
            'User: "play music" -> {"action":"execute","tool":"launch_app","arguments":{"app_name":"music"}}\n'
            "For regular conversation (greetings, questions not requiring tools) reply with plain text only."
        )
        if language == "sw":
            sys_prompt += "\nJibu kwa Kiswahili fasaha kwa maongezi ya kawaida."
        else:
            sys_prompt += "\nRespond strictly in English for regular conversation."

        sys_prompt += f"\nAvailable tools: {json.dumps([t['name'] for t in available_tools()], ensure_ascii=True)}"

        started_at = time.perf_counter()
        response = ollama.chat(
            model="qwen2.5-coder:3b",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": message}
            ],
            options={
                "temperature": 0.2,
                "top_p": 0.9,
                "repeat_penalty": 1.2,
                "repeat_last_n": 64,
                "num_predict": 512,
            },
            keep_alive="10m",
        )
        elapsed = time.perf_counter() - started_at
        ai_reply = response["message"]["content"].strip()
        print(f"[OLLAMA]: Response generated in {elapsed:.2f}s: {ai_reply[:80]}")

        tool_request = _parse_tool_request(ai_reply)
        if not tool_request:
            return LocalQueryResult(reply=ai_reply)

        tool_name = tool_request.get("tool", "")
        arguments = tool_request.get("arguments", {})

        # Qwen sometimes double-nests: {"arguments": {"arguments": {"app_name": "..."}}}
        if isinstance(arguments, dict) and "arguments" in arguments and isinstance(arguments.get("arguments"), dict):
            arguments = arguments["arguments"]

        # Qwen sometimes mixes up tool names for close commands — remap them
        _TOOL_ALIASES = {
            "close_window": "close_window",
            "close_app":    "close_app",
            "kill_app":      "close_app",
            "quit_app":      "close_app",
            "terminate_app": "close_app",
            "stop_app":      "close_app",
            "launch_app":    "launch_app",
            "open_app":      "launch_app",
            "start_app":     "launch_app",
            "locate_app":    "launch_app",    # "locate microsoft store" → launch_app
            "search_file":   "search_file",
            "find_file":     "search_file",
            "locate_file":   "search_file",
            "list_files":    "list_files",
            "list_directory": "list_files",
            "list_dir":      "list_files",
            "write_file":    "write_file",
            "create_file":   "write_file",
            "save_file":     "write_file",
            "write_code":    "write_file",
            "create_script": "write_file",
            "installed_apps": "installed_apps",
            "list_apps":     "installed_apps",
            "list_programs": "installed_apps",
            "installed_programs": "installed_apps",
            "available_programs": "installed_apps",
            "run_command":   "run_command",
            "execute_command": "run_command",
            "run_script":    "run_command",
            "shell_command": "run_command",
        }
        tool_name = _TOOL_ALIASES.get(tool_name, tool_name)

        # If Qwen returns launch_app with a "close" intent in the original message, fix it
        if tool_name == "launch_app":
            raw_msg = message.strip().lower()
            if any(w in raw_msg for w in ["close", "exit", "quit", "funga", "zima", "kill"]):
                app_arg = arguments.get("app_name") or arguments.get("name") or ""
                if app_arg:
                    # Redirect to close_app instead of launch_app
                    tool_name = "close_app"
                    arguments = {"app_name": app_arg}
                    print(f"[OLLAMA FIX]: Redirected launch_app -> close_app for '{raw_msg}'")

        # If Qwen uses search_file when user says "locate/find <app>" — redirect to launch_app
        if tool_name == "search_file":
            raw_msg = message.strip().lower()
            app_hint = any(w in raw_msg for w in ["store", "app", "program", "application", "software"])
            open_hint = any(w in raw_msg for w in ["locate", "find", "open", "fungua"])
            if open_hint and app_hint:
                q = arguments.get("name") or arguments.get("query") or ""
                if q:
                    tool_name = "launch_app"
                    arguments = {"app_name": q}
                    print(f"[OLLAMA FIX]: Redirected search_file -> launch_app for '{raw_msg}'")

        if not isinstance(tool_name, str) or not tool_name:
            return LocalQueryResult(reply="I could not validate that tool request. Please try again.")

        tool_definition = next(
            (tool for tool in available_tools() if tool["name"] == tool_name),
            None,
        )
        if not tool_definition:
            return LocalQueryResult(reply=f"I cannot use the requested tool: {tool_name}.")
        if tool_definition["requires_approval"]:
            approval = approval_store.create(tool_name, arguments)
            return LocalQueryResult(
                reply=f"Samson, I need your approval before I run: {tool_name}. Please approve request {approval.approval_id} in the HUD.",
                approval_id=approval.approval_id,
            )

        print(f"[TOOL]: Executing {tool_name} with {arguments} via executor.py")
        tool_result = execute_tool(tool_name, arguments, approved=True)
        output = tool_result.get("output", f"{tool_name} completed.")

        # For write_file: give Nyota a voice-friendly completion message
        if tool_name == "write_file" and tool_result.get("success"):
            file_name = arguments.get("path", "the file")
            output = f"Done! I have written and saved {file_name} for you. You can find it in the chat console."

        return LocalQueryResult(reply=output)
    except Exception as e:
        print(f"[OLLAMA ERROR]: Local Ollama failed ({e}). Falling back to Gemini...")
        return None


def warm_ollama_local():
    """Load the local model during backend startup instead of the first user request."""
    try:
        ollama.chat(
            model="qwen2.5-coder:3b",
            messages=[{"role": "user", "content": "Reply with OK."}],
            options={"num_predict": 1},
            keep_alive="10m",
        )
        print("[OLLAMA]: qwen2.5-coder:3b is warm and ready")
    except Exception as e:
        print(f"[OLLAMA]: Warm-up skipped ({e}). Gemini/Mistral fallback remains available.")

def calculate_rms(audio_data: bytes) -> float:
    import struct
    import math
    try:
        count = len(audio_data) // 2
        if count == 0:
            return 0.0
        # 16-bit signed PCM (2 bytes per sample)
        shorts = struct.unpack(f"{count}h", audio_data)
        sum_squares = sum((sample / 32768.0) ** 2 for sample in shorts)
        return math.sqrt(sum_squares / count)
    except Exception as e:
        print(f"Error calculating RMS: {e}")
        return 0.0


def _sanitize_for_voice(text: str) -> str:
    """Prepare text for natural SAPI5 TTS: strip code blocks, file paths, raw JSON syntax, and markdown."""
    clean = text.strip()

    # 1. If text is a raw JSON dict, convert to friendly natural speech
    if clean.startswith("{") and clean.endswith("}"):
        try:
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                items = [k for k, v in parsed.items() if v and "not" not in str(v).lower()]
                if items:
                    return f"Available tools found on your system include: {', '.join(items)}."
        except Exception:
            pass

    # 2a. Handle CLOSED fenced code blocks (```...```)
    if "```" in clean:
        has_intro = bool(re.match(r'^[^\`]{10,}', clean))
        clean = re.sub(r'```[\w]*[\r\n]+[\s\S]*?```', '', clean).strip()
        if not clean:
            return "I have generated the code in your chat console."
        elif not has_intro:
            clean = "I have provided the code in your console. " + clean

    # 2b. Handle UNCLOSED fenced code blocks (code was cut off mid-generation)
    # e.g. "```python\ndef foo():\n    ..." — no closing fence
    if "```" in clean:
        # Everything from the opening fence to end is raw code — strip it
        clean = re.sub(r'```[\w]*[\r\n][\s\S]*$', '', clean).strip()
        if not clean:
            return "I have generated the code in your chat console."
        clean = clean + " The full code is in your chat window."

    # 3. Strip raw Windows file paths (e.g. C:\Program Files\...)
    clean = re.sub(r'from\s+[A-Za-z]:\\[^\n\r.]+\.[a-zA-Z0-9]+', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'[A-Za-z]:\\[^\n\r.]+\.[a-zA-Z0-9]+', '', clean, flags=re.IGNORECASE)

    # 4. Clean markdown formatting symbols
    clean = clean.replace('`', '').replace('*', '').replace('#', '')
    clean = re.sub(r'\s+', ' ', clean).strip()

    # 5. Shorten excessively long code dumps if code wasn't wrapped in fences
    code_keywords = ["def ", "import ", "class ", "return ", "const ", "function ", "print(", "#include", "int main(", "void ", "typedef struct", "struct "]
    if len(clean) > 220 and any(keyword in clean for keyword in code_keywords):
        first_sentence = clean.split('.')[0]
        if len(first_sentence) < 120 and any(w in first_sentence.lower() for w in ["here", "script", "code", "created", "wrote", "saved", "program", "queue", "sort"]):
            clean = first_sentence + ". The full code is available in your chat window where you can save or copy it."
        else:
            clean = "I have written the requested script and code for you in the console."

    return clean


def speak_async(text: str, language: str = "en"):
    threading.Thread(
        target=tts_engine.speak,
        args=(text, language),
        daemon=True
    ).start()


def persist_messages(conversation_id: str, user_message: str, assistant_message: str):
    try:
        from services.supabase_service import supabase

        check_conv = supabase.table("conversations").select("id").eq("id", conversation_id).execute()
        if not check_conv.data:
            supabase.table("conversations").insert({
                "id": conversation_id,
                "title": "Jarvis Core Chat",
                "status": "active"
            }).execute()

        supabase.table("messages").insert({
            "conversation_id": conversation_id,
            "role": "user",
            "content": user_message
        }).execute()
        supabase.table("messages").insert({
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": assistant_message
        }).execute()
        print("[CHAT]: Successfully logged to Supabase")
    except Exception as e:
        print(f"[CHAT]: Failed to log chat to Supabase: {e}")
        # Fallback to hybrid memory if available
        try:
            from services.hybrid_memory import hybrid_memory
            if hybrid_memory and hybrid_memory.local_conn:
                hybrid_memory._execute_query(
                    "INSERT INTO sync_queue (operation_type, table_name, record_id, payload, sync_status) VALUES (%s, %s, %s, %s, 'pending')",
                    ("insert", "messages", 0, json.dumps({
                        "conversation_id": conversation_id,
                        "user_message": user_message,
                        "assistant_message": assistant_message
                    }))
                )
                print("[CHAT]: Queued for sync in hybrid memory")
        except Exception as fallback_error:
            print(f"[CHAT]: Hybrid memory fallback also failed: {fallback_error}")


@router.get("/tts-status")
def get_tts_status(_=Depends(validate_api_key)):
    return {"is_speaking": tts_engine.is_speaking()}


@router.post("/interrupt")
def interrupt_speech(_=Depends(validate_api_key)):
    tts_engine.interrupt()
    print("[TTS]: Speech interrupted by user request.")
    return {"status": "interrupted"}


@router.post("/chat", response_model=ChatResponse)
@limiter.limit("30/minute")
async def chat(request: Request, body: ChatRequest, _=Depends(validate_api_key)):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    mode_result = check_mode_change(body.message)
    if mode_result:
        speak_async(mode_result, language="en")
        return ChatResponse(
            reply=mode_result,
            conversation_id=body.conversation_id or "new-conversation-id",
            approval_id=None,
            approval_status=None,
        )

    approval_id = None
    try:
        language = body.language or "en"

        # 1. Direct tool execution via executor.py for launch, close & tool requests
        target_app = _extract_launch_app(body.message)
        target_close = _extract_close_app(body.message)
        direct_tool_reply = _check_direct_tool(body.message)
        if target_app:
            print(f"[EXECUTOR]: Direct launch detected for '{target_app}' via executor.py")
            res = execute_tool("launch_app", {"app_name": target_app}, approved=True)
            reply = res.get("output", f"Opening {target_app.title()}.")
        elif target_close:
            tool_name, tool_args = target_close
            print(f"[EXECUTOR]: Direct close detected ({tool_name}, {tool_args}) via executor.py")
            res = execute_tool(tool_name, tool_args, approved=True)
            reply = res.get("output", "Closed.")
        elif direct_tool_reply:
            print(f"[EXECUTOR]: Direct tool query handled via executor.py")
            reply = direct_tool_reply
        else:
            # Short-circuit: check local offline commands instantly before hitting Ollama
            local_reply = parse_local_command(body.message, language)
            if local_reply:
                print(f"[LOCAL CMD]: Short-circuited via parse_local_command: '{body.message}'")
                reply = local_reply
            else:
                # 2. Query Ollama local model (qwen2.5-coder:3b)
                local_result = await asyncio.to_thread(query_ollama_local, body.message, language)
                approval_id = local_result.approval_id if local_result else None
                if local_result:
                    reply = local_result.reply
                else:
                    reply = await asyncio.to_thread(query_nyota, body.message, body.conversation_id, language)
    except Exception as e:
        reply = f"Error processing query: {e}"

    conv_id = body.conversation_id or "f8a49c95-3bc4-4161-b51c-4b53cb12c3e1"
    threading.Thread(
        target=persist_messages,
        args=(conv_id, body.message, reply),
        daemon=True,
    ).start()

    speak_async(reply, language)

    return ChatResponse(
        reply=reply,
        conversation_id=body.conversation_id or "new-conversation-id",
        approval_id=approval_id,
        approval_status="pending" if approval_id else None,
    )


@router.post("/voice-chat", response_model=VoiceChatResponse)
@limiter.limit("15/minute")
def voice_chat(request: Request, body: VoiceChatRequest, _=Depends(validate_api_key)):
    lang = body.language or "en"
    approval_id = None

    # 1. Record audio dynamically from default input device
    p = pyaudio.PyAudio()
    try:
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=1024
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to open microphone: {e}")

    frames = []
    chunk_size = 1024
    rate = 16000
    max_duration = 10.0  # max 10 seconds of recording
    silence_duration_limit = 1.2  # stop after 1.2s of silence
    
    max_chunks = int(rate / chunk_size * max_duration)
    silence_chunks_limit = int(rate / chunk_size * silence_duration_limit)
    initial_silence_chunks_limit = int(rate / chunk_size * 2.5)  # 2.5s initial timeout
    
    # Calibrate against the current microphone noise floor before listening.
    calibration_chunks = max(1, int(rate / chunk_size * 0.4))
    calibration_rms = []
    for _ in range(calibration_chunks):
        try:
            data = stream.read(chunk_size, exception_on_overflow=False)
            frames.append(data)
            calibration_rms.append(calculate_rms(data))
        except Exception as e:
            print(f"[MIC]: Calibration error: {e}")
            break

    noise_floor = sum(calibration_rms) / len(calibration_rms) if calibration_rms else 0.0
    silence_threshold = max(0.012, noise_floor * 2.5)
    print(f"[MIC]: Noise floor {noise_floor:.6f}; voice threshold {silence_threshold:.6f}")

    silence_counter = 0
    active_chunk_count = 0
    has_spoken = False
    
    print(f"[MIC]: Starting dynamic recording (target language: {lang})...")
    for i in range(max_chunks):
        try:
            data = stream.read(chunk_size, exception_on_overflow=False)
            frames.append(data)
            
            rms = calculate_rms(data)
            if rms >= silence_threshold:
                active_chunk_count += 1
                if not has_spoken and active_chunk_count >= 3:
                    print(f"[MIC]: Speech detected at {i * chunk_size / rate:.2f}s")
                    has_spoken = True
                if has_spoken:
                    silence_counter = 0
            else:
                active_chunk_count = 0
                silence_counter += 1
                if has_spoken:
                    if silence_counter >= silence_chunks_limit:
                        print(f"[MIC]: Silence detected after speech. Stopping early at {i * chunk_size / rate:.2f}s.")
                        break
                else:
                    if silence_counter >= initial_silence_chunks_limit:
                        print("[MIC]: No speech detected within initial timeout. Stopping early.")
                        break
        except Exception as e:
            print(f"[MIC]: Error reading chunk: {e}")
            break

    stream.stop_stream()
    stream.close()
    p.terminate()

    audio_bytes = b''.join(frames)
    
    # Calculate RMS to detect silence
    rms = calculate_rms(audio_bytes)
    print(f"[MIC]: Captured audio RMS value is {rms:.6f}")
    
    if rms < 0.008:
        print("[MIC]: Silence detected (below threshold 0.008). Skipping transcription.")
        return VoiceChatResponse(
            user_query="",
            reply="",
            conversation_id=body.conversation_id or "new-conversation-id",
            is_silence=True
        )

    # 2. Write frames to temporary WAV file
    temp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    temp_wav_path = temp_wav.name
    temp_wav.close()

    try:
        wf = wave.open(temp_wav_path, 'wb')
        wf.setnchannels(1)
        wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
        wf.setframerate(16000)
        wf.writeframes(audio_bytes)
        wf.close()

        # Target transcription instructions based on selected language
        if lang == "sw":
            prompt = "Transcribe only clearly spoken words in this audio in Swahili. Return only the transcription text. If the audio is silence, noise, music, or unclear, return exactly SILENCE. Do not guess or invent words."
        else:
            prompt = "Transcribe only clearly spoken words in this audio in English. Return only the transcription text. If the audio is silence, noise, music, or unclear, return exactly SILENCE. Do not guess or invent words."
            
        user_query = None
        gemini_failed = False
        try:
            response = model.generate_content([
                {
                    "mime_type": "audio/wav",
                    "data": audio_bytes
                },
                prompt
            ])
            user_query = response.text.strip()
        except Exception as t_err:
            print(f"[VOICE]: Gemini transcription failed: {t_err}. Attempting free SpeechRecognition fallback...")
            gemini_failed = True

        if gemini_failed or not user_query or user_query.lower() in ["silence", "silence.", "silence...", "..."]:
            # Attempt SpeechRecognition fallback
            try:
                import speech_recognition as sr
                r = sr.Recognizer()
                with sr.AudioFile(temp_wav_path) as source:
                    audio = r.record(source)
                
                # Check target language first
                if lang == "sw":
                    try:
                        user_query = r.recognize_google(audio, language="sw-TZ")
                        print(f"[VOICE FALLBACK]: Transcribed via Google (sw-TZ): '{user_query}'")
                    except Exception:
                        try:
                            user_query = r.recognize_google(audio, language="en-US")
                            print(f"[VOICE FALLBACK]: Transcribed via Google (en-US): '{user_query}'")
                        except Exception as ex:
                            print(f"[VOICE FALLBACK]: SpeechRecognition failed on both Swahili and English: {ex}")
                else:
                    try:
                        user_query = r.recognize_google(audio, language="en-US")
                        print(f"[VOICE FALLBACK]: Transcribed via Google (en-US): '{user_query}'")
                    except Exception:
                        try:
                            user_query = r.recognize_google(audio, language="sw-TZ")
                            print(f"[VOICE FALLBACK]: Transcribed via Google (sw-TZ): '{user_query}'")
                        except Exception as ex:
                            print(f"[VOICE FALLBACK]: SpeechRecognition failed on both English and Swahili: {ex}")
            except Exception as sr_err:
                print(f"[VOICE FALLBACK]: SpeechRecognition error: {sr_err}")

        # Clean enclosing quotes if generated by API
        if user_query and user_query.startswith('"') and user_query.endswith('"'):
            user_query = user_query[1:-1].strip()

        if not user_query or user_query.lower() in ["silence", "silence.", "silence...", "..."]:
            if gemini_failed:
                reply = "Samson, nimefikia kikomo cha maswali kwa sasa. Tafadhali jaribu tena baada ya dakika moja. | Samson, my API quota limit has been reached. Please try again in a minute."
                threading.Thread(target=speak_async, args=(reply,), daemon=True).start()
                return VoiceChatResponse(
                    user_query="[Voice Command]",
                    reply=reply,
                    conversation_id=body.conversation_id or "f8a49c95-3bc4-4161-b51c-4b53cb12c3e1",
                    is_rate_limited=True
                )
            else:
                print("[MIC]: Transcription returned silence.")
                return VoiceChatResponse(
                    user_query="",
                    reply="",
                    conversation_id=body.conversation_id or "new-conversation-id",
                    is_silence=True
                )

        # Check for mode change via voice
        mode_result = check_mode_change(user_query)
        if mode_result:
            threading.Thread(target=speak_async, args=(mode_result,), daemon=True).start()
            return VoiceChatResponse(
                user_query=user_query,
                reply=mode_result,
                conversation_id=body.conversation_id or "new-conversation-id",
                is_silence=False
            )

        # 1. Direct tool execution via executor.py for voice launch, close & tool requests
        target_app = _extract_launch_app(user_query)
        target_close = _extract_close_app(user_query)
        direct_tool_reply = _check_direct_tool(user_query)
        if target_app:
            print(f"[EXECUTOR]: Direct voice launch detected for '{target_app}' via executor.py")
            res = execute_tool("launch_app", {"app_name": target_app}, approved=True)
            reply = res.get("output", f"Opening {target_app.title()}.")
        elif target_close:
            tool_name, tool_args = target_close
            print(f"[EXECUTOR]: Direct voice close detected ({tool_name}, {tool_args}) via executor.py")
            res = execute_tool(tool_name, tool_args, approved=True)
            reply = res.get("output", "Closed.")
        elif direct_tool_reply:
            print(f"[EXECUTOR]: Direct voice tool query handled via executor.py")
            reply = direct_tool_reply
        else:
            # Short-circuit: check local offline commands instantly before hitting Ollama
            local_reply = parse_local_command(user_query, lang)
            if local_reply:
                print(f"[LOCAL CMD]: Short-circuited via parse_local_command: '{user_query}'")
                reply = local_reply
            else:
                # 2. Prefer the local Ollama model (qwen2.5-coder:3b), then fall back to Gemini/Mistral.
                local_result = query_ollama_local(user_query, lang)
                if local_result:
                    reply = local_result.reply
                    approval_id = local_result.approval_id
                else:
                    reply = query_nyota(user_query, body.conversation_id, lang)

    except Exception as e:
        print(f"Voice query processing failed: {e}")
        if os.path.exists(temp_wav_path):
            os.remove(temp_wav_path)
        raise HTTPException(status_code=500, detail=f"Failed to process voice query: {e}")

    finally:
        if os.path.exists(temp_wav_path):
            os.remove(temp_wav_path)

    speak_async(reply, lang)

    conv_id = body.conversation_id or "f8a49c95-3bc4-4161-b51c-4b53cb12c3e1"
    threading.Thread(
        target=persist_messages,
        args=(conv_id, user_query, reply),
        daemon=True,
    ).start()

    is_limit = "nimefikia kikomo cha maswali" in reply or "API quota limit has been reached" in reply
    return VoiceChatResponse(
        user_query=user_query,
        reply=reply,
        conversation_id=body.conversation_id or "f8a49c95-3bc4-4161-b51c-4b53cb12c3e1",
        is_rate_limited=is_limit,
        approval_id=approval_id,
        approval_status="pending" if approval_id else None,
    )


@router.post("/voice-mode")
async def voice_mode_switch(body: dict):
    """Switch TTS engine mode via API (for frontend UI button)"""
    mode = body.get("mode", "standard")
    result = tts_engine.set_mode(mode)
    return {"status": "ok", "mode": tts_engine.get_mode(), "message": result}

