import os
import subprocess
import re
import httpx
from dotenv import load_dotenv
import google.generativeai as genai

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(BASE_DIR)
load_dotenv(os.path.join(BASE_DIR, ".env"))

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

from .executor import DYNAMIC_APP_MAP, execute_tool


def run_command(command: str) -> str:
    """Execute a system shell command on the local Windows machine using PowerShell.
    Use this to run local scripts, install packages, check system resources, run security audits,
    or compile and test code. Always returns the stdout or stderr output.
    """
    try:
        res = subprocess.run(["powershell", "-Command", command], capture_output=True, text=True, timeout=45)
        out = res.stdout or ""
        err = res.stderr or ""
        if err:
            return f"Output:\n{out}\n\nErrors/Warnings:\n{err}"
        return out if out.strip() else "Command executed successfully with no output."
    except Exception as e:
        return f"Error executing command: {e}"


def read_file(filepath: str) -> str:
    """Read the contents of a local file. Use absolute paths or paths relative to the project workspace root."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(filepath: str, content: str) -> str:
    """Write or overwrite the contents of a local file. Use absolute paths or paths relative to the project workspace root."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote to file: {filepath}"
    except Exception as e:
        return f"Error writing file: {e}"


def list_directory(dir_path: str) -> str:
    """List all files and folders in a local directory. Use absolute paths or relative paths."""
    try:
        items = os.listdir(dir_path)
        return "\n".join(items) if items else "Directory is empty."
    except Exception as e:
        return f"Error listing directory: {e}"


def open_app_or_file(target: str) -> str:
    """Launch a Windows GUI application (e.g. 'notepad', 'calc'), open a directory path in File Explorer,
    open a file in its default program, or open a web URL in the browser. This runs non-blocking in the background.
    """
    try:
        # os.startfile acts like double-clicking in Explorer (non-blocking, supports URLs, paths, and executables)
        os.startfile(target)
        return f"Successfully opened {target} in the background."
    except Exception as e:
        # Fallback using subprocess.Popen with shell=True
        try:
            import subprocess
            subprocess.Popen(target, shell=True)
            return f"Opened {target} via subprocess shell."
        except Exception as ex:
            return f"Failed to open {target}: {ex} (Error details: {e})"


SYSTEM_INSTRUCTION = f"""
You are Nyota (v2.5-JARVIS), Samson's highly advanced personal AI assistant. You run natively on Samson's Windows environment.
You are equipped with tools to execute PowerShell commands, read/write local files, list directory contents, and open applications/files/directories/URLs to assist Samson.

Active Workspace:
- Samson's active project workspace is: {PROJECT_DIR}
- Always use this folder path when Samson asks you to search, open, list, or locate folders, scripts, or modules in his project.

Language & Communication Guidelines:
- You fully understand Swahili (Kiswahili) and English.
- You must dynamically detect the language of Samson's prompt and respond in the same language.
- If Samson addresses you in English, respond ENTIRELY in English. Do NOT use the pipe separator (|).
- If Samson addresses you in Swahili or Sheng, respond in Swahili (or Sheng). Since the local Windows SAPI5 voice engine is English-only, if you reply in Swahili, you MUST output your response in the format: `[Swahili Text] | [English Translation for TTS]`.
  Example: `Habari Samson! Nimefungua Notepad. | Hello Samson! I have opened Notepad.`
- Always be warm, intelligent, highly technical, and concise.

Tool Use guidelines:
- When Samson asks you to do something (e.g. create a file, check code, run tests, run a vulnerability audit), use your local tools to execute it on his behalf.
- When Samson asks you to open an application (like Notepad, Calculator, Chrome), a folder (like his project folder), a file, or a website, use the 'open_app_or_file' tool. It runs instantly and non-blocking.
- Always explain what you did and present the output clearly.
"""

model = genai.GenerativeModel(
    model_name="gemini-2.5-flash",
    tools=[run_command, read_file, write_file, list_directory, open_app_or_file],
    system_instruction=SYSTEM_INSTRUCTION
)


