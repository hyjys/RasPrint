"""Forward the actual peer address, replacing any client-supplied proxies."""

from mitmproxy import http


def requestheaders(flow: http.HTTPFlow) -> None:
    headers = flow.request.headers
    for name in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Port", "X-Forwarded-Proto"):
        headers.pop(name, None)
    if flow.client_conn.peername:
        headers["X-Forwarded-For"] = flow.client_conn.peername[0]
    headers["X-Forwarded-Proto"] = "http"
