import os
import httpx
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
# Supabase Management API token - generate from https://app.supabase.com/account/tokens
SUPABASE_MGMT_TOKEN = os.getenv("SUPABASE_MGMT_TOKEN")

PROJECT_REF = SUPABASE_URL.replace("https://", "").split(".")[0]


def get_headers():
    return {
        "Authorization": f"Bearer {SUPABASE_MGMT_TOKEN}",
        "Content-Type": "application/json",
    }


class Migration:
    version: str = ""
    description: str = ""

    def up(self, api):
        raise NotImplementedError

    def down(self, api):
        raise NotImplementedError


class MigrationAPI:
    """Executes SQL via Supabase Management API endpoint"""

    def __init__(self):
        self.client = httpx.Client(base_url="https://api.supabase.com", verify=True, timeout=120.0)

    def run(self, sql: str):
        resp = self.client.post(
            f"/v1/projects/{PROJECT_REF}/database/query",
            headers=get_headers(),
            json={"query": sql},
        )
        if resp.status_code >= 400:
            raise Exception(f"SQL error ({resp.status_code}): {resp.text}")
        return resp.json()

    def close(self):
        self.client.close()


class MigrationRunner:
    def __init__(self):
        self.api = MigrationAPI()

    def create_tracking_table(self):
        self.api.run("""
            CREATE TABLE IF NOT EXISTS _migrations (
                version TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                executed_at TIMESTAMPTZ DEFAULT now()
            )
        """)

    def get_executed(self):
        try:
            rows = self.api.run(
                "SELECT version FROM _migrations ORDER BY version"
            )
            return {row["version"] for row in rows} if rows else set()
        except Exception:
            return set()

    def run(self, migrations: list[Migration]):
        self.create_tracking_table()
        executed = self.get_executed()

        for m in migrations:
            if m.version in executed:
                print(f"[SKIP] {m.version} - already executed")
                continue
            print(f"[RUN]  {m.version} - {m.description}")
            try:
                m.up(self.api)
                self.api.run(
                    "INSERT INTO _migrations (version, description) VALUES "
                    f"('{m.version}', '{m.description}')"
                )
                print(f"[DONE] {m.version}")
            except Exception as e:
                print(f"[FAIL] {m.version}: {e}")
                raise

    def rollback(self, migrations: list[Migration]):
        self.create_tracking_table()
        executed = self.get_executed()

        for m in reversed(migrations):
            if m.version not in executed:
                continue
            print(f"[ROLLBACK] {m.version} - {m.description}")
            try:
                m.down(self.api)
                self.api.run(
                    f"DELETE FROM _migrations WHERE version = '{m.version}'"
                )
                print(f"[DONE] Rolled back {m.version}")
            except Exception as e:
                print(f"[FAIL] rollback {m.version}: {e}")
                raise

    def close(self):
        self.api.close()
