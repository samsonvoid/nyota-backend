from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from middleware.rate_limit import limiter

import re


def setup_security(app: FastAPI):
    """Configure all security middleware for the Nyota API"""

    # Register rate limit handler
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Security Headers Middleware
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)

        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com "
            "https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "worker-src 'self' blob:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' https://*.supabase.co wss://*.supabase.co; "
            "frame-ancestors 'none';"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Permissions-Policy"] = "microphone=(), camera=(), geolocation=()"

        return response

    # Input Sanitization Middleware
    @app.middleware("http")
    async def sanitize_inputs(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            if body:
                decoded = body.decode("utf-8", errors="ignore")
                dangerous = [
                    r"(?i)\b(DROP|TRUNCATE|ALTER|EXEC|EXECUTE)\b",
                    r"(?i)'.*\bOR\b.*'='",
                    r"(?i)'.*\bUNION\b.*\bSELECT\b",
                    r"--",
                ]
                for pattern in dangerous:
                    if re.search(pattern, decoded):
                        return JSONResponse(
                            status_code=400,
                            content={"error": "Invalid input pattern detected"},
                        )

        return await call_next(request)

    return limiter


# API Key Validation
API_KEYS = set()

def register_api_key(key: str):
    if key:
        API_KEYS.add(key)

async def validate_api_key(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid API key")
    key = auth.replace("Bearer ", "")
    if key not in API_KEYS:
        raise HTTPException(status_code=403, detail="Invalid API key")
