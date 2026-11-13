import httpx
from fastapi import Request, Response
from .config import get_config

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


def upstream_headers(request: Request) -> dict[str, str]:
    cfg = get_config()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    if cfg.omlx.api_key:
        headers["authorization"] = f"Bearer {cfg.omlx.api_key}"
    return headers


async def proxy_http(
    request: Request,
    path: str,
    *,
    body_override: bytes | None = None,
) -> Response:
    cfg = get_config()
    url = f"{cfg.omlx.base_url.rstrip('/')}/{path.lstrip('/')}"
    body = body_override if body_override is not None else await request.body()
    timeout = httpx.Timeout(cfg.omlx.timeout_seconds, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        upstream = await client.request(
            request.method,
            url,
            params=request.query_params,
            headers=upstream_headers(request),
            content=body,
        )
    headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() not in {"content-encoding", "content-length"}
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type"),
    )
