import math

import pytest

from tdx_relay.admission import Admitter, QueueFull, State
from tdx_relay.tokens import TokenSigner


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make(**kwargs):
    clock = Clock()
    kwargs.setdefault("rate", 10.0)
    kwargs.setdefault("burst", 10.0)
    signer = TokenSigner(b"test-key", clock=clock)
    return Admitter(signer, clock=clock, **kwargs), clock


def test_an_idle_relay_admits_the_burst_at_once():
    admitter, _ = make()
    statuses = [admitter.issue() for _ in range(10)]
    assert all(s.state is State.ADMITTED for s in statuses)
    assert all(s.new_token for s in statuses)


def test_the_rest_are_paced_one_slot_apart():
    admitter, clock = make()
    for _ in range(10):
        admitter.issue()

    first = admitter.issue()
    second = admitter.issue()
    assert first.state is State.QUEUED
    assert (first.position, second.position) == (1, 2)
    # Sub-millisecond precision is lost: a token carries whole milliseconds.
    assert first.eta_seconds == pytest.approx(0.1, abs=0.002)
    assert second.eta_seconds == pytest.approx(0.2, abs=0.002)

    clock.advance(1.0)
    assert admitter.check(first.token).state is State.ADMITTED
    assert admitter.check(second.token).state is State.ADMITTED


def test_admits_exactly_ten_per_second():
    admitter, clock = make()
    tokens = [admitter.issue().token for _ in range(35)]
    admitted = lambda: sum(admitter.check(t).state is State.ADMITTED for t in tokens)
    assert admitted() == 10          # the burst
    clock.advance(1.0)
    assert admitted() == 20
    clock.advance(1.5)
    assert admitted() == 35


def test_idle_time_does_not_bank_more_than_the_burst():
    admitter, clock = make()
    clock.advance(600.0)
    tokens = [admitter.issue().token for _ in range(20)]
    assert sum(admitter.check(t).state is State.ADMITTED for t in tokens) == 10


def test_the_relay_stores_nothing_per_token():
    admitter, _ = make()
    for _ in range(1000):
        admitter.issue()
    assert admitter.__dict__.keys() >= {"_tail"}
    assert not any(isinstance(v, (dict, list, set)) for v in admitter.__dict__.values())


def test_a_token_outlives_a_restart():
    admitter, clock = make()
    for _ in range(50):
        admitter.issue()
    token = admitter.issue().token

    restarted = Admitter(TokenSigner(b"test-key", clock=clock), rate=10.0, clock=clock)
    assert restarted.check(token).state is State.QUEUED
    clock.advance(30.0)
    assert restarted.check(token).state is State.ADMITTED


def test_unknown_and_forged_tokens_are_not_recognised():
    admitter, clock = make()
    assert admitter.check(None) is None
    assert admitter.check("") is None
    assert admitter.check("t1.0.AAAA.BBBB") is None
    other = Admitter(TokenSigner(b"another-key", clock=clock), clock=clock)
    assert admitter.check(other.issue().token) is None


def test_an_unused_admission_window_closes():
    admitter, clock = make(window=30.0)
    token = admitter.issue().token
    clock.advance(29.0)
    assert admitter.check(token).state is State.ADMITTED

    clock.advance(2.0)
    assert admitter.check(token) is None        # late is the same as expired
    assert admitter.is_stale(token) is True     # but the relay knows it was ours


def test_a_served_call_restarts_the_window():
    admitter, clock = make(window=30.0)
    token = admitter.issue().token
    for _ in range(20):
        clock.advance(29.0)
        assert admitter.check(token).state is State.ADMITTED
        token = admitter.renew(admitter.claims(token))
    clock.advance(31.0)
    assert admitter.check(token) is None


def test_a_forged_token_is_not_called_stale():
    admitter, clock = make()
    other = Admitter(TokenSigner(b"another-key", clock=clock), clock=clock)
    assert admitter.is_stale(other.issue().token) is False
    assert admitter.is_stale("rubbish") is False


def test_a_queued_token_stays_good_until_its_window_closes():
    admitter, clock = make(rate=1.0, burst=1.0, window=30.0)
    admitter.issue()
    token = admitter.issue().token               # admitted one second from now
    clock.advance(30.5)
    assert admitter.check(token).state is State.ADMITTED
    clock.advance(1.0)
    assert admitter.check(token) is None


def test_a_wait_past_the_horizon_is_refused():
    admitter, _ = make(rate=1.0, burst=1.0, max_wait=5.0)
    for _ in range(6):
        admitter.issue()
    with pytest.raises(QueueFull):
        admitter.issue()


def test_waiting_counts_the_booked_slots():
    admitter, clock = make(rate=10.0, burst=10.0)
    assert admitter.waiting() == 0
    for _ in range(30):
        admitter.issue()
    assert admitter.waiting() == pytest.approx(20, abs=1)
    clock.advance(1.0)
    assert admitter.waiting() == pytest.approx(10, abs=1)
