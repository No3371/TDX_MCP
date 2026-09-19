import pytest

from tdx_relay.admission import Admitter
from tdx_relay.gate import Gate
from tdx_relay.meter import RateMeter
from tdx_relay.ratelimit import RateLimiter
from tdx_relay.tokens import TokenSigner


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_gate(
    clock,
    rate=10.0,
    burst=10.0,
    fault_burst=20.0,
    fault_rate=1.0,
    request_burst=1000.0,
    request_rate=1000.0,
    bypass_threshold=0.0,
    load_window=10.0,
    **admitter_kwargs,
):
    signer = TokenSigner(b"gate-test-key", clock=clock)
    admitter = Admitter(signer, rate=rate, burst=burst, clock=clock, **admitter_kwargs)
    faults = RateLimiter(burst=fault_burst, rate=fault_rate, clock=clock)
    throughput = RateLimiter(burst=request_burst, rate=request_rate, clock=clock)
    meter = RateMeter(window=load_window, clock=clock)
    return Gate(admitter, faults, throughput, meter, bypass_threshold=bypass_threshold)


# -- the queue path ------------------------------------------------------


def test_first_caller_is_served_at_once_and_keeps_the_new_token():
    decision = make_gate(Clock(), rate=1.0, burst=1.0).admit(None, "1.1.1.1")
    assert decision.admitted is True
    assert decision.new_token is True
    assert decision.token


def test_caller_past_the_rate_is_queued_with_a_position_and_a_padded_wait():
    gate = make_gate(Clock(), rate=1.0, burst=1.0)
    gate.admit(None, "1.1.1.1")

    decision = gate.admit(None, "2.2.2.2")
    assert decision.admitted is False
    payload = decision.payload
    assert payload["status"] == "queued"
    assert payload["new_token"] is True
    assert payload["queue_position"] == 1
    assert payload["estimated_wait_seconds"] == 1.0
    assert payload["retry_after_seconds"] == 1.2      # padded, so obeying it is safe
    assert payload["token"] == decision.token


def test_the_same_token_is_served_once_its_slot_arrives():
    clock = Clock()
    gate = make_gate(clock, rate=1.0, burst=1.0)
    gate.admit(None, "1.1.1.1")
    token = gate.admit(None, "2.2.2.2").token

    assert gate.admit(token, "2.2.2.2").admitted is False
    clock.advance(1.2)
    served = gate.admit(token, "2.2.2.2")
    assert served.admitted is True
    assert served.payload is None


def test_a_served_call_hands_back_a_token_with_a_fresh_window():
    clock = Clock()
    gate = make_gate(clock, window=60.0)
    token = gate.admit(None, "1.1.1.1").token
    clock.advance(59.0)

    renewed = gate.admit(token, "1.1.1.1").token
    assert renewed != token
    clock.advance(59.0)
    assert gate.admit(renewed, "1.1.1.1").admitted is True   # the old one would be dead


def test_a_caller_that_comes_back_late_is_charged_and_re_queued():
    clock = Clock()
    gate = make_gate(clock, rate=1.0, burst=1.0, window=5.0, fault_burst=5, fault_rate=0.0001)
    gate.admit(None, "1.1.1.1")                  # someone else holds the first slot
    token = gate.admit(None, "9.9.9.9").token    # one fault credit for the token

    clock.advance(30.0)                          # the window came and went
    for _ in range(3):
        gate.admitter.issue()                    # and the line filled up again

    late = gate.admit(token, "9.9.9.9")
    assert late.admitted is False
    assert late.payload["status"] == "queued"
    assert "window" in late.payload["reason"]
    assert late.payload["token"] != token        # a new place in line
    assert gate.faults.peek("9.9.9.9") == pytest.approx(3, abs=0.01)  # charged like any fault


def test_a_wait_past_the_horizon_is_refused_as_busy():
    gate = make_gate(Clock(), rate=1.0, burst=1.0, max_wait=3.0, fault_burst=1000)
    for _ in range(4):
        gate.admit(None, "1.1.1.1")
    assert gate.admit(None, "1.1.1.1").payload["status"] == "busy"


# -- what the IP buckets charge -----------------------------------------


def test_waiting_as_told_costs_one_credit_for_the_whole_session():
    clock = Clock()
    gate = make_gate(clock, rate=1.0, burst=1.0, window=30.0, fault_burst=2, fault_rate=0.0001)
    gate.admit(None, "1.1.1.1")                 # someone else takes the first slot
    token = gate.admit(None, "9.9.9.9").token   # one credit for the token

    for _ in range(50):
        clock.advance(2.0)
        decision = gate.admit(token, "9.9.9.9")
        assert decision.admitted is True
        token = decision.token                  # each served call renews the window
    assert gate.faults.peek("9.9.9.9") >= 1     # a patient caller is barely charged


