from fastapi import Header, HTTPException
from .config import get_config

async def authorize(authorization: str | None = Header(default=None)):
    expected = get_config().server.api_key
    if not expected:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if supplied != expected:
        raise HTTPException(status_code=401, detail="Invalid bridge API key")
