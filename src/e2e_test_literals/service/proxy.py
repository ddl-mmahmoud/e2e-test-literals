"""Reverse-proxies a request into a revision's `datasette serve` subprocess over its
Unix domain socket (see pool.py). This is the only layer between a caller and
Datasette's own HTTP API -- everything Datasette exposes (the JSON table API, the raw
SQL endpoint, canned queries, CSV export, ...) passes through untouched; this module
forwards bytes, it doesn't interpret them.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

# Headers that are connection-specific to the client<->wrapper hop (or the
# wrapper<->datasette hop, for the response side) and must not be forwarded verbatim
# across the proxy boundary.
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def _filter_headers(items) -> list[tuple[str, str]]:
    return [(key, value) for key, value in items if key.lower() not in _HOP_BY_HOP_HEADERS]


async def proxy_request(request: Request, socket_path: Path, upstream_path: str) -> StreamingResponse:
    """Forward `request` to `datasette serve`'s Unix socket at `socket_path`, requesting
    `/<upstream_path>` (plus the original query string) there, and stream the response
    straight back. `upstream_path` is whatever came after the `/{sha}/` prefix in the
    wrapper's own route -- see app.py."""
    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
    client = httpx.AsyncClient(transport=transport, base_url="http://datasette")

    body = await request.body()
    upstream_request = client.build_request(
        request.method,
        f"/{upstream_path}",
        params=request.query_params,
        headers=_filter_headers(request.headers.items()),
        content=body,
    )
    upstream_response = await client.send(upstream_request, stream=True)

    async def _close() -> None:
        await upstream_response.aclose()
        await client.aclose()

    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=dict(_filter_headers(upstream_response.headers.items())),
        background=BackgroundTask(_close),
    )
