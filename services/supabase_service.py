import os
from dotenv import load_dotenv
from supabase import create_client, Client

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_KEY")

supabase: Client = create_client(supabase_url, supabase_key)
