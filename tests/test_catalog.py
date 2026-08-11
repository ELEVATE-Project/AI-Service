"""Unit tests for the model/provider discovery catalog."""
from __future__ import annotations

import pytest

from src.llm_service.providers.catalog import _openrouter_mode, known_modes


@pytest.mark.parametrize("modality,expected", [
    ("text->text", "chat"),
    ("text+image->text", "chat"),
    ("text+image->text+image", "chat"),
    ("text+audio->text+audio", "chat"),
    ("text->embeddings", "embedding"),
    ("text->speech", "audio_speech"),
    ("audio->transcription", "audio_transcription"),
    ("text->rerank", "rerank"),
    ("text+image->image", "image_generation"),
    ("text+image->video", "video_generation"),
])
def test_openrouter_mode_maps_known_modalities(modality: str, expected: str) -> None:
    assert _openrouter_mode(modality) == expected


@pytest.mark.parametrize("modality", ["", "weird->unknown", "no-arrow-here"])
def test_openrouter_mode_unsupported_returns_none(modality: str) -> None:
    assert _openrouter_mode(modality) is None


def test_known_modes_excludes_sample_spec_placeholder() -> None:
    modes = known_modes()
    assert "one of: chat, embedding, completion, image_generation, audio_transcription, audio_speech, image_generation, moderation, rerank, search" not in modes
    assert "chat" in modes
    assert "embedding" in modes
