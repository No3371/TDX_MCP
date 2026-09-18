from tdx_relay.admission import AdmissionQueue
from tdx_relay.gate import Gate
from tdx_relay.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_gate(clock, rate=10.0, burst=10.0, ip_burst=3.0, ip_rate=0.1, **queue_kwargs):
    queue = AdmissionQueue(rate=rate, burst=burst, clock=clock, **queue_kwargs)
    limiter = RateLimiter(burst=ip_burst, rate=ip_rate, clock=clock)
    return Gate(queue, limiter)


def test_first_caller_is_served_at_once_and_keeps_the_new_token():
    gate = make_gate(Clock(), rate=1.0, burst=1.0)
    decision = gate.admit(None, "1.1.1.1")
    assert decision.admitted is True
    assert decision.new_token is True
    assert decision.token


def test_caller_past_the_rate_is_queued_with_a_position_and_a_wait():
    gate = make_gate(Clock(), rate=1.0, burst=1.0)
    gate.admit(None, "1.1.1.1")  # spends the only slot

    decision = gate.admit(None, "2.2.2.2")
    assert decision.admitted is False
    payload = decision.payload
    assert payload["status"] == "queued"
    assert payload["new_token"] is True
    assert payload["queue_position"] == 1
    assert payload["retry_after_seconds"] == 1.0
    assert payload["token"] == decision.token


def test_same_token_is_served_once_the_queue_reaches_it():
    clock = Clock()
    gate = make_gate(clock, rate=1.0, burst=1.0)
    gate.admit(None, "1.1.1.1")
    token = gate.admit(None, "2.2.2.2").token

    again = gate.admit(token, "2.2.2.2")
    assert again.admitted is False
    assert again.payload["new_token"] is False
    assert again.payload["token"] == token

    clock.advance(1.0)
    served = gate.admit(token, "2.2.2.2")
    assert served.admitted is True
    assert served.payload is None


def test_expired_token_is_replaced_by_a_fresh_one():
    clock = Clock()
    gate = make_gate(clock, rate=10.0, admitted_ttl=10.0)
    token = gate.admit(None, "1.1.1.1").token
    clock.advance(11.0)

    decision = gate.admit(token, "1.1.1.1")
    assert decision.token != token
    assert decision.new_token is True


def test_ip_rate_limit_applies_to_token_requests():
    gate = make_gate(Clock(), ip_burst=2, ip_rate=0.1)
    gate.admit(None, "9.9.9.9")
    gate.admit(None, "9.9.9.9")

    decision = gate.admit(None, "9.9.9.9")
    assert decision.admitted is False
    assert decision.payload["status"] == "rate_limited"
    assert decision.payload["retry_after_seconds"] == 10.0


def test_holding_a_token_costs_nothing_against_the_ip_limit():
    gate = make_gate(Clock(), ip_burst=1, ip_rate=0.1)
    token = gate.admit(None, "9.9.9.9").token
    for _ in range(20):
        assert gate.admit(token, "9.9.9.9").admitted is True


def test_rate_limited_ip_does_not_block_other_ips():
    gate = make_gate(Clock(), ip_burst=1, ip_rate=0.1)
    gate.admit(None, "1.1.1.1")
    assert gate.admit(None, "1.1.1.1").payload["status"] == "rate_limited"
    assert gate.admit(None, "2.2.2.2").admitted is True


def test_queue_full_is_reported_as_busy():
    gate = make_gate(Clock(), rate=1.0, burst=1.0, ip_burst=100, queue_limit=2)
    for _ in range(3):
        gate.admit(None, "1.1.1.1")
    decision = gate.admit(None, "1.1.1.1")
    assert decision.payload["status"] == "busy"
