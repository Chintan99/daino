"""How many tokens a message is, learned from what providers actually charge.

Every budget in the agent — when to compact, how much repository grounding to
inline, whether a transcript still fits — is computed from an estimate, and the
estimate was ``len(text) // 4``. That constant is roughly right for English
prose and badly wrong for what this agent actually sends: JSON tool
observations, source code, diffs. In one measured turn it read 19,025 tokens for
a request the provider billed at about 45,000.

Both directions of that error cost real money. Too optimistic and the loop
compacts far too late, overflowing the window and failing the turn. Too
pessimistic — which is what happened — and it compacts at a fraction of the real
capacity, throwing away context the agent immediately re-reads, refilling the
window, and compacting again: 52 times in that same turn.

So stop guessing. Every response carries the provider's own count of the input
it just charged for. Comparing that with the characters that were sent gives a
chars-per-token ratio for that model on this workload, and a few calls in, the
estimate is grounded in fact rather than in a constant.

Two deliberate asymmetries:

* **The ratio ratchets down fast and up slowly.** Discovering that text is
  denser than assumed is a safety correction and is applied immediately;
  discovering it is sparser only relaxes the estimate gradually. The failure
  from being too generous is a failed turn; from being too cautious, a slightly
  early compaction.
* **It is bounded.** A malformed usage report cannot drive the estimator to a
  ratio that makes the whole transcript look free, or so dense that everything
  compacts on the first step.
"""

from __future__ import annotations

import json
import threading

from daino.schemas import Message

#: What to assume before a provider has reported anything. The historical
#: constant, kept so behaviour on the first call of a fresh process is unchanged.
DEFAULT_CHARS_PER_TOKEN = 4.0

#: Nothing real is denser than this; below it, an estimate stops being useful and
#: starts making every step look like it needs compaction.
MIN_CHARS_PER_TOKEN = 1.5

#: Nor sparser. Prose tokenizes near 4; anything claiming much more is a bad
#: usage report, not a discovery.
MAX_CHARS_PER_TOKEN = 6.0

#: Weight given to an observation that says text is *denser* than assumed. High,
#: because under-estimating is the error that fails turns.
TIGHTEN_WEIGHT = 0.6

#: Weight for an observation that says text is sparser. Low, so the estimator
#: relaxes only on sustained evidence.
RELAX_WEIGHT = 0.15

#: Ignore reports below this many characters: a tiny request is mostly fixed
#: per-request overhead, and its ratio says nothing about a transcript.
MIN_SAMPLE_CHARS = 2_000


class TokenCalibration:
    """Per-model chars-per-token, learned from reported usage.

    Process-wide rather than per-gateway: a gateway is rebuilt for every pinned
    profile and every turn, and a calibration that reset that often would spend
    its life at the default it exists to replace.
    """

    def __init__(self) -> None:
        self._ratios: dict[str, float] = {}
        self._lock = threading.Lock()

    def chars_per_token(self, model: str = "") -> float:
        with self._lock:
            return self._ratios.get(model, DEFAULT_CHARS_PER_TOKEN)

    def observe(self, model: str, chars: int, tokens: int) -> None:
        """Fold one provider-reported input count into the model's ratio."""
        if not model or chars < MIN_SAMPLE_CHARS or tokens <= 0:
            return
        observed = _bounded(chars / tokens)
        with self._lock:
            current = self._ratios.get(model)
            if current is None:
                self._ratios[model] = observed
                return
            weight = TIGHTEN_WEIGHT if observed < current else RELAX_WEIGHT
            self._ratios[model] = _bounded(current + weight * (observed - current))

    def reset(self) -> None:
        """Forget everything learned. For tests, and for a changed model."""
        with self._lock:
            self._ratios.clear()


#: The shared instance. Imported rather than injected because the estimate has
#: to be reachable from the loop, the gateway and the context builder alike,
#: none of which otherwise share an object.
CALIBRATION = TokenCalibration()


def _bounded(ratio: float) -> float:
    return min(MAX_CHARS_PER_TOKEN, max(MIN_CHARS_PER_TOKEN, ratio))


def message_chars(message: Message) -> int:
    """Characters this message contributes to a request, tool calls included."""
    if not message.tool_calls:
        return len(message.content)
    encoded = json.dumps(
        [item.model_dump(mode="json") for item in message.tool_calls],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return len(message.content) + len(encoded)


def estimate_message(message: Message, model: str = "") -> int:
    """Estimate one message, with the per-request overhead every message carries."""
    ratio = CALIBRATION.chars_per_token(model)
    return max(1, int(message_chars(message) / ratio)) + 8


def estimate_messages(messages: list[Message], model: str = "") -> int:
    return sum(estimate_message(item, model) for item in messages)


def estimate_text(text: str, model: str = "") -> int:
    return max(1, int(len(text) / CALIBRATION.chars_per_token(model)))