MISTRAL_KEYS = [
    "d5F1b9ljF9HUg1PfL8GzOrHV70gF59jm",
    "XyKn3eVwvNpxrFPjclFVj6YF5zmrkWvA",
    "OWEMpjjsDfW4CEJ5gl1ZC52gjBOqFOu5",
    "PFkjVIXAdaRWjqKlOc5fntDjrnOjjTwG"
]


def parse_local_command(prompt: str, language: str = "en") -> str | None:
    """Offline Swahili/English NLP local command parser.
    Intercepts app launch requests and common greetings instantly, without Ollama or Gemini.
    """
    clean_prompt = prompt.strip().lower()
    clean_prompt = re.sub(r'[?!.,;:_#@*()\-+]', ' ', clean_prompt).strip()
    clean_prompt = re.sub(r'\s+', ' ', clean_prompt)

    launch_actions = [
        "launch", "open", "start", "run", "execute", "fire up",
        "fungua", "washa", "washe", "anzisha", "endesha",
    ]

    close_actions = [
        "close", "exit", "quit", "stop", "terminate", "shutdown",
        "funga", "acha", "simamisha", "zima",
    ]

    # 1. Known-app keyword table — ordered longest-match first to avoid "microsoft" matching before "microsoft store"
    app_table = [
        # Multi-word entries FIRST (longest keyword match priority)
        (["microsoft store", "windows store", "ms store", "store app", "duka la windows"], "microsoft store",
         "I have opened the Microsoft Store for you!",
         "Nimefungua Microsoft Store! | I have opened the Microsoft Store for you!"),
        (["microsoft edge", "ms edge"], "msedge",
         "I have opened Microsoft Edge for you!",
         "Nimefungua Microsoft Edge! | I have opened Microsoft Edge for you!"),
        (["microsoft word", "ms word"], "word",
         "I have opened Microsoft Word for you!",
         "Nimefungua Microsoft Word! | I have opened Microsoft Word for you!"),
        (["microsoft excel", "ms excel"], "excel",
         "I have opened Microsoft Excel for you!",
         "Nimefungua Microsoft Excel! | I have opened Microsoft Excel for you!"),
        (["microsoft powerpoint", "ms powerpoint"], "powerpoint",
         "I have opened PowerPoint for you!",
         "Nimefungua PowerPoint! | I have opened PowerPoint for you!"),
        (["google chrome"], "chrome",
         "I have opened Google Chrome for you!",
         "Nimefungua Google Chrome! | I have opened Google Chrome for you!"),
        (["visual studio code", "vs code", "vscode"], "code",
         "I have opened Visual Studio Code for you!",
         "Nimefungua VS Code! | I have opened Visual Studio Code for you!"),
        (["file explorer", "windows explorer"], "explorer",
         "I have opened File Explorer for you!",
         "Nimefungua File Explorer! | I have opened File Explorer for you!"),
        (["windows terminal"], "terminal",
         "I have opened Windows Terminal for you!",
         "Nimefungua Terminal! | I have opened Windows Terminal for you!"),
        (["command prompt"], "cmd",
         "I have opened the Command Prompt for you!",
         "Nimefungua Command Prompt! | I have opened Command Prompt for you!"),
        (["vlc player", "vlc media player"], "vlc",
         "I have opened VLC Media Player for you!",
         "Nimefungua VLC! | I have opened VLC Media Player for you!"),
        (["ms paint"], "paint",
         "I have opened MS Paint for you!",
         "Nimefungua Paint! | I have opened MS Paint for you!"),
        # Single-word entries follow
        (["notepad", "notipadi"], "notepad",
         "I have opened Notepad for you!",
         "Nimefungua Notepad kwa ajili yako! | I have opened Notepad for you!"),
        (["calculator", "kikokotoo", "calc"], "calc",
         "I have opened the Calculator for you!",
         "Nimefungua Kikokotoo (Calculator) kwa ajili yako! | I have opened the Calculator for you!"),
        (["chrome"], "chrome",
         "I have opened Google Chrome for you!",
         "Nimefungua Google Chrome! | I have opened Google Chrome for you!"),
        (["msedge", "edge"], "msedge",
         "I have opened Microsoft Edge for you!",
         "Nimefungua Microsoft Edge! | I have opened Microsoft Edge for you!"),
        (["firefox"], "firefox",
         "I have opened Firefox for you!",
         "Nimefungua Firefox! | I have opened Firefox for you!"),
        (["code"], "code",
         "I have opened Visual Studio Code for you!",
         "Nimefungua VS Code! | I have opened Visual Studio Code for you!"),
        (["spotify"], "spotify",
         "I have opened Spotify for you!",
         "Nimefungua Spotify! | I have opened Spotify for you!"),
        (["discord"], "discord",
         "I have opened Discord for you!",
         "Nimefungua Discord! | I have opened Discord for you!"),
        (["telegram"], "telegram",
         "I have opened Telegram for you!",
         "Nimefungua Telegram! | I have opened Telegram for you!"),
        (["whatsapp"], "whatsapp",
         "I have opened WhatsApp for you!",
         "Nimefungua WhatsApp! | I have opened WhatsApp for you!"),
        (["word"], "word",
         "I have opened Microsoft Word for you!",
         "Nimefungua Microsoft Word! | I have opened Microsoft Word for you!"),
        (["excel"], "excel",
         "I have opened Microsoft Excel for you!",
         "Nimefungua Microsoft Excel! | I have opened Microsoft Excel for you!"),
        (["powerpoint", "ppt"], "powerpoint",
         "I have opened PowerPoint for you!",
         "Nimefungua PowerPoint! | I have opened PowerPoint for you!"),
        (["vlc"], "vlc",
         "I have opened VLC Media Player for you!",
         "Nimefungua VLC! | I have opened VLC Media Player for you!"),
        (["paint"], "paint",
         "I have opened MS Paint for you!",
         "Nimefungua Paint! | I have opened MS Paint for you!"),
        (["terminal"], "terminal",
         "I have opened Windows Terminal for you!",
         "Nimefungua Terminal! | I have opened Windows Terminal for you!"),
        (["powershell"], "powershell",
         "I have opened PowerShell for you!",
         "Nimefungua PowerShell! | I have opened PowerShell for you!"),
        (["cmd"], "cmd",
         "I have opened the Command Prompt for you!",
         "Nimefungua Command Prompt! | I have opened Command Prompt for you!"),
        (["explorer"], "explorer",
         "I have opened File Explorer for you!",
         "Nimefungua File Explorer! | I have opened File Explorer for you!"),
        (["store", "duka"], "microsoft store",
         "I have opened the Microsoft Store for you!",
         "Nimefungua Microsoft Store! | I have opened the Microsoft Store for you!"),
    ]

    # Extend app_table with dynamically discovered apps from the full PC scan
    for app_key, exe_paths in DYNAMIC_APP_MAP.items():
        if len(app_key) >= 2:
            for keywords, _, _, _ in app_table:
                if app_key in keywords:
                    break
            else:
                display_name = " ".join(app_key.split("_")) if "_" in app_key else app_key.replace("-", " ")
                app_table.append(([app_key, display_name], exe_paths[0] if exe_paths else app_key,
                                  f"I have opened {display_name} for you!",
                                  f"Nimefungua {display_name}! | I have opened {display_name} for you!"))

    for keywords, app_target, en_reply, sw_reply in app_table:
        if any(kw in clean_prompt for kw in keywords):
            has_launch = any(act in clean_prompt for act in launch_actions)
            has_close = any(act in clean_prompt for act in close_actions)
            is_exact = clean_prompt in keywords
            
            # Check close actions FIRST (priority over launch)
            if has_close:
                result = execute_tool("close_app", {"app_name": app_target})
                if result["success"]:
                    close_en = en_reply.replace("opened", "closed")
                    # Swahili: replace "Nimefungua" (I opened) with "Nimefunga" (I closed) and "opened" with "closed" in English part
                    close_sw = sw_reply.replace("opened", "closed").replace("Nimefungua", "Nimefunga")
                    return close_en if language == "en" else close_sw
                else:
                    return result["output"]
            
            # Then check launch actions
            if has_launch or is_exact:
                open_app_or_file(app_target)
                return en_reply if language == "en" else sw_reply

    # 2. Generic "launch/open <anything>" catch-all
    generic_match = re.match(
        r'^(?:launch|open|start|run|execute|fire up|fungua|washa|washe|anzisha|endesha)\s+(.+)$',
        clean_prompt
    )
    if generic_match:
        target_name = generic_match.group(1).strip()
        if target_name and len(target_name) <= 60:
            try:
                import threading
                from .executor import _find_windows_app
                result_holder: list = []

                def _lookup():
                    path = _find_windows_app(target_name)
                    result_holder.append(path)

                t = threading.Thread(target=_lookup, daemon=True)
                t.start()
                t.join(timeout=2.0)

                if result_holder and result_holder[0] and os.path.exists(result_holder[0]):
                    open_app_or_file(result_holder[0])
                    if language == "en":
                        return f"I have launched {target_name} for you!"
                    return f"Nimefungua {target_name}! | I have launched {target_name} for you!"
            except Exception as e:
                print(f"[LOCAL CMD]: Generic launch failed for '{target_name}': {e}")

    # 2b. Generic "close/exit <anything>" catch-all
    close_match = re.match(
        r'^(?:close|exit|quit|stop|terminate|shutdown|funga|acha|simamisha|zima)\s+(.+)$',
        clean_prompt
    )
    if close_match:
        target_name = close_match.group(1).strip()
        if target_name and len(target_name) <= 60:
            result = execute_tool("close_app", {"app_name": target_name})
            if result["success"]:
                if language == "en":
                    return f"I have closed {target_name} for you!"
                return f"Nimefunga {target_name}! | I have closed {target_name} for you!"
            else:
                return result["output"]

    # 3. Browser / Google URL patterns
    browser_words = ["browser", "broswer", "broza", "internet", "google"]
    if any(w in clean_prompt for w in browser_words):
        if any(act in clean_prompt for act in launch_actions) or clean_prompt in ["browser", "broswer", "broza"]:
            open_app_or_file("https://www.google.com")
            if language == "en":
                return "I have opened your web browser!"
            return "Nimefungua kivinjari chako! | I have opened your web browser!"

    # 4. Project Folder / Directory patterns
    folder_words = ["folder", "folda", "directory", "dir", "workspace", "mradi"]
    folder_actions = ["fungua", "nionyeshe", "onyesha", "open", "show", "explore", "start", "washa", "washe", "launch"]
    if any(w in clean_prompt for w in folder_words) or "project" in clean_prompt:
        if any(act in clean_prompt for act in folder_actions) or clean_prompt in folder_words:
            open_app_or_file(PROJECT_DIR)
            if language == "en":
                return "I have opened the project directory in File Explorer!"
            return "Nimefungua folda ya mradi kwenye File Explorer! | I have opened the project directory in File Explorer!"

    # 5. Greetings
    swahili_greetings = ["mambo", "habari", "jambo", "sasa", "kisasa", "vipi mambo"]
    english_greetings = ["hello", "hi", "hey", "how are you"]
    words = clean_prompt.split()
    if len(words) <= 3:
        if (
            clean_prompt in swahili_greetings
            or any(clean_prompt.startswith(g) for g in swahili_greetings)
            or clean_prompt in english_greetings
            or any(clean_prompt.startswith(g) for g in english_greetings)
        ):
            if language == "en":
                return "Hello Samson! I am doing great and ready to assist you. | Hello Samson! I am doing great and ready to assist you."
            return "Safi sana Samson! Habari yako? Nikusaidie nini leo? | I am doing great Samson! How are you? How can I help you today?"

    # 6. Gratitude
    swahili_thanks = ["asante", "asante sana", "shukran", "shukrani"]
    english_thanks = ["thank you", "thanks", "appreciate it"]
    if len(words) <= 3:
        if any(t in clean_prompt for t in swahili_thanks) or any(t in clean_prompt for t in english_thanks):
            if language == "en":
                return "You are very welcome Samson! Let me know if you need anything else. | You are very welcome Samson! Let me know if you need anything else."
            return "Karibu sana Samson! Kuna lingine la kukusaidia? | You are very welcome Samson! Is there anything else I can help you with?"

    # 7. Identity & Boss / Creator queries (instant 0ms response, zero hallucination)
    identity_triggers = [
        "who is your boss", "who's your boss", "who is your creator", "who created you",
        "who made you", "who built you", "who owns you", "what is your name",
        "who are you", "who are u", "tell me about yourself",
        "wewe ni nani", "bosi wako ni nani", "nani bosi wako", "bosi wako",
        "nani aliyekuunda", "nani alikutengeneza", "jina lako nani", "wewe nani"
    ]
    if any(trig in clean_prompt for trig in identity_triggers):
        if language == "en":
            return "I am Nyota, an autonomous AI assistant built specifically for Samson Mwamloso. Samson is my boss and creator. | I am Nyota, an autonomous AI assistant built specifically for Samson Mwamloso. Samson is my boss and creator."
        return "Mimi ni Nyota, msaidizi wako wa AI niliyetengenezwa maalum kwa ajili ya Samson Mwamloso. Samson ndiye bosi na muundaji wangu! | I am Nyota, an autonomous AI assistant built specifically for Samson Mwamloso. Samson is my boss and creator."

    # 8. Installed Programs / Applications queries (instant 0ms response, zero token loops)
    app_query_words = ["program", "programu", "app", "application", "software"]
    action_words = ["list", "available", "avaible", "installed", "what", "show", "all", "orodha", "zilizopo"]
    if any(w in clean_prompt for w in app_query_words) and any(a in clean_prompt for a in action_words):
        if not any(w in clean_prompt for w in ["write", "create", "install", "download", "delete", "close"]):
            from .executor import installed_apps
            res = installed_apps()
            out = res.get("output", "")
            if out:
                if language == "en":
                    return f"{out} | {out}"
                return f"Programu zilizopatikana kwenye kompyuta yako: {out} | {out}"

    return None


