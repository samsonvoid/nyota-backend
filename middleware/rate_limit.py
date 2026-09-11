from slowapi import Limiter
from slowapi.util import get_remote_address

# Global rate limiter instance - import this in routes
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200/hour", "30/minute"],
    storage_uri="memory://",
)
