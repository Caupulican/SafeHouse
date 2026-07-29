"""Provider-neutral HTTP boundary for AdGuard Home administration."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def api(base: str, auth: str | None, path: str, payload=None, method=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method or ("POST" if data is not None else "GET")
    )
    if auth:
        request.add_header("Authorization", "Basic " + auth)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc)
