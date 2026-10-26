"""Bounded, loss-aware map/reduce of development transcripts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pepsicode.cancellation import TurnCancelledError, raise_if_cancelled

SUMMARY_INPUT_TOKENS = 3_000
SUMMARY_OUTPUT_TOKENS = 1_500
SUMMARY_MAX_CALLS = 64


class SummaryError(ValueError):
    """A checkpoint could not be produced without silently dropping input."""


@dataclass(frozen=True)
class SummaryResult:
    text: str
    calls: int
    chunks: int


def split_by_tokens(text: str, budget: int, estimate: Callable[[str], int]) -> list[str]:
    """Split every character, including long single lines, into bounded inputs."""
    if budget < 1:
        raise ValueError("Token budget must be positive")
    chunks = []
    offset = 0
    while offset < len(text):
        low, high = 1, min(len(text) - offset, budget * 4)
        while low < high:
            middle = (low + high + 1) // 2
            if estimate(text[offset : offset + middle]) <= budget:
                low = middle
            else:
                high = middle - 1
        piece = text[offset : offset + low]
        if estimate(piece) > budget:
            raise SummaryError("A character exceeds the summary input budget")
        chunks.append(piece)
        offset += low
    return chunks


def summarize_transcript(
    transcript: str,
    *,
    previous: str,
    summarize: Callable[[str], str],
    estimate: Callable[[str], int],
    max_chars: int,
    max_tokens: int = SUMMARY_OUTPUT_TOKENS,
    max_calls: int = SUMMARY_MAX_CALLS,
) -> SummaryResult:
    """Visit all input, then reduce summaries; never slice a checkpoint to fit."""
    source = f"[Previous checkpoint]\n{previous}\n\n[New transcript]\n{transcript}"
    calls = 0
    first_chunks = 0
    for reduction in range(6):
        chunks = split_by_tokens(source, SUMMARY_INPUT_TOKENS - 150, estimate)
        if not reduction:
            first_chunks = len(chunks)
        if calls + len(chunks) > max_calls:
            raise SummaryError("Summary call budget exhausted; original context retained")
        results = []
        for index, chunk in enumerate(chunks, 1):
            raise_if_cancelled()
            try:
                result = summarize(f"[Checkpoint part {index}/{len(chunks)}; reduction {reduction}]\n{chunk}")
            except TurnCancelledError:
                raise
            except Exception as error:
                raise SummaryError("Summary request failed; original context retained") from error
            calls += 1
            if not isinstance(result, str) or not result.strip():
                raise SummaryError("Summary was empty or incomplete; original context retained")
            results.append(result.strip())
        combined = "\n\n".join(dict.fromkeys(results))
        if len(combined) <= max_chars and estimate(combined) <= max_tokens:
            return SummaryResult(combined, calls, first_chunks)
        if estimate(combined) >= estimate(source):
            raise SummaryError("Summary made no progress; original context retained")
        source = "[Merge all checkpoint parts; preserve constraints and current status]\n" + combined
    raise SummaryError("Summary reduction limit reached; original context retained")
