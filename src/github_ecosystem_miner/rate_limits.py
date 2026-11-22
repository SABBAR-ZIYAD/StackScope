from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from github_ecosystem_miner.errors import CollectionError
from github_ecosystem_miner.progress import get_logger

LOGGER = get_logger("rate_limits")


@dataclass
class Budget:
    remaining: int | None = None
    limit: int | None = None
    reset_epoch: float = 0.0


class RateLimitManager:
    """Track independent GitHub budgets and a shared secondary-limit cooldown."""

    def __init__(
        self,
        token_count: int,
        *,
        reserves: Mapping[str, int] | None = None,
        wait_enabled: bool = True,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token_count = max(1, token_count)
        self.reserves = {"core": 0, "search": 0, "graphql": 0, **(reserves or {})}
        self.wait_enabled = wait_enabled
        self.clock = clock
        self.sleeper = sleeper
        self.budgets: dict[tuple[int, str], Budget] = {}
        self.secondary_cooldown_until = 0.0

    def budget(self, token_index: int, resource: str) -> Budget:
        return self.budgets.setdefault((token_index, resource), Budget())

    def before_request(self, token_index: int, resource: str) -> None:
        now = self.clock()
        if self.secondary_cooldown_until > now:
            self._wait_or_fail(self.secondary_cooldown_until - now, "secondary rate limit")
            now = self.clock()
        state = self.budget(token_index, resource)
        reserve = self.reserves.get(resource, 0)
        if state.remaining is not None and state.remaining <= reserve and state.reset_epoch > now:
            self._wait_or_fail(state.reset_epoch - now + 2, f"{resource} rate limit")

    def observe_headers(
        self, token_index: int, resource: str, headers: Mapping[str, str]
    ) -> None:
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        state = self.budget(token_index, resource)
        state.remaining = _optional_int(normalized.get("x-ratelimit-remaining"), state.remaining)
        state.limit = _optional_int(normalized.get("x-ratelimit-limit"), state.limit)
        state.reset_epoch = _optional_float(
            normalized.get("x-ratelimit-reset"), state.reset_epoch
        )

    def observe_graphql(
        self, token_index: int, *, remaining: int | None, reset_epoch: float | None
    ) -> None:
        state = self.budget(token_index, "graphql")
        if remaining is not None:
            state.remaining = int(remaining)
        if reset_epoch is not None:
            state.reset_epoch = float(reset_epoch)

    def mark_primary_exhausted(
        self, token_index: int, resource: str, reset_epoch: float
    ) -> None:
        state = self.budget(token_index, resource)
        state.remaining = 0
        state.reset_epoch = reset_epoch

    def mark_secondary_limit(self, delay_seconds: float) -> None:
        self.secondary_cooldown_until = max(
            self.secondary_cooldown_until, self.clock() + max(1.0, delay_seconds)
        )

    def token_available(self, token_index: int, resource: str) -> bool:
        state = self.budget(token_index, resource)
        return not (
            state.remaining is not None
            and state.remaining <= self.reserves.get(resource, 0)
            and state.reset_epoch > self.clock()
        )

    def earliest_reset(self, resource: str) -> float | None:
        resets = [
            self.budget(index, resource).reset_epoch
            for index in range(self.token_count)
            if not self.token_available(index, resource)
        ]
        return min(resets) if resets else None

    def snapshot(self) -> dict[str, dict[str, int | float | None]]:
        result = {}
        for (token_index, resource), state in sorted(self.budgets.items()):
            result[f"token_{token_index}:{resource}"] = {
                "remaining": state.remaining,
                "limit": state.limit,
                "reset_epoch": state.reset_epoch,
            }
        return result

    def _wait_or_fail(self, delay: float, reason: str) -> None:
        if not self.wait_enabled:
            raise CollectionError(f"Request blocked by {reason}; retry in {delay:.0f}s")
        LOGGER.warning("[rate-limit] Waiting %.1f seconds for %s", delay, reason)
        self.sleeper(max(0.0, delay))


def _optional_int(value: str | None, default: int | None) -> int | None:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _optional_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default
