"""Backoff calculation, a pure function.

Retries are scheduled, not slept: the executor stores next_attempt_at in the
database, so a restart loses nothing.
"""
import random

BASE_DELAY = 2.0      # seconds, delay ceiling after the first failure
MAX_DELAY = 300.0     # never wait longer than 5 minutes between attempts
MAX_ATTEMPTS = 5      # after this many failures the operation is dead-lettered


def backoff_delay(attempt: int, retry_after: float | None = None, *,
                  base: float = BASE_DELAY, cap: float = MAX_DELAY,
                  rng=random) -> float:
    """Seconds to wait after the `attempt`-th failure (1 = first failure).

    Exponential growth with "equal jitter" (between half and the full ceiling),
    so many failing operations do not all retry at the same moment. A server's
    Retry-After is a minimum.
    """
    ceiling = min(cap, base * 2 ** (attempt - 1))
    delay = rng.uniform(ceiling / 2, ceiling)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay
