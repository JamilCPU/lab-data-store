from ingest_api.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_allows_up_to_limit_then_blocks():
    limiter = RateLimiter(2, now=Clock())
    assert [limiter.allow("a") for _ in range(3)] == [True, True, False]


def test_keys_are_independent():
    limiter = RateLimiter(1, now=Clock())
    assert limiter.allow("a") is True
    assert limiter.allow("b") is True
    assert limiter.allow("a") is False


def test_refills_over_time_without_exceeding_capacity():
    clock = Clock()
    limiter = RateLimiter(60, now=clock)  # one token per second
    assert all(limiter.allow("a") for _ in range(60))
    assert limiter.allow("a") is False
    clock.t += 1.0
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    clock.t += 3600.0
    assert sum(limiter.allow("a") for _ in range(100)) == 60
