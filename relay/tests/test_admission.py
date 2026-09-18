import pytest

from tdx_relay.admission import AdmissionQueue, QueueFull, State


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_queue(**kwargs):
    clock = Clock()
    kwargs.setdefault("rate", 10.0)
    return AdmissionQueue(clock=clock, **kwargs), clock


def test_first_tokens_are_admitted_from_the_burst():
    queue, _ = make_queue()
    statuses = [queue.issue("1.1.1.1") for _ in range(10)]
    assert all(s.state is State.ADMITTED for s in statuses)
    assert all(s.new_token for s in statuses)


def test_eleventh_token_waits_and_reports_its_position():
    queue, clock = make_queue()
    for _ in range(10):
        queue.issue("1.1.1.1")
    first = queue.issue("1.1.1.1")
    second = queue.issue("1.1.1.1")
    assert first.state is State.QUEUED
    assert (first.position, second.position) == (1, 2)
    assert first.eta_seconds == pytest.approx(0.1)

    clock.advance(1.0)
    assert queue.check(first.token).state is State.ADMITTED
    assert queue.check(second.token).state is State.ADMITTED


def test_admits_exactly_ten_per_second():
    queue, clock = make_queue()
    tokens = [queue.issue("1.1.1.1").token for _ in range(35)]
    # 10 admitted from the burst, 25 waiting.
    assert sum(queue.check(t).state is State.ADMITTED for t in tokens) == 10
    clock.advance(1.0)
    assert sum(queue.check(t).state is State.ADMITTED for t in tokens) == 20
    clock.advance(1.5)
    assert sum(queue.check(t).state is State.ADMITTED for t in tokens) == 35


def test_idle_credits_do_not_pile_up_past_the_burst():
    queue, clock = make_queue()
    clock.advance(600.0)
    tokens = [queue.issue("1.1.1.1").token for _ in range(20)]
    assert sum(queue.check(t).state is State.ADMITTED for t in tokens) == 10


def test_unknown_token_is_not_recognised():
    queue, _ = make_queue()
    assert queue.check("nope") is None
    assert queue.check(None) is None
    assert queue.check("") is None


def test_admitted_token_expires_after_idle_ttl():
    queue, clock = make_queue(admitted_ttl=60.0)
    token = queue.issue("1.1.1.1").token
    clock.advance(59.0)
    assert queue.check(token).state is State.ADMITTED  # refreshes last_seen
    clock.advance(59.0)
    assert queue.check(token).state is State.ADMITTED
    clock.advance(61.0)
    assert queue.check(token) is None


def test_abandoned_waiting_token_is_dropped():
    queue, clock = make_queue(rate=1.0, queued_ttl=30.0)
    tokens = [queue.issue("1.1.1.1").token for _ in range(100)]
    far_back = tokens[-1]
    assert queue.check(far_back).state is State.QUEUED
    clock.advance(31.0)
    assert queue.check(far_back) is None


def test_queue_limit_is_enforced():
    queue, _ = make_queue(rate=1.0, queue_limit=2)
    queue.issue("1.1.1.1")  # admitted straight away
    queue.issue("1.1.1.1")
    queue.issue("1.1.1.1")
    with pytest.raises(QueueFull):
        queue.issue("1.1.1.1")


def test_tokens_are_unique_and_opaque():
    queue, _ = make_queue()
    tokens = {queue.issue("1.1.1.1").token for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 24 for t in tokens)
