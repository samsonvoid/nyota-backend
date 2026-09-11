from fastapi import APIRouter, Depends, Request
from services.supabase_service import supabase
from middleware.security import validate_api_key
from middleware.rate_limit import limiter

router = APIRouter(prefix="/api", tags=["conversations"])


@router.get("/conversations")
@limiter.limit("30/minute")
async def list_conversations(request: Request, _=Depends(validate_api_key)):
    resp = supabase.table("conversations").select("*").order("updated_at", desc=True).execute()
    return {"conversations": resp.data}


@router.post("/conversations")
@limiter.limit("20/minute")
async def create_conversation(request: Request, _=Depends(validate_api_key)):
    resp = supabase.table("conversations").insert(
        {"title": "New Conversation", "status": "active"}
    ).execute()
    return resp.data[0] if resp.data else {"error": "Failed to create"}
