import base64
import hashlib
import hmac
from uuid import uuid4

import pytest

from os_sol_checker.application.sessions import csrf_token, token_hash, valid_csrf, validate_profile
from os_sol_checker.domain.errors import CheckerError
from os_sol_checker.web.middleware import LocalRateLimiter


def test_csrf_is_stable_domain_separated_hmac_of_internal_identity():
    identity = uuid4()
    expected = base64.urlsafe_b64encode(hmac.digest(b"secret", f"checker-csrf-v1:{identity}".encode(), "sha256")).rstrip(b"=").decode()
    assert csrf_token("secret", identity) == expected
    assert valid_csrf("secret", identity, expected)
    assert not valid_csrf("secret", uuid4(), expected)
    assert not valid_csrf("secret", identity, "\u0430" * 43)
    assert token_hash("a" * 43) == hashlib.sha256(b"a" * 43).hexdigest()


@pytest.mark.parametrize("value", [None, [], {"name": "x", "group": "g", "extra": 1},
    {"name": 1, "group": "g"}, {"name": " ", "group": "g"},
    {"name": "x" * 121, "group": "g"}, {"name": "x", "group": "g" * 65},
    {"name": "x\u202ey", "group": "g"}, {"name": "x", "group": "g\n"},
    {"name": "\ud800", "group": "g"}])
def test_invalid_profile_rejected(value):
    with pytest.raises(CheckerError) as error:
        validate_profile(value)
    assert error.value.code == "invalid_profile"


def test_profile_is_unicode_and_trimmed():
    assert validate_profile({"name": "  Иван 李  ", "group": "  ИУ-12  "}) == ("Иван 李", "ИУ-12")


def test_process_local_rate_windows_are_finite_and_do_not_evict_active_keys():
    now = [0.0]
    limiter = LocalRateLimiter(max_keys=2, clock=lambda: now[0])
    limiter.check("a", 2)
    limiter.check("a", 2)
    with pytest.raises(CheckerError) as error:
        limiter.check("a", 2)
    assert error.value.status_code == 429
    limiter.check("b", 2)
    with pytest.raises(CheckerError):
        limiter.check("c", 2)
    now[0] = 61
    limiter.check("c", 2)
    assert len(limiter.windows) == 1
