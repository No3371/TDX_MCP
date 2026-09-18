from tdx_relay.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_burst_then_refusal():
    clock = Clock()
    limiter = RateLimiter(burst=3, rate=0.1, clock=clock)
    assert [limiter.take("ip")[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = limiter.take("ip")
    assert allowed is False
    assert retry_after == 10.0


def test_credits_refill_over_time():
    clock = Clock()
    limiter = RateLimiter(burst=2, rate=0.5, clock=clock)
    limiter.take("ip")
    limiter.take("ip")
    assert limiter.take("ip")[0] is False
    clock.advance(2.0)
    assert limiter.take("ip")[0] is True


def test_limits_are_per_ip():
    clock = Clock()
    limiter = RateLimiter(burst=1, rate=0.1, clock=clock)
    assert limiter.take("a")[0] is True
    assert limiter.take("a")[0] is False
    assert limiter.take("b")[0] is True
