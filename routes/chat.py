import os
import json
import tempfile
import wave
import asyncio
import time
import ollama
import pyaudio
import pyttsx3
import threading
from dataclasses import dataclass
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from middleware.security import validate_api_key
from middleware.rate_limit import limiter
from services.gemini_service import model, query_nyota
from services.executor import available_tools, execute_tool
from services.approvals import approval_store
from services.memory import memory_context

router = APIRouter(prefix="/api", tags=["chat"])


active_tts_engine = None
active_tts_lock = threading.Lock()


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

def query_ollama_local(message: str, language: str = "en") -> LocalQueryResult | None:
    """Query Ollama and execute only validated, registered local tools."""
    try:
        sys_prompt = (
    "Wewe ni Nyota Assistant, msaidizi binafsi wa Samson Mwamloso. "
    "Jibu kwa ufupi, usahihi na kwa lugha inayoeleweka kulingana na swali. "
    "Una tools salama za read-only na tools zinazohitaji approval kama 'launch_app'. "
    "Ikiwa mtumiaji anaomba kutumia au kuanzisha programu (kama VS Code au Chrome), au kuitisha tool yoyote, "
    "rudisha JSON pekee katika muundo huu: "
    '{"action":"execute","tool":"launch_app","arguments":{"app_name":"code"}}. '
    "Usibuni tools au commands zisizokuwepo. Kwa mazungumzo ya kawaida yasiyo hitaji tool, jibu kwa maandishi ya kawaida."
)
        if language == "sw":
            sys_prompt += " Jibu kwa Kiswahili fasaha pekee."
        else:
            sys_prompt += " Respond strictly in English."

        sys_prompt += f" Available tools: {json.dumps(available_tools(), ensure_ascii=True)}"
        sys_prompt += f"\nVerified local context:\n{memory_context()}"

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
                "num_predict": 160,
            },
            keep_alive="10m",
        )
        elapsed = time.perf_counter() - started_at
        ai_reply = response["message"]["content"].strip()
        print(f"[OLLAMA]: Response generated in {elapsed:.2f}s")

        tool_request = _parse_tool_request(ai_reply)
        if not tool_request:
            return LocalQueryResult(reply=ai_reply)

        tool_name = tool_request.get("tool")
        arguments = tool_request.get("arguments", {})
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
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
                reply=f"Approval is required before I use {tool_name}. Please approve request {approval.approval_id}.",
                approval_id=approval.approval_id,
            )

        print(f"[TOOL]: Executing read-only tool {tool_name} with {arguments}")
        tool_result = execute_tool(tool_name, arguments)
        follow_up = ollama.chat(
            model="qwen2.5-coder:3b",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": message},
                {"role": "assistant", "content": ai_reply},
                {
                    "role": "user",
                    "content": (
                        f"Tool result for {tool_name}:\n{json.dumps(tool_result, ensure_ascii=True)}\n"
                        "Summarize the result briefly. Do not propose another tool call."
                    ),
                },
            ],
            options={"temperature": 0.2, "num_predict": 160},
            keep_alive="10m",
        )
        return LocalQueryResult(reply=follow_up["message"]["content"].strip())
    except Exception as e:
        print(f"[OLLAMA ERROR]: Local Ollama failed ({e}). Falling back to Gemini...")
        return None


def _parse_tool_request(content: str) -> dict[str, object] | None:
    """Parse strict JSON tool requests, including fenced JSON from small models."""
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").removeprefix("json").strip()
    if not candidate.startswith("{") or not candidate.endswith("}"):
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and parsed.get("action") == "execute":
        return parsed
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


def speak_async(text: str):
    global active_tts_engine
    try:
        if "|" in text:
            text_to_speak = text.split("|")[1].strip()
        else:
            text_to_speak = text.strip()

        # Re-initialize pyttsx3 inside the thread
        engine = pyttsx3.init()
        voices = engine.getProperty('voices')
        if voices:
            engine.setProperty('voice', voices[0].id)
        engine.setProperty('rate', 155)
        
        with active_tts_lock:
            active_tts_engine = engine
            
        engine.say(text_to_speak)
        engine.runAndWait()
    except Exception as e:
        print(f"Background TTS Speech error: {e}")
    finally:
        with active_tts_lock:
            active_tts_engine = None


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


@router.post("/interrupt")
def interrupt_speech(_=Depends(validate_api_key)):
    global active_tts_engine
    with active_tts_lock:
        if active_tts_engine:
            try:
                active_tts_engine.stop()
                print("[TTS]: Speech interrupted by user request.")
                return {"status": "interrupted"}
            except Exception as e:
                print(f"[TTS]: Failed to interrupt speech: {e}")
                return {"status": "error", "detail": str(e)}
    return {"status": "idle"}


@router.post("/chat", response_model=ChatResponse)
@limiter.limit("30/minute")
async def chat(request: Request, body: ChatRequest, _=Depends(validate_api_key)):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    approval_id = None
    try:
        language = body.language or "en"

        # Prefer the local Ollama model, then fall back to Gemini/Mistral.
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

    threading.Thread(target=speak_async, args=(reply,), daemon=True).start()

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

        # Prefer the local Ollama model, then fall back to Gemini/Mistral.
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

    threading.Thread(target=speak_async, args=(reply,), daemon=True).start()

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

