import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from middleware.security import setup_security, register_api_key
from routes.chat import router as chat_router, warm_ollama_local
from routes.logs import router as logs_router
from routes.conversations import router as conversations_router
from routes.tools import router as tools_router
from services.memory import initialize_memory
from services.hybrid_memory import hybrid_memory, initialize_hybrid_memory

load_dotenv()

app = FastAPI(
    title="Nyota Assistant API",
    version="2.5.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    max_age=3600,
)

# --- Security Middleware Stack ---
limiter = setup_security(app)

# --- API Key ---
register_api_key(os.getenv("API_SECRET_KEY", ""))

# --- Routes ---
app.include_router(chat_router)
app.include_router(logs_router)
app.include_router(conversations_router)
app.include_router(tools_router)

import pyttsx3
import threading

def run_startup_speech():
    try:
        engine = pyttsx3.init()
        voices = engine.getProperty('voices')
        if voices:
            # Set to David (usually index 0 on Windows)
            engine.setProperty('voice', voices[0].id)
        engine.setProperty('rate', 155)
        engine.say("Hello Samson. Nyota Assistant has initialized successfully. Core systems are live and listening.")
        engine.runAndWait()
    except Exception as e:
        print(f"Startup Speech Error: {e}")

@app.on_event("startup")
async def startup_event():
    initialize_memory()
    # Initialize hybrid memory (local PostgreSQL)
    await initialize_hybrid_memory()
    # Run in a separate thread so it doesn't block the API engine startup
    threading.Thread(target=run_startup_speech, daemon=True).start()
    threading.Thread(target=warm_ollama_local, daemon=True).start()


@app.get("/")
@limiter.limit("30/minute")
async def root(request: Request):
    return {"status": "ok", "engine": "Nyota Jarvis Core", "version": "2.5.0"}


@app.get("/health")
@limiter.limit("10/minute")
async def health(request: Request):
    return {"status": "healthy", "daemon": "active"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("nyota_engine:app", host="0.0.0.0", port=8000, reload=True)

