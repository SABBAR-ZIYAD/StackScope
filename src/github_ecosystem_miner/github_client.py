from __future__ import annotations

import os
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from dotenv import load_dotenv

from github_ecosystem_miner.config import CollectionConfig
from github_ecosystem_miner.errors import CollectionError, TransientCollectionError


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
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-ecosystem-miner/0.2",
            },
        )
        self.last_search_request = 0.0
        self.exhausted_until: dict[tuple[int, str], float] = {}

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
        now = time.time()
        for _ in range(len(self.tokens)):
            if self.exhausted_until.get((self.token_index, resource), 0) <= now:
                return
            self.token_index = (self.token_index + 1) % len(self.tokens)
        earliest = min(self.exhausted_until[(index, resource)] for index in range(len(self.tokens)))
        if not self.config.wait_for_rate_limit:
            raise CollectionError(
                f"All tokens are rate-limited for {resource} until "
                f"{datetime.fromtimestamp(earliest, UTC).isoformat()}"
            )
        time.sleep(max(0.0, earliest - now) + 2.0)

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
    ) -> dict[str, Any]:
        last_error = "unknown failure"
        for attempt in range(self.config.max_retries):
            self._choose_available_token(resource)
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
                    headers=self._headers(),
                )
                status = response.status_code
            except httpx.RequestError as error:
                last_error = f"network error: {error}"
                delay = min(2.0 * (2**attempt), 60.0) + random.uniform(0, 0.5)
                self._log(
                    {
                        "api": api,
                        "endpoint": endpoint,
                        "status": None,
                        "duration_ms": (time.perf_counter() - started) * 1000,
                        "attempt": attempt + 1,
                        "token_index": self.token_index,
                        "remaining": None,
                        "reset_at": None,
                        "graphql_cost": None,
                        "error": last_error,
                    }
                )
                time.sleep(delay)
                continue

            duration_ms = (time.perf_counter() - started) * 1000
            remaining = _int_header(response.headers, "x-ratelimit-remaining")
            reset_at = _int_header(response.headers, "x-ratelimit-reset")
            log_row = {
                "api": api,
                "endpoint": endpoint,
                "status": status,
                "duration_ms": duration_ms,
                "attempt": attempt + 1,
                "token_index": self.token_index,
                "remaining": remaining,
                "reset_at": reset_at,
                "graphql_cost": None,
                "error": None,
            }

            if status == 200:
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
                self._log(log_row)
                if remaining == 0 and reset_at:
                    self.exhausted_until[(self.token_index, resource)] = float(reset_at)
                return body

            decision = retry_decision(status, response.headers, response.text, attempt)
            last_error = decision.reason
            log_row["error"] = decision.reason
            self._log(log_row)
            if decision.action == "wait_reset":
                self.exhausted_until[(self.token_index, resource)] = decision.reset_epoch
                if len(self.tokens) > 1:
                    self.token_index = (self.token_index + 1) % len(self.tokens)
                continue
            if decision.action == "wait":
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
    ) -> dict[str, Any]:
        url = endpoint if endpoint.startswith("http") else f"https://api.github.com{endpoint}"
        return self._request(
            "GET", url, api="rest", resource=resource, endpoint=endpoint, params=params
        )

    def graphql(self, query: str) -> dict[str, Any]:
        if not self.authenticated:
            raise CollectionError("GraphQL enrichment requires GITHUB_TOKEN or GITHUB_TOKENS")
        return self._request(
            "POST",
            "https://api.github.com/graphql",
            api="graphql",
            resource="graphql",
            endpoint="graphql",
            json_body={"query": query},
        )


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
