import random

from glue.retry import backoff_delay


def test_grows_exponentially_within_jitter_bounds():
    for attempt, ceiling in [(1, 2), (2, 4), (3, 8), (4, 16)]:
        for seed in range(50):
            delay = backoff_delay(attempt, rng=random.Random(seed))
            assert ceiling / 2 <= delay <= ceiling


def test_delay_is_capped():
    assert backoff_delay(30, rng=random.Random(1)) <= 300


def test_retry_after_is_a_minimum():
    assert backoff_delay(1, retry_after=30, rng=random.Random(1)) >= 30


def test_same_seed_gives_same_delay():
    assert (backoff_delay(3, rng=random.Random(7))
            == backoff_delay(3, rng=random.Random(7)))
