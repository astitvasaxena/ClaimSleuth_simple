"""The ADK side's A2A wait is explicit, configurable and bounded."""
import pytest

from claimsleuth.adk_service.forensic_a2a import A2A_TIMEOUT_SECONDS, a2a_timeout


def test_default_and_environment(monkeypatch):
    monkeypatch.delenv("CLAIMSLEUTH_A2A_TIMEOUT", raising=False)
    assert a2a_timeout() == A2A_TIMEOUT_SECONDS == 180.0
    monkeypatch.setenv("CLAIMSLEUTH_A2A_TIMEOUT", "1200")
    assert a2a_timeout() == 1200.0
    assert a2a_timeout(300) == 300.0                  # an explicit argument wins


@pytest.mark.parametrize("value", [5, 0, -1, 7200])
def test_out_of_range_values_are_rejected(value):
    with pytest.raises(ValueError, match="outside"):
        a2a_timeout(value)


def test_non_numeric_environment_value_is_rejected(monkeypatch):
    monkeypatch.setenv("CLAIMSLEUTH_A2A_TIMEOUT", "forever")
    with pytest.raises(ValueError):
        a2a_timeout()