def query_mistral_fallback(prompt: str, conversation_id: str | None = None, language: str = "en") -> str | None:
    """Queries the Mistral chat completions API as a fallback when Gemini fails or is rate-limited.
    Utilizes verified active Mistral API keys in rotation and maintains conversational context.
    """
    messages = [
        {"role": "system", "content": SYSTEM_INSTRUCTION}
    ]
    
    if conversation_id and conversation_id != "new-conversation-id" and conversation_id != "samson-conversation":
        try:
            from services.supabase_service import supabase
            # Fetch last 15 messages for context
            resp = supabase.table("messages") \
                .select("role, content") \
                .eq("conversation_id", conversation_id) \
                .order("created_at", desc=False) \
                .limit(15) \
                .execute()
            
            for msg in resp.data:
                # Map role to Mistral equivalents
                role = "user" if msg["role"] == "user" else "assistant"
                messages.append({"role": role, "content": msg["content"]})
        except Exception as e:
            print(f"[MISTRAL FALLBACK]: Failed to fetch history: {e}")
            
    # Language guidance directive
    if language == "en":
        lang_instruction = "IMPORTANT: You must respond ENTIRELY in English. Do NOT use the pipe separator (|) or include any Swahili translations."
    else:
        lang_instruction = "IMPORTANT: You must respond in Swahili (or Sheng). Since the local audio TTS voice is English-only, you MUST output your response in the pipe-split format: [Swahili Response] | [English Translation for SAPI5 TTS]."
        
    backup_instruction = "IMPORTANT BACKUP NOTICE: You are currently running as a backup model because Gemini is temporarily unavailable or rate-limited. You do NOT have tool execution capabilities (you cannot run PowerShell, list directories, read/write files, or open applications). If the user asks you to perform a system action (like listing files, executing a command, or opening a folder), explain politely that you are running in backup mode and cannot execute actions directly, and ask them to retry in a minute when Gemini is back online."
    
    guided_prompt = f"System Directive: {lang_instruction}\nSystem Backup Directive: {backup_instruction}\n\nUser Input: {prompt}"
    messages.append({"role": "user", "content": guided_prompt})
    
    # Check env first, otherwise fallback to known working keys
    env_keys = os.getenv("MISTRAL_API_KEYS", "")
    keys = [k.strip() for k in env_keys.split(",") if k.strip()]
    if not keys:
        keys = MISTRAL_KEYS
        
    for key in keys:
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {key}"
            }
            payload = {
                "model": "mistral-small-latest",
                "messages": messages,
                "temperature": 0.7
            }
            
            print(f"[MISTRAL FALLBACK]: Attempting request with key prefix {key[:8]}...")
            with httpx.Client() as client:
                response = client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=15.0
                )
                if response.status_code == 200:
                    res_data = response.json()
                    reply = res_data["choices"][0]["message"]["content"].strip()
                    print(f"[MISTRAL FALLBACK]: Query succeeded with key prefix {key[:8]}...")
                    return reply
                else:
                    print(f"[MISTRAL FALLBACK]: Key prefix {key[:8]}... returned HTTP {response.status_code}: {response.text}")
        except Exception as ex:
            print(f"[MISTRAL FALLBACK]: Exception with key prefix {key[:8]}...: {ex}")
            
    return None


