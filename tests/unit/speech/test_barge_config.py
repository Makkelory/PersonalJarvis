"""[voice.barge]: how easily the owner can interrupt Jarvis (fork addition)."""
from __future__ import annotations

import pytest

from jarvis.speech import barge_config


@pytest.fixture
def toml_path(tmp_path, monkeypatch):
    import jarvis.core.config as cfg

    path = tmp_path / "jarvis.toml"
    monkeypatch.setattr(cfg, "resolve_config_path", lambda: path)
    monkeypatch.setattr(barge_config, "_cache", None)
    return path


def test_without_a_table_the_detector_keeps_its_defaults(toml_path) -> None:
    assert barge_config.current().detector_kwargs() == {}  # no file
    toml_path.write_text("[voice]\nmode = 'pipeline'\n", encoding="utf-8")
    barge_config._cache = None
    assert barge_config.current().detector_kwargs() == {}


def test_the_table_maps_to_the_detectors_parameter_names_and_is_clamped(toml_path) -> None:
    toml_path.write_text("[voice.barge]\ngrace_s = 0.7\nspeech_threshold = 0.9\nconsecutive_frames = 8\n"
                         "echo_correlation = 0.85\nfloor_margin = 9\n", encoding="utf-8")

    kwargs = barge_config.current().detector_kwargs()

    assert kwargs == {"grace_s": 0.7, "speech_threshold": 0.9, "consecutive_frames": 8,
                      "echo_correlation_threshold": 0.85, "adaptive_floor_margin": 3.0}


def test_one_set_knob_leaves_the_others_alone(toml_path) -> None:
    toml_path.write_text("[voice.barge]\ngrace_s = 0.5\n", encoding="utf-8")

    assert barge_config.current().detector_kwargs() == {"grace_s": 0.5}
