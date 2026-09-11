from fastapi import APIRouter, Depends, Query, Request
from services.supabase_service import supabase
from middleware.security import validate_api_key
from middleware.rate_limit import limiter

router = APIRouter(prefix="/api", tags=["logs"])


@router.get("/logs")
@limiter.limit("60/minute")
async def get_logs(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    level: str | None = Query(None),
    _=Depends(validate_api_key),
):
    query = supabase.table("system_logs").select("*").order("created_at", desc=True).limit(limit)
    if level:
        query = query.eq("level", level)
    resp = query.execute()
    return {"logs": resp.data}


@router.get("/logs/recent")
@limiter.limit("30/minute")
async def recent_logs(request: Request, _=Depends(validate_api_key)):
    resp = supabase.table("system_logs").select("*").order("created_at", desc=True).limit(10).execute()
    return {"logs": resp.data}
