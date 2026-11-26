"""Unit tests for merging streamed OpenRouter reasoning_details fragments."""
from __future__ import annotations

import litellm  # noqa: F401 — importing this appends the repo root to sys.path, required for `from src...` below

from src.llm_service.providers.litellm import _merge_reasoning_details


def _merge(*fragments: dict) -> list[dict]:
    blocks: list[dict] = []
    for fragment in fragments:
        _merge_reasoning_details(blocks, [fragment])
    return blocks


def test_null_signature_then_string_fragment_merges_to_string() -> None:
    blocks = _merge(
        {"type": "reasoning.text", "index": 0, "text": "a", "signature": None},
        {"index": 0, "signature": "sig"},
    )
    assert blocks == [{"type": "reasoning.text", "index": 0, "text": "a", "signature": "sig"}]


def test_string_then_none_keeps_the_string() -> None:
    blocks = _merge(
        {"type": "reasoning.text", "index": 0, "text": "a", "signature": "sig"},
        {"index": 0, "text": None, "signature": None},
    )
    assert blocks == [{"type": "reasoning.text", "index": 0, "text": "a", "signature": "sig"}]


def test_text_fragments_with_same_index_and_type_still_merge() -> None:
    blocks = _merge(
        {"type": "reasoning.text", "index": 0, "text": "a"},
        {"type": "reasoning.text", "index": 0, "text": "b"},
        {"index": 0, "text": "c"},
    )
    assert blocks == [{"type": "reasoning.text", "index": 0, "text": "abc"}]


def test_different_types_with_same_index_stay_separate_blocks() -> None:
    summary = {"type": "reasoning.summary", "index": 0, "summary": "why"}
    encrypted = {"type": "reasoning.encrypted", "index": 0, "data": "xyz"}
    assert _merge(summary, encrypted) == [summary, encrypted]


def test_different_ids_with_same_index_stay_separate_blocks() -> None:
    first = {"type": "reasoning.encrypted", "id": "r1", "index": 0, "data": "aa"}
    second = {"type": "reasoning.encrypted", "id": "r2", "index": 0, "data": "bb"}
    assert _merge(first, second) == [first, second]


def test_same_id_and_index_merge() -> None:
    blocks = _merge(
        {"type": "reasoning.encrypted", "id": "r1", "index": 0, "data": "aa"},
        {"type": "reasoning.encrypted", "id": "r1", "index": 0, "data": "bb"},
    )
    assert blocks == [{"type": "reasoning.encrypted", "id": "r1", "index": 0, "data": "aabb"}]


def test_none_in_a_later_fragment_never_erases_stored_metadata() -> None:
    # OpenRouter sends explicit nulls. A null must not wipe id, format, type or index.
    blocks = _merge(
        {"type": "reasoning.encrypted", "index": 1, "id": "r1", "format": "f", "data": "a"},
        {"index": 1, "id": None, "type": None, "format": None, "data": "b"},
    )
    assert blocks == [{"type": "reasoning.encrypted", "index": 1, "id": "r1", "format": "f", "data": "ab"}]


def test_a_later_value_fills_a_key_that_started_as_none() -> None:
    blocks = _merge(
        {"type": "reasoning.text", "index": 0, "format": None, "text": "a"},
        {"index": 0, "format": "anthropic-claude-v1", "text": "b"},
    )
    assert blocks == [{"type": "reasoning.text", "index": 0, "format": "anthropic-claude-v1", "text": "ab"}]
