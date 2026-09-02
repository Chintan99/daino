"""Estimating tokens from what providers actually charge, not from a constant.

`len(text) // 4` read 19,025 tokens for a request one provider billed at about
45,000. Compaction therefore fired at a fraction of the real capacity, threw away
context the agent immediately re-read, and did it 52 times in a single turn.
These tests pin the correction and, more importantly, its safety bounds: the
estimator may be wrong, but it must not be wrong in the direction that overflows
a context window.
"""

from __future__ import annotations

import pytest

from daino.agents.tokens import (
    DEFAULT_CHARS_PER_TOKEN,
    MAX_CHARS_PER_TOKEN,
    MIN_CHARS_PER_TOKEN,
    TokenCalibration,
    estimate_message,
)
from daino.schemas import Message


@pytest.fixture
def calibration() -> TokenCalibration:
    return TokenCalibration()


def test_the_default_holds_until_a_provider_has_reported_anything(
    calibration: TokenCalibration,
) -> None:
    """A fresh process must behave exactly as it always did."""
    assert calibration.chars_per_token("gpt-x") == DEFAULT_CHARS_PER_TOKEN


def test_it_learns_the_density_of_this_workload(calibration: TokenCalibration) -> None:
    """The real ratio for JSON and code is nearer 2.5 than 4."""
    for _ in range(6):
        calibration.observe("gpt-x", chars=100_000, tokens=40_000)

    assert calibration.chars_per_token("gpt-x") == pytest.approx(2.5, abs=0.1)


def test_denser_than_expected_is_believed_immediately(
    calibration: TokenCalibration,
) -> None:
    """Under-estimating overflows the window, so that correction cannot wait."""
    calibration.observe("gpt-x", chars=100_000, tokens=25_000)  # ratio 4.0
    calibration.observe("gpt-x", chars=100_000, tokens=50_000)  # ratio 2.0

    # More than halfway to the new, tighter reading after a single observation.
    assert calibration.chars_per_token("gpt-x") < 3.0


def test_sparser_than_expected_is_believed_slowly(calibration: TokenCalibration) -> None:
    """Relaxing costs nothing to delay and everything to get wrong."""
    calibration.observe("gpt-x", chars=100_000, tokens=50_000)  # ratio 2.0
    calibration.observe("gpt-x", chars=100_000, tokens=25_000)  # ratio 4.0

    assert calibration.chars_per_token("gpt-x") < 2.5


def test_an_absurd_report_cannot_move_the_estimate_out_of_bounds(
    calibration: TokenCalibration,
) -> None:
    """A malformed usage field must not make the transcript look free."""
    calibration.observe("gpt-x", chars=100_000, tokens=1)
    assert calibration.chars_per_token("gpt-x") <= MAX_CHARS_PER_TOKEN

    calibration.reset()
    calibration.observe("gpt-y", chars=10_000, tokens=1_000_000)
    assert calibration.chars_per_token("gpt-y") >= MIN_CHARS_PER_TOKEN


def test_a_tiny_request_teaches_nothing(calibration: TokenCalibration) -> None:
    """Most of a small request is fixed overhead, which says nothing about text."""
    calibration.observe("gpt-x", chars=200, tokens=400)

    assert calibration.chars_per_token("gpt-x") == DEFAULT_CHARS_PER_TOKEN


def test_models_are_calibrated_separately(calibration: TokenCalibration) -> None:
    calibration.observe("dense-model", chars=100_000, tokens=50_000)

    assert calibration.chars_per_token("dense-model") < DEFAULT_CHARS_PER_TOKEN
    assert calibration.chars_per_token("other-model") == DEFAULT_CHARS_PER_TOKEN


def test_an_estimate_counts_tool_calls_as_well_as_content() -> None:
    """A tool call is tokens on the wire even when the content is empty."""
    from daino.schemas import ToolCall

    plain = Message(role="assistant", content="")
    with_call = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="1", name="read_file", arguments={"path": "a/b/c.py"})],
    )

    assert estimate_message(with_call) > estimate_message(plain)
