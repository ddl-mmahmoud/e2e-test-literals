"""Reverse-proxies a request into the upstream e2e-test-literals-service API, over its
Unix domain socket (`config.API_SOCKET_PATH`), for anything this UI doesn't have its
own route for -- chiefly each revision's Datasette pages (`/data/{sha}/...`, see
`app.py`'s catch-all route). Forwards bytes, doesn't interpret them.

This is what lets a `datasette_url` returned by the upstream API (e.g. `/data/<sha>`)
stay a plain same-origin path for the browser to open, even when the upstream process
isn't itself externally reachable in this deployment (see repo-root `app.sh`): the
upstream never binds a port at all, so there's nothing for the Domino app's
single-port ingress to conflict with.

A near-duplicate of `service/proxy.py` by design: that module proxies into a
per-revision Datasette subprocess over a UDS, this one proxies into the upstream API
over a (different) UDS, and the two apps share no code (see client.py's docstring).
"""

from __future__ import annotations

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from . import config

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


async def proxy_to_api(request: Request, upstream_path: str) -> StreamingResponse:
    """Forward `request` to the upstream API's Unix socket, requesting `upstream_path`
    (plus the original query string) there, and stream the response straight back.
    `upstream_path` must already have the UI's own routing prefix stripped -- it's
    whatever path the upstream API itself would recognize (see app.py's
    `proxy_passthrough`)."""
    # base_url's host is a placeholder -- the UDS transport is what actually routes
    # the connection, matching service/proxy.py's own UDS client pattern.
    transport = httpx.AsyncHTTPTransport(uds=str(config.API_SOCKET_PATH))
    client = httpx.AsyncClient(transport=transport, base_url="http://api")

    body = await request.body()
    upstream_request = client.build_request(
        request.method,
        upstream_path,
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
