#!/usr/bin/env python3
"""
Nyota Assistant - Database Seeder
Usage:
    py -3.13 backend/seed.py
"""

import os
import httpx
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_MGMT_TOKEN = os.getenv("SUPABASE_MGMT_TOKEN")
PROJECT_REF = SUPABASE_URL.replace("https://", "").split(".")[0]


def run_sql(sql: str):
    resp = httpx.post(
        f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query",
        headers={
            "Authorization": f"Bearer {SUPABASE_MGMT_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"query": sql},
    )
    if resp.status_code >= 400:
        raise Exception(f"SQL error ({resp.status_code}): {resp.text}")
    return resp.json()


def seed():
    print("[SEED] Inserting default agent state...")
    run_sql("""
        INSERT INTO public.agent_state (state, payload)
        VALUES ('idle', '{"mode": "default", "version": "2.5.0"}'::jsonb)
    """)

    print("[SEED] Inserting diagnostic system logs...")
    for level, source, message in [
        ("info", "system", "Nyota Jarvis Core v2.5.0 initialized"),
        ("info", "sapi5", "Microsoft David voice loaded successfully"),
        ("info", "supabase", "Supabase connection established"),
        ("info", "gemini", "Gemini 1.5-Flash model ready"),
        ("info", "daemon", "Uvicorn server listening on 127.0.0.1:8000"),
        ("info", "supabase", "Migration 001+002 applied successfully"),
        ("info", "system", "Nyota Jarvis Core database initialized"),
    ]:
        run_sql(
            "INSERT INTO public.system_logs (level, source, message) VALUES "
            f"('{level}', '{source}', '{message}')"
        )

    print("[SEED] Inserting admin user...")
    run_sql("""
        INSERT INTO public.users (username, email, metadata)
        VALUES ('samson', 'samson@nyota.local', '{"role": "admin", "windows_user": "Samson"}'::jsonb)
    """)

    print("[SEED] Seeding complete!")


if __name__ == "__main__":
    seed()
