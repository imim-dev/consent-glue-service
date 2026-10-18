"""Fault injection shared by both simulators.

Failures can be switched on at runtime (POST /admin/faults) to demonstrate
retries, rate limits, timeouts and lost responses.
"""
import asyncio
import random

from fastapi import HTTPException

BEFORE_MODES = {"500", "429", "422", "timeout"}


class Faults:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.mode = None            # "500", "429", "422", "timeout", "drop_response"
        self.probability = 1.0      # chance that a call is affected
        self.remaining = None       # affect only the next N calls (None = unlimited)
        self.hang_seconds = 10.0    # how long "timeout" / "drop_response" hang

    def configure(self, mode, probability=1.0, remaining=None, hang_seconds=10.0) -> None:
        self.mode = mode
        self.probability = probability
        self.remaining = remaining
        self.hang_seconds = hang_seconds

    def state(self) -> dict:
        return {"mode": self.mode, "probability": self.probability,
                "remaining": self.remaining, "hang_seconds": self.hang_seconds}

    def _triggered(self) -> bool:
        if self.remaining is not None and self.remaining <= 0:
            return False
        if random.random() > self.probability:
            return False
        if self.remaining is not None:
            self.remaining -= 1
        return True

    async def before(self) -> None:
        """Call at the start of a handler: fails before anything is applied."""
        if self.mode not in BEFORE_MODES or not self._triggered():
            return
        if self.mode == "500":
            raise HTTPException(500, "injected server error")
        if self.mode == "429":
            raise HTTPException(429, "injected rate limit", headers={"Retry-After": "1"})
        if self.mode == "422":
            raise HTTPException(422, "injected validation error")
        await asyncio.sleep(self.hang_seconds)  # "timeout": client gives up first
        raise HTTPException(504, "injected timeout")

    async def after(self) -> None:
        """Call after a write was applied: the response is 'lost'."""
        if self.mode != "drop_response" or not self._triggered():
            return
        await asyncio.sleep(self.hang_seconds)
        raise HTTPException(504, "injected lost response")