def test_polling_early_is_charged_and_eventually_refused():
    clock = Clock()
    gate = make_gate(clock, rate=0.2, burst=1.0, fault_burst=5, fault_rate=0.0001)
    gate.admit(None, "1.1.1.1")
    token = gate.admit(None, "9.9.9.9").token   # 1 of 5 credits

    for _ in range(4):                          # four early polls spend the rest
        assert gate.admit(token, "9.9.9.9").payload["status"] == "queued"

    refused = gate.admit(token, "9.9.9.9")
    assert refused.payload["status"] == "rate_limited"
    assert refused.payload["token"] == token    # its place in line is not lost


def test_a_rubbish_token_is_charged_like_a_token_request():
    gate = make_gate(Clock(), rate=1.0, burst=1.0, fault_burst=2, fault_rate=0.0001)
    gate.admit(None, "1.1.1.1")
    gate.admit("t1.0.forged.forged", "9.9.9.9")
    gate.admit("t1.0.forged.forged", "9.9.9.9")
    assert gate.admit("t1.0.forged.forged", "9.9.9.9").payload["status"] == "rate_limited"


def test_a_valid_token_replayed_hard_is_stopped_by_the_throughput_bucket():
    clock = Clock()
    gate = make_gate(clock, request_burst=10, request_rate=0.0001, fault_burst=1000)
    token = gate.admit(None, "9.9.9.9").token

    served = sum(gate.admit(token, "9.9.9.9").admitted for _ in range(50))
    assert served == 9      # the call that fetched the token spent the tenth
    assert gate.admit(token, "9.9.9.9").payload["status"] == "rate_limited"


def test_the_buckets_are_per_ip():
    gate = make_gate(Clock(), rate=1.0, burst=1.0, fault_burst=1, fault_rate=0.0001)
    gate.admit(None, "1.1.1.1")
    gate.admit(None, "9.9.9.9")
    assert gate.admit(None, "9.9.9.9").payload["status"] == "rate_limited"
    assert gate.admit(None, "2.2.2.2").payload["status"] == "queued"


# -- bypass while the relay is quiet ------------------------------------


def test_quiet_relay_serves_without_a_token_at_all():
    clock = Clock()
    gate = make_gate(clock, rate=1.0, burst=1.0, bypass_threshold=5.0)
    for _ in range(5):
        decision = gate.admit(None, "1.1.1.1")
        assert (decision.admitted, decision.bypassed, decision.token) == (True, True, None)
        clock.advance(2.0)


def test_bypass_stops_once_the_moving_average_passes_the_threshold():
    clock = Clock()
    gate = make_gate(clock, rate=1.0, burst=1.0, bypass_threshold=5.0, fault_burst=1000)
    bypassed = 0
    for _ in range(200):     # 20 requests per second
        bypassed += gate.admit(None, "1.1.1.1").bypassed
        clock.advance(0.05)
    assert 0 < bypassed < 200
    assert gate.admit(None, "1.1.1.1").bypassed is False
    assert gate.load() > 5.0


def test_bypass_resumes_once_the_burst_has_faded():
    clock = Clock()
    gate = make_gate(clock, rate=1.0, burst=1.0, bypass_threshold=5.0, fault_burst=1000)
    for _ in range(200):
        gate.admit(None, "1.1.1.1")
        clock.advance(0.05)
    assert gate.admit(None, "1.1.1.1").bypassed is False

    clock.advance(600.0)     # traffic stops: the line drains, the average decays
    assert gate.admit(None, "1.1.1.1").bypassed is True


def test_waiting_callers_veto_the_bypass():
    clock = Clock()
    gate = make_gate(clock, rate=1.0, burst=1.0, bypass_threshold=1000.0, fault_burst=1000)
    for _ in range(3):
        gate.admitter.issue()    # a crowd that arrived before things went quiet
    assert gate.admitter.waiting() >= 2

    newcomer = gate.admit(None, "3.3.3.3")
    assert newcomer.bypassed is False
    assert newcomer.admitted is False
    assert newcomer.payload["queue_position"] == 3


def test_a_bypassed_call_costs_no_fault_credit():
    clock = Clock()
    gate = make_gate(clock, bypass_threshold=1000.0, fault_burst=2, fault_rate=0.0001)
    for _ in range(20):
        assert gate.admit(None, "1.1.1.1").bypassed is True
    assert gate.faults.peek("1.1.1.1") == 2


def test_bypass_is_off_when_the_threshold_is_zero():
    assert make_gate(Clock()).admit(None, "1.1.1.1").bypassed is False


def test_a_held_token_still_works_once_the_relay_goes_quiet_again():
    clock = Clock()
    gate = make_gate(clock, rate=1.0, burst=1.0, bypass_threshold=5.0, fault_burst=1000)
    gate.admitter.issue()
    token = gate.admitter.issue().token    # a caller that had to queue
    clock.advance(5.0)

    served = gate.admit(token, "2.2.2.2")
    assert served.admitted is True
    assert served.bypassed is False
    assert served.token                     # renewed, not discarded
