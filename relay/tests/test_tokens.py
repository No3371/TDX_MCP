from tdx_relay.tokens import TokenSigner


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_round_trip():
    clock = Clock()
    signer = TokenSigner(b"secret", clock=clock)
    token = signer.sign(admit_at=clock.now + 3.5, expires_at=clock.now + 3600)
    claims = signer.verify(token)
    assert claims.admit_at == clock.now + 3.5
    assert claims.expires_at == clock.now + 3600
    assert claims.nonce


def test_token_is_compact_and_url_safe():
    signer = TokenSigner(b"secret")
    token = signer.sign(1.0, 2.0)
    assert len(token) < 80
    assert all(c.isalnum() or c in "-_." for c in token)


def test_rejects_rubbish():
    signer = TokenSigner(b"secret")
    for bad in [None, "", "nope", "t1.0.x", "t1.0.x.y.z", 7]:
        assert signer.verify(bad) is None


def test_rejects_a_tampered_payload():
    signer = TokenSigner(b"secret")
    prefix, key_id, payload, mac = signer.sign(1.0, 10_000_000_000.0).split(".")
    forged = signer.sign(0.0, 10_000_000_000.0).split(".")[2]
    assert signer.verify(f"{prefix}.{key_id}.{forged}.{mac}") is None


def test_rejects_another_signers_token():
    mine = TokenSigner(b"secret-a")
    theirs = TokenSigner(b"secret-b")
    assert mine.verify(theirs.sign(1.0, 10_000_000_000.0)) is None


def test_rejects_an_expired_token():
    clock = Clock()
    signer = TokenSigner(b"secret", clock=clock)
    token = signer.sign(clock.now, clock.now + 60)
    assert signer.verify(token) is not None
    clock.advance(61)
    assert signer.verify(token) is None


def test_a_retired_key_still_verifies_until_it_is_dropped():
    old = TokenSigner(b"old-key", key_id="1")
    token = old.sign(1.0, 10_000_000_000.0)

    rotated = TokenSigner(b"new-key", key_id="2", retired={"1": b"old-key"})
    assert rotated.verify(token) is not None
    assert rotated.sign(1.0, 2.0).split(".")[1] == "2"

    dropped = TokenSigner(b"new-key", key_id="2")
    assert dropped.verify(token) is None
