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
    Intercepts basic requests (like opening Notepad, Calculator, Web Browser, Project folder, 
    and common greetings/gratitude) so they execute instantly and offline.
    """
    # Normalize input
    clean_prompt = prompt.strip().lower()
    # Remove common ending/starting punctuation
    clean_prompt = re.sub(r'[?!.,;:_#@*()\-+]', ' ', clean_prompt).strip()
    # Replace multiple spaces with a single space
    clean_prompt = re.sub(r'\s+', ' ', clean_prompt)
    
    # 1. Notepad patterns
    notepad_words = ["notepad", "notipadi"]
    if any(w in clean_prompt for w in notepad_words):
        if any(action in clean_prompt for action in ["fungua", "washa", "nionyeshe", "open", "launch", "start", "run"]) or clean_prompt in notepad_words:
            open_app_or_file("notepad.exe")
            if language == "en":
                return "I have opened Notepad for you!"
            return "Nimefungua Notepad kwa ajili yako! | I have opened Notepad for you!"
        
    # 2. Calculator patterns
    calc_words = ["calculator", "kikokotoo", "calc"]
    if any(w in clean_prompt for w in calc_words):
        if any(action in clean_prompt for action in ["fungua", "washa", "nionyeshe", "open", "launch", "start", "run"]) or clean_prompt in calc_words:
            open_app_or_file("calc.exe")
            if language == "en":
                return "I have opened the Calculator for you!"
            return "Nimefungua Kikokotoo (Calculator) kwa ajili yako! | I have opened the Calculator for you!"
        
    # 3. Browser / Internet / Google patterns
    browser_words = ["browser", "broswer", "broza", "internet", "google"]
    if any(w in clean_prompt for w in browser_words):
        if any(action in clean_prompt for action in ["fungua", "washa", "washe", "nionyeshe", "open", "launch", "start", "run"]) or clean_prompt in ["browser", "broswer", "broza"]:
            open_app_or_file("https://www.google.com")
            if language == "en":
                return "I have opened your Google web browser!"
            return "Nimefungua kivinjari chako cha Google! | I have opened your Google web browser!"

    # 4. Project Folder / Directory patterns
    folder_words = ["folder", "folda", "directory", "dir", "workspace", "mradi"]
    folder_actions = ["fungua", "nionyeshe", "onyesha", "open", "show", "explore", "start", "washa", "washe", "launch"]
    if any(w in clean_prompt for w in folder_words) or "project" in clean_prompt:
        if any(act in clean_prompt for act in folder_actions) or clean_prompt in folder_words:
            open_app_or_file(PROJECT_DIR)
            if language == "en":
                return "I have opened the project directory in File Explorer!"
            return "Nimefungua folda ya mradi kwenye File Explorer! | I have opened the project directory in File Explorer!"

    # 5. Greetings patterns
    swahili_greetings = ["mambo", "habari", "jambo", "sasa", "kisasa", "vipi mambo"]
    english_greetings = ["hello", "hi", "hey", "how are you"]
    
    words = clean_prompt.split()
    if len(words) <= 3:
        if clean_prompt in swahili_greetings or any(clean_prompt.startswith(g) for g in swahili_greetings) or clean_prompt in english_greetings or any(clean_prompt.startswith(g) for g in english_greetings):
            if language == "en":
                return "Hello Samson! I am doing great and ready to assist you. | Hello Samson! I am doing great and ready to assist you."
            return "Safi sana Samson! Habari yako? Nikusaidie nini leo? | I am doing great Samson! How are you? How can I help you today?"

    # 6. Gratitude patterns
    swahili_thanks = ["asante", "asante sana", "shukran", "shukrani"]
    english_thanks = ["thank you", "thanks", "appreciate it"]
    
    if len(words) <= 3:
        if any(t in clean_prompt for t in swahili_thanks) or any(t in clean_prompt for t in english_thanks):
            if language == "en":
                return "You are very welcome Samson! Let me know if you need anything else. | You are very welcome Samson! Let me know if you need anything else."
            return "Karibu sana Samson! Kuna lingine la kukusaidia? | You are very welcome Samson! Is there anything else I can help you with?"

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

