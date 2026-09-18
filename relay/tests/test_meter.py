import pytest

from tdx_relay.meter import RateMeter


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_starts_at_zero():
    assert RateMeter(clock=Clock()).value() == 0.0


def test_settles_on_the_arrival_rate():
    clock = Clock()
    meter = RateMeter(window=10.0, clock=clock)
    for _ in range(20 * 100):  # 20 requests per second for ten windows
        meter.record()
        clock.advance(0.05)
    assert meter.value() == pytest.approx(20.0, rel=0.05)


def test_a_slow_caller_reads_as_a_low_rate():
    clock = Clock()
    meter = RateMeter(window=10.0, clock=clock)
    for _ in range(50):
        meter.record()
        clock.advance(2.0)  # one request every two seconds
    assert meter.value() == pytest.approx(0.5, rel=0.1)


def test_a_burst_decays_once_it_stops():
    clock = Clock()
    meter = RateMeter(window=10.0, clock=clock)
    meter.record(100)
    assert meter.value() > 5.0
    clock.advance(60.0)
    assert meter.value() < 0.1
