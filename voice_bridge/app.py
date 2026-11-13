import hmac

from fastapi import FastAPI, Request, WebSocket, Depends
from fastapi.responses import JSONResponse

from .config import get_config
from .security import authorize
from .proxy import proxy_http
from .realtime import realtime_endpoint

app = FastAPI(title="oMLX Voice Bridge", version="0.3.0")


@app.get("/health")
async def health():
    cfg = get_config()
    return {
        "status": "ok",
        "bridge": "omlx-voice-bridge",
        "bridge_version": app.version,
        "upstream": cfg.omlx.base_url,
        "reasoning_model": cfg.models.reasoning,
        "speech_model": cfg.models.speech,
    }


@app.websocket("/v1/realtime")
async def realtime(ws: WebSocket):
    expected = get_config().server.api_key
    if expected:
        supplied = ws.headers.get("authorization", "")
        supplied = supplied.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied, expected):
            await ws.close(code=4401, reason="Invalid bridge API key")
            return
    await realtime_endpoint(ws)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def catch_all(request: Request, path: str, _=Depends(authorize)):
    if path == "health":
        return JSONResponse({"status": "ok"})
    # Route every chat-completion request through the configured reasoning model.
    # This is applied to the body passed to the proxy, rather than mutating
    # FastAPI's internal Request cache.
    if path == "v1/chat/completions" and request.method == "POST":
        import json

        body = await request.body()
        try:
            payload = json.loads(body)
            payload["model"] = get_config().models.reasoning
            body = json.dumps(payload).encode("utf-8")
        except (json.JSONDecodeError, TypeError):
            pass
        return await proxy_http(request, path, body_override=body)
    return await proxy_http(request, path)
