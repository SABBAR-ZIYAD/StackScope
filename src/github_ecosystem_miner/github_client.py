from __future__ import annotations

import os
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv

from github_ecosystem_miner.config import CollectionConfig
from github_ecosystem_miner.errors import CollectionError, TransientCollectionError
from github_ecosystem_miner.rate_limits import RateLimitManager


@dataclass(frozen=True)
class RetryDecision:
    action: str
    delay_seconds: float = 0
    reset_epoch: float = 0
    reason: str = ""


def retry_decision(
    status: int,
    headers: Mapping[str, str],
    body: str,
    attempt: int,
    *,
    now: float | None = None,
) -> RetryDecision:
    """Classify an HTTP response without sleeping or performing I/O."""
    current = time.time() if now is None else now
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    remaining = normalized.get("x-ratelimit-remaining")
    retry_after = normalized.get("retry-after")
    text = (body or "").lower()

    if status in (403, 429):
        if remaining == "0":
            reset = float(normalized.get("x-ratelimit-reset", current + 60))
            return RetryDecision("wait_reset", reset_epoch=reset, reason="primary_rate_limit")
        if retry_after is not None:
            return RetryDecision(
                "wait", delay_seconds=max(1.0, float(retry_after) + 1), reason="retry_after"
            )
        if status == 429 or any(
            phrase in text for phrase in ("rate limit", "abuse detection", "secondary rate")
        ):
            return RetryDecision(
                "wait",
                delay_seconds=min(60.0 * (2**attempt), 900.0),
                reason="secondary_rate_limit",
            )
        return RetryDecision("fail", reason="permission_denied")
    if status >= 500:
        return RetryDecision(
            "wait",
            delay_seconds=min(5.0 * (2**attempt), 300.0),
            reason="server_error",
        )
    if status == 401:
        return RetryDecision("fail", reason="authentication_failed")
    if status == 404:
        return RetryDecision("fail", reason="not_found")
    if status == 422:
        return RetryDecision("fail", reason="invalid_request")
    return RetryDecision("fail", reason=f"http_{status}")


