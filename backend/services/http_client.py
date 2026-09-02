"""Shared HTTP client: one place for timeouts, retries and error normalisation."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import httpx

from backend import config


class UpstreamError(RuntimeError):
    """Any external-API failure. Surfaced as 'evidence unavailable' -> abstention."""

    def __init__(self, service: str, detail: str):
        super().__init__(f"{service}: {detail}")
        self.service = service
        self.detail = detail


async def get_json(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    service: str = "upstream",
    retries: int = 1,
    headers: Optional[Dict[str, str]] = None,
    allow_list: bool = False,
) -> Any:
    """
    GET + JSON with one retry. Never returns None: failure raises UpstreamError.
    allow_list=True for endpoints that answer with a bare JSON array (Nominatim).
    """
    if config.SIMULATE_LATENCY_MS:
        await asyncio.sleep(config.SIMULATE_LATENCY_MS / 1000.0)

    last_error = "unknown error"
    for _attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_S) as client:
                resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            else:
                data = resp.json()
                # Open-Meteo reports errors as 200/400 bodies: {"error": true, "reason": "..."}
                if isinstance(data, dict) and data.get("error") is True:
                    raise UpstreamError(service, str(data.get("reason", "upstream error")))
                if not isinstance(data, (dict, list)):
                    raise UpstreamError(service, f"unexpected response type {type(data).__name__}")
                if isinstance(data, list) and not allow_list:
                    raise UpstreamError(service, "unexpected JSON array response (allow_list not set)")
                return data
        except UpstreamError:
            raise
        except Exception as exc:  # network / JSON / timeout
            last_error = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(0.4)
    raise UpstreamError(service, last_error)


async def get_text(
    url: str, *, service: str = "upstream", headers: Optional[Dict[str, str]] = None
) -> str:
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_S) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise UpstreamError(service, f"HTTP {resp.status_code}")
        return resp.text
    except UpstreamError:
        raise
    except Exception as exc:
        raise UpstreamError(service, f"{type(exc).__name__}: {exc}") from exc


async def post_json(
    url: str,
    *,
    payload: Dict[str, Any],
    service: str = "upstream",
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
    retries: int = 0,
) -> Any:
    """POST + JSON on the same client/timeout/error contract as get_json.

    Added for the Phase-4 LLM call. `retries=0` by default: a chat-completions request is not
    idempotent-cheap, and the LLM layer already owns its single regeneration attempt, so silently
    retrying here would double the latency the demo feels. Failures raise UpstreamError, so the
    caller can fall back deterministically instead of surfacing an exception to the user.
    """
    if config.SIMULATE_LATENCY_MS:
        await asyncio.sleep(config.SIMULATE_LATENCY_MS / 1000.0)

    last_error = "unknown error"
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout or config.HTTP_TIMEOUT_S) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                # never echo the request body: it carries the bearer token context
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            else:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise UpstreamError(service, f"non-JSON response body: {exc}") from exc
        except UpstreamError:
            raise
        except Exception as exc:  # timeout / connection / TLS
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:      # never sleep after the final attempt: the LLM path has retries=0
            await asyncio.sleep(0.4)
    raise UpstreamError(service, last_error)
