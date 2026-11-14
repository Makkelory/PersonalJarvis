"""barge_config: how easily the owner can interrupt Jarvis by speaking over him.

Fork addition (2026-11-14). The barge-in detector (``jarvis.realtime.desktop``) is tuned very
conservatively so that Jarvis' own voice coming back through open speakers never counts as the
owner. With speakers plus a desk microphone that is too strict: the log showed "Barge-in candidate
suppressed as speaker echo (envelope correlation 0.75)" while the owner was really speaking.

``[voice.barge]`` in jarvis.toml overrides the detector's knobs (read when a turn starts, so a
change applies from the next answer, no restart)::

    [voice.barge]
    grace_s = 0.7              # seconds after the answer starts before an interruption may count (default 1.5)
    speech_threshold = 0.9     # Silero speech probability (default 0.97)
    consecutive_frames = 8     # sustained speech frames, 32 ms each (default 12)
    echo_correlation = 0.85    # candidates tracking the output envelope above this are called echo (default 0.70)
    floor_margin = 1.3         # factor over the loudest echo seen (default 1.4)

A missing key keeps the detector's default. A very low value makes Jarvis stop for his own echo.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BargeSettings:
    grace_s: float | None = None
    speech_threshold: float | None = None
    consecutive_frames: int | None = None
    echo_correlation: float | None = None
    floor_margin: float | None = None

    def detector_kwargs(self) -> dict[str, float | int]:
        """Only the knobs the owner set, under the detector's own parameter names."""
        pairs = {
            "grace_s": self.grace_s,
            "speech_threshold": self.speech_threshold,
            "consecutive_frames": self.consecutive_frames,
            "echo_correlation_threshold": self.echo_correlation,
            "adaptive_floor_margin": self.floor_margin,
        }
        return {k: v for k, v in pairs.items() if v is not None}


_cache: tuple[float, BargeSettings] | None = None


def _number(table: dict, key: str, low: float, high: float) -> float | None:
    if key not in table:
        return None
    try:
        return max(low, min(high, float(table[key])))
    except (TypeError, ValueError):
        return None


def current() -> BargeSettings:
    """``[voice.barge]`` of the active jarvis.toml, re-read when the file changes."""
    global _cache  # noqa: PLW0603
    try:
        from jarvis.core.config import resolve_config_path  # noqa: PLC0415

        path = resolve_config_path()
        mtime = os.path.getmtime(path)
    except Exception:  # noqa: BLE001 - no file: the detector keeps its defaults
        return BargeSettings()
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    settings = BargeSettings()
    try:
        with open(path, "rb") as fh:
            table = tomllib.load(fh).get("voice", {}).get("barge", {})
        if isinstance(table, dict) and table:
            frames = _number(table, "consecutive_frames", 3, 30)
            settings = BargeSettings(
                grace_s=_number(table, "grace_s", 0.2, 5.0),
                speech_threshold=_number(table, "speech_threshold", 0.5, 0.99),
                consecutive_frames=int(frames) if frames is not None else None,
                echo_correlation=_number(table, "echo_correlation", 0.3, 1.0),
                floor_margin=_number(table, "floor_margin", 1.0, 3.0),
            )
    except Exception:  # noqa: BLE001 - a broken file must not disable interruption
        log.warning("barge_config: could not read [voice.barge]", exc_info=True)
    _cache = (mtime, settings)
    return settings