class GitHubClient:
    def __init__(
        self,
        config: CollectionConfig,
        *,
        tokens: list[str] | None = None,
        logger: Callable[[dict[str, Any]], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.tokens = [token for token in (tokens or []) if token] or [""]
        self.token_index = 0
        self.logger = logger
        self.client = httpx.Client(
            timeout=config.request_timeout_seconds,
            transport=transport,
            follow_redirects=True,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "stackscope/0.3",
            },
        )
        self.last_search_request = 0.0
        self.rate_limits = RateLimitManager(
            len(self.tokens),
            reserves={
                "core": config.core_rate_reserve,
                "search": config.search_rate_reserve,
                "graphql": config.graphql_rate_reserve,
            },
            wait_enabled=config.wait_for_rate_limit,
        )

    @classmethod
    def from_environment(
        cls,
        config: CollectionConfig,
        *,
        logger: Callable[[dict[str, Any]], None] | None = None,
    ) -> GitHubClient:
        load_dotenv()
        raw = os.getenv("GITHUB_TOKENS") or os.getenv("GITHUB_TOKEN") or ""
        return cls(config, tokens=[token.strip() for token in raw.split(",")], logger=logger)

    @property
    def authenticated(self) -> bool:
        return bool(self.tokens[self.token_index])

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        token = self.tokens[self.token_index]
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _log(self, row: dict[str, Any]) -> None:
        if self.logger:
            self.logger(row)

    def _choose_available_token(self, resource: str) -> None:
        for _ in range(len(self.tokens)):
            if self.rate_limits.token_available(self.token_index, resource):
                return
            self.token_index = (self.token_index + 1) % len(self.tokens)
        self.rate_limits.before_request(self.token_index, resource)

    def _pace_search(self) -> None:
        interval = (
            self.config.search_interval_seconds
            if self.authenticated
            else max(self.config.search_interval_seconds, 6.5)
        )
        delay = self.last_search_request + interval - time.time()
        if delay > 0:
            time.sleep(delay)
        self.last_search_request = time.time()

    def _request(
        self,
        method: str,
        url: str,
        *,
        api: str,
        resource: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        response_kind: str = "json",
        allow_not_found: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | bytes | None:
        last_error = "unknown failure"
        for attempt in range(self.config.max_retries):
            self._choose_available_token(resource)
            self.rate_limits.before_request(self.token_index, resource)
            if resource == "search":
                self._pace_search()
            started = time.perf_counter()
            status: int | None = None
            response: httpx.Response | None = None
            try:
                response = self.client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={**self._headers(), **(extra_headers or {})},
                )
                status = response.status_code
            except httpx.RequestError as error:
                last_error = f"network error: {error}"
                delay = min(2.0 * (2**attempt), 60.0) + random.uniform(0, 0.5)
                self._log(
                    {
                        "api": api,
                        "resource": resource,
                        "endpoint": endpoint,
                        "status": None,
                        "duration_ms": (time.perf_counter() - started) * 1000,
                        "attempt": attempt + 1,
                        "token_index": self.token_index,
                        "remaining": None,
                        "reset_at": None,
                        "graphql_cost": None,
                        "bytes_transferred": 0,
                        "error": last_error,
                    }
                )
                time.sleep(delay)
                continue

            duration_ms = (time.perf_counter() - started) * 1000
            remaining = _int_header(response.headers, "x-ratelimit-remaining")
            reset_at = _int_header(response.headers, "x-ratelimit-reset")
            self.rate_limits.observe_headers(self.token_index, resource, response.headers)
            log_row = {
                "api": api,
                "resource": resource,
                "endpoint": endpoint,
                "status": status,
                "duration_ms": duration_ms,
                "attempt": attempt + 1,
                "token_index": self.token_index,
                "remaining": remaining,
                "reset_at": reset_at,
                "graphql_cost": None,
                "bytes_transferred": len(response.content),
                "error": None,
            }

            if status == 200:
                if response_kind == "bytes":
                    self._log(log_row)
                    return response.content
                try:
                    body = response.json()
                except ValueError as error:
                    last_error = f"non-JSON response: {error}"
                    log_row["error"] = last_error
                    self._log(log_row)
                    time.sleep(min(2.0 * (2**attempt), 30.0))
                    continue
                if not isinstance(body, dict):
                    last_error = "GitHub response is not a JSON object"
                    log_row["error"] = last_error
                    self._log(log_row)
                    continue
                if api == "graphql":
                    rate = (body.get("data") or {}).get("rateLimit") or {}
                    log_row["remaining"] = rate.get("remaining", remaining)
                    log_row["graphql_cost"] = rate.get("cost")
                    self.rate_limits.observe_graphql(
                        self.token_index,
                        remaining=rate.get("remaining"),
                        reset_epoch=_iso_epoch(rate.get("resetAt")),
                    )
                self._log(log_row)
                return body

            if status == 404 and allow_not_found:
                log_row["error"] = "not_found"
                self._log(log_row)
                return None

            decision = retry_decision(status, response.headers, response.text, attempt)
            last_error = decision.reason
            log_row["error"] = decision.reason
            self._log(log_row)
            if decision.action == "wait_reset":
                self.rate_limits.mark_primary_exhausted(
                    self.token_index, resource, decision.reset_epoch
                )
                if len(self.tokens) > 1:
                    self.token_index = (self.token_index + 1) % len(self.tokens)
                continue
            if decision.action == "wait":
                if decision.reason in {"secondary_rate_limit", "retry_after"}:
                    self.rate_limits.mark_secondary_limit(decision.delay_seconds)
                time.sleep(decision.delay_seconds + random.uniform(0, 0.5))
                continue
            raise CollectionError(
                f"GitHub request failed ({decision.reason}) for {endpoint}: {response.text[:300]}"
            )

        if api == "graphql":
            raise TransientCollectionError(
                f"GraphQL request failed after {self.config.max_retries} attempts: {last_error}"
            )
        raise CollectionError(
            f"GitHub request failed after {self.config.max_retries} attempts: {last_error}"
        )

    def rest_get(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        resource: str = "core",
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        url = endpoint if endpoint.startswith("http") else f"https://api.github.com{endpoint}"
        result = self._request(
            "GET",
            url,
            api="rest",
            resource=resource,
            endpoint=endpoint,
            params=params,
            allow_not_found=allow_not_found,
        )
        return result if isinstance(result, dict) or result is None else None

    def get_bytes(
        self,
        url: str,
        *,
        api: str = "raw",
        resource: str = "raw",
        endpoint: str | None = None,
        allow_not_found: bool = False,
        accept: str | None = None,
    ) -> bytes | None:
        result = self._request(
            "GET",
            url,
            api=api,
            resource=resource,
            endpoint=endpoint or url,
            response_kind="bytes",
            allow_not_found=allow_not_found,
            extra_headers={"Accept": accept} if accept else None,
        )
        return result if isinstance(result, bytes) or result is None else None

    def graphql(self, query: str) -> dict[str, Any]:
        if not self.authenticated:
            raise CollectionError("GraphQL enrichment requires GITHUB_TOKEN or GITHUB_TOKENS")
        result = self._request(
            "POST",
            "https://api.github.com/graphql",
            api="graphql",
            resource="graphql",
            endpoint="graphql",
            json_body={"query": query},
        )
        if not isinstance(result, dict):
            raise TransientCollectionError("GraphQL returned an unexpected response type")
        return result

    def budget_snapshot(self) -> dict[str, dict[str, int | float | None]]:
        return self.rate_limits.snapshot()


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _iso_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
