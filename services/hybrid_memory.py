"""
Hybrid Memory Service - Local PostgreSQL + Supabase Cloud Sync
Handles offline-first memory with automatic cloud synchronization
Uses pg8000 (pure Python) for PostgreSQL connectivity
"""

import os
import json
import asyncio
import pg8000
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
import httpx
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))


@dataclass
class LearnedPath:
    app_name: str
    resolved_path: str
    confidence_score: int = 1


@dataclass
class ToolFailure:
    tool_name: str
    error_message: str
    resolution_note: Optional[str] = None


class HybridMemoryService:
    """Hybrid memory service with local PostgreSQL and Supabase sync"""
    
    def __init__(self):
        self.local_conn: Optional[pg8000.Connection] = None
        self.supabase_url = os.getenv("SUPABASE_URL")
        self.supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
        self.sync_enabled = os.getenv("SYNC_ENABLED", "true").lower() == "true"
        self._lock = asyncio.Lock()
        
    async def initialize(self):
        """Initialize local PostgreSQL connection"""
        try:
            self.local_conn = pg8000.connect(
                host=os.getenv("LOCAL_DB_HOST", "localhost"),
                port=int(os.getenv("LOCAL_DB_PORT", "5432")),
                database=os.getenv("LOCAL_DB_NAME", "nyota_local"),
                user=os.getenv("LOCAL_DB_USER", "postgres"),
                password=os.getenv("LOCAL_DB_PASSWORD", "postgres"),
                ssl_context=False
            )
            print("[HYBRID]: Local PostgreSQL connection established")
        except Exception as e:
            print(f"[HYBRID]: Failed to connect to local PostgreSQL: {e}")
            print("[HYBRID]: Running in degraded mode (local memory unavailable)")
    
    async def close(self):
        """Close database connections"""
        if self.local_conn:
            self.local_conn.close()
    
    def _execute_query(self, query: str, params: tuple = ()) -> List[tuple]:
        """Execute a query and return results"""
        if not self.local_conn:
            return []
        try:
            cursor = self.local_conn.cursor()
            cursor.execute(query, params)
            if cursor.description:
                return cursor.fetchall()
            cursor.close()
            self.local_conn.commit()
            return []
        except Exception as e:
            print(f"[HYBRID]: Query error: {e}")
            self.local_conn.rollback()
            return []
    
    # ============ LEARNED PATHS ============
    
    async def learn_path(self, app_name: str, resolved_path: str, confidence: int = 1) -> bool:
        """Store or update a learned application path"""
        if not self.local_conn:
            return False
            
        try:
            self._execute_query("""
                INSERT INTO learned_paths (app_name, resolved_path, confidence_score, last_used_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (app_name, resolved_path) 
                DO UPDATE SET 
                    confidence_score = GREATEST(learned_paths.confidence_score, %s),
                    last_used_at = now()
            """, (app_name, resolved_path, confidence, confidence))
                
            print(f"[HYBRID]: Learned path for '{app_name}' -> {resolved_path}")
            return True
        except Exception as e:
            print(f"[HYBRID]: Failed to learn path: {e}")
            return False
    
    async def get_learned_path(self, app_name: str) -> Optional[str]:
        """Retrieve a learned path for an application"""
        if not self.local_conn:
            return None
            
        try:
            rows = self._execute_query("""
                SELECT resolved_path, confidence_score 
                FROM learned_paths 
                WHERE app_name = %s 
                ORDER BY confidence_score DESC, last_used_at DESC 
                    LIMIT 1
            """, (app_name,))
                
            if rows:
                resolved_path = rows[0][0]
                # Update last_used_at
                self._execute_query("""
                    UPDATE learned_paths 
                    SET last_used_at = now() 
                    WHERE app_name = %s AND resolved_path = %s
                """, (app_name, resolved_path))
                return resolved_path
            return None
        except Exception as e:
            print(f"[HYBRID]: Failed to get learned path: {e}")
            return None
    
    # ============ TOOL FAILURES ============
    
    async def record_tool_failure(self, tool_name: str, error_message: str, resolution: Optional[str] = None) -> bool:
        """Record a tool execution failure for learning"""
        if not self.local_conn:
            return False
            
        try:
            self._execute_query("""
                INSERT INTO tool_failures (tool_name, error_message, resolution_note, last_seen_at, occurrence_count)
                VALUES (%s, %s, %s, now(), 1)
                ON CONFLICT (tool_name, error_message) 
                DO UPDATE SET 
                    occurrence_count = tool_failures.occurrence_count + 1,
                    resolution_note = COALESCE(%s, tool_failures.resolution_note),
                    last_seen_at = now()
            """, (tool_name, error_message, resolution, resolution))
                
            print(f"[HYBRID]: Recorded failure for tool '{tool_name}'")
            return True
        except Exception as e:
            print(f"[HYBRID]: Failed to record failure: {e}")
            return False
    
    async def get_tool_resolution(self, tool_name: str, error_message: str) -> Optional[str]:
        """Get known resolution for a tool failure"""
        if not self.local_conn:
            return None
            
        try:
            rows = self._execute_query("""
                SELECT resolution_note, occurrence_count 
                FROM tool_failures 
                WHERE tool_name = %s AND error_message = %s
                LIMIT 1
            """, (tool_name, error_message))
                
            return rows[0][0] if rows and rows[0][0] else None
        except Exception as e:
            print(f"[HYBRID]: Failed to get tool resolution: {e}")
            return None
    
    # ============ SYSTEM CONFIGS ============
    
    async def get_config(self, key: str, default: Any = None) -> Any:
        """Get a system configuration value"""
        if not self.local_conn:
            return default
            
        try:
            rows = self._execute_query("""
                SELECT config_value, config_type 
                FROM system_configs 
                WHERE config_key = %s
            """, (key,))
                
            if not rows:
                return default
            
            value = rows[0][0]
            config_type = rows[0][1]
            
            if config_type == 'integer':
                return int(value)
            elif config_type == 'boolean':
                return value.lower() == 'true'
            elif config_type == 'json':
                return json.loads(value)
            return value
        except Exception as e:
            print(f"[HYBRID]: Failed to get config: {e}")
            return default
    
    async def set_config(self, key: str, value: Any, config_type: str = 'string', description: str = '') -> bool:
        """Set a system configuration value"""
        if not self.local_conn:
            return False
            
        try:
            str_value = str(value)
            if config_type == 'json':
                str_value = json.dumps(value)
                
            self._execute_query("""
                INSERT INTO system_configs (config_key, config_value, config_type, description, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (config_key) 
                DO UPDATE SET 
                    config_value = %s,
                    config_type = %s,
                    description = COALESCE(%s, system_configs.description),
                    updated_at = now()
            """, (key, str_value, config_type, description, str_value, config_type, description))
                
            return True
        except Exception as e:
            print(f"[HYBRID]: Failed to set config: {e}")
            return False
    
    # ============ SYNC QUEUE ============
    
    async def _queue_sync(self, operation: str, table: str, payload: Dict[str, Any]) -> bool:
        """Queue a record for cloud synchronization"""
        if not self.sync_enabled or not self.local_conn:
            return False
            
        try:
            self._execute_query("""
                INSERT INTO sync_queue (operation_type, table_name, record_id, payload, sync_status)
                VALUES (%s, %s, 0, %s, 'pending')
            """, (operation, table, json.dumps(payload)))
            return True
        except Exception as e:
            print(f"[HYBRID]: Failed to queue sync: {e}")
            return False
    
    async def sync_to_cloud(self) -> Dict[str, int]:
        """Sync pending records to Supabase cloud"""
        if not self.sync_enabled or not self.supabase_url or not self.local_conn:
            return {"synced": 0, "failed": 0}
        
        results = {"synced": 0, "failed": 0}
        
        try:
            # Get pending records
            rows = self._execute_query("""
                SELECT id, operation_type, table_name, payload 
                FROM sync_queue 
                WHERE sync_status = 'pending' 
                ORDER BY created_at ASC
                LIMIT 50
            """)
                
            for row in rows:
                try:
                    # Attempt to sync to Supabase
                    success = await self._sync_single_record(
                        row[1],  # operation_type
                        row[2],  # table_name
                        json.loads(row[3])  # payload
                    )
                        
                    if success:
                        self._execute_query("""
                            UPDATE sync_queue 
                            SET sync_status = 'synced' 
                            WHERE id = %s
                        """, (row[0],))
                        results["synced"] += 1
                    else:
                        self._execute_query("""
                            UPDATE sync_queue 
                            SET sync_status = 'failed', 
                                retry_count = retry_count + 1,
                                last_attempt_at = now()
                            WHERE id = %s
                        """, (row[0],))
                        results["failed"] += 1
                except Exception as e:
                    print(f"[HYBRID]: Failed to sync record {row[0]}: {e}")
                    results["failed"] += 1
                        
        except Exception as e:
            print(f"[HYBRID]: Sync process failed: {e}")
        
        return results
    
    async def _sync_single_record(self, operation: str, table: str, payload: Dict[str, Any]) -> bool:
        """Sync a single record to Supabase via REST API"""
        try:
            url = f"{self.supabase_url}/rest/v1/{table}"
            headers = {
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            }
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                if operation == "insert":
                    response = await client.post(url, json=payload, headers=headers)
                elif operation == "update":
                    # For updates, we'd need the record ID - simplified here
                    return True
                else:
                    return True
                    
                return response.status_code in (200, 201)
                
        except Exception as e:
            print(f"[HYBRID]: Cloud sync failed: {e}")
            return False


# Global instance
hybrid_memory = HybridMemoryService()


async def initialize_hybrid_memory():
    """Initialize the hybrid memory service"""
    await hybrid_memory.initialize()
