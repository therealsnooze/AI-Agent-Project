"""HTTP GET against an allowlist of public JSON APIs. Redirects are
re-checked against the allowlist so they can't be used to reach other hosts."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .. import config
from .base import ActionError, ExecutionResult, SandboxError


def check_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise SandboxError("Only https:// URLs are allowed.")
    host = (parts.hostname or "").lower()
    if host not in config.ALLOWED_HTTP_HOSTS:
        allowed = ", ".join(config.ALLOWED_HTTP_HOSTS)
        raise SandboxError(f"Host '{host}' is not on the allowlist. Allowed hosts: {allowed}.")
    if parts.username or parts.password or (parts.port not in (None, 443)):
        raise SandboxError("Credentials and custom ports are not allowed in URLs.")
    return url.strip()


class _AllowlistRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_AllowlistRedirects)


class WebExecutor:
    def http_get(self, url: str, **_) -> ExecutionResult:
        url = check_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": "OpsPilot-Agent/1.0 (student project)", "Accept": "application/json"})
        try:
            with _OPENER.open(req, timeout=config.HTTP_TIMEOUT_SECONDS) as resp:
                status = resp.status
                body = resp.read(config.HTTP_MAX_BYTES + 1)
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detail = e.read(500).decode("utf-8", errors="replace")
            raise ActionError(f"HTTP {e.code} from {urlsplit(url).hostname}: {detail}") from e
        except urllib.error.URLError as e:
            raise ActionError(f"Network error: {e.reason}") from e
        except TimeoutError as e:
            raise ActionError(f"The request timed out after {config.HTTP_TIMEOUT_SECONDS}s.") from e

        truncated = len(body) > config.HTTP_MAX_BYTES
        text = body[: config.HTTP_MAX_BYTES].decode("utf-8", errors="replace")
        display = {"kind": "text", "text": text[:5000], "language": "text"}
        if "json" in ctype and not truncated:
            try:
                data = json.loads(text)
                text = json.dumps(data, ensure_ascii=False)
                display = {"kind": "json", "data": data}
            except json.JSONDecodeError:
                pass
        if len(text) > config.MAX_TOOL_RESULT_CHARS:
            text = text[: config.MAX_TOOL_RESULT_CHARS] + "\n[response truncated]"
        return ExecutionResult(
            content=f"HTTP {status}\n{text}",
            summary=f"HTTP {status} from {urlsplit(url).hostname} ({len(body):,} bytes)",
            display=display,
        )