def query_nyota(prompt: str, conversation_id: str | None = None, language: str = "en") -> str:
    """Queries the stateful conversational agent.
    1. Intercepts local offline commands instantly via parse_local_command.
    2. Attempts stateful Gemini API model generation.
    3. If Gemini fails or is rate-limited, queries the Mistral API backup layer.
    """
    # 1. Check local offline command parser
    local_reply = parse_local_command(prompt, language)
    if local_reply:
        print(f"[LOCAL COMMAND]: Successfully intercepted query: '{prompt}' (lang: {language})")
        return local_reply

    history = []
    if conversation_id and conversation_id != "new-conversation-id" and conversation_id != "samson-conversation":
        try:
            from services.supabase_service import supabase
            # Fetch last 15 messages for context
            resp = supabase.table("messages") \
                .select("role, content") \
                .eq("conversation_id", conversation_id) \
                .order("created_at", desc=False) \
                .limit(15) \
                .execute()
            
            last_role = None
            for msg in resp.data:
                role = "user" if msg["role"] == "user" else "model"
                if role != last_role:
                    history.append({
                        "role": role,
                        "parts": [msg["content"]]
                    })
                    last_role = role
        except Exception as e:
            print(f"Failed to fetch conversation history: {e}")

    # Language guidance directive prepended to Gemini prompt
    if language == "en":
        lang_instruction = "IMPORTANT: You must respond ENTIRELY in English. Do NOT use the pipe separator (|) or include any Swahili translations."
    else:
        lang_instruction = "IMPORTANT: You must respond in Swahili (or Sheng). Since the local audio TTS voice is English-only, you MUST output your response in the pipe-split format: [Swahili Response] | [English Translation for SAPI5 TTS]."
        
    guided_prompt = f"System Directive: {lang_instruction}\n\nUser Input: {prompt}"

    try:
        # Start chat with history and auto function calling enabled in start_chat
        chat = model.start_chat(history=history, enable_automatic_function_calling=True)
        response = chat.send_message(guided_prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Gemini query failed: {e}")
        
        # 3. Fallback to Mistral API
        print("[FALLBACK]: Querying Mistral fallback...")
        mistral_reply = query_mistral_fallback(prompt, conversation_id, language)
        if mistral_reply:
            return mistral_reply
            
        err_str = str(e).lower()
        if "429" in err_str or "quota" in err_str or "limit" in err_str:
            if language == "en":
                return "Samson, my API quota limit has been reached. Please try again in a minute."
            return "Samson, nimefikia kikomo cha maswali kwa sasa. Tafadhali jaribu tena baada ya dakika moja. | Samson, my API quota limit has been reached. Please try again in a minute."
        
        # Fallback to direct generation if history or chat fails
        try:
            response = model.generate_content(guided_prompt)
            return response.text.strip()
        except Exception as ex:
            print(f"Gemini generate content failed: {ex}")
            
            # Fallback to Mistral API again
            mistral_reply = query_mistral_fallback(prompt, conversation_id, language)
            if mistral_reply:
                return mistral_reply
                
            ex_str = str(ex).lower()
            if "429" in ex_str or "quota" in ex_str or "limit" in ex_str:
                if language == "en":
                    return "Samson, my API quota limit has been reached. Please try again in a minute."
                return "Samson, nimefikia kikomo cha maswali kwa sasa. Tafadhali jaribu tena baada ya dakika moja. | Samson, my API quota limit has been reached. Please try again in a minute."
            return f"Error querying Nyota Engine: {ex}"


