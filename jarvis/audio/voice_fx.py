"""voice_fx: a calm, clean, slightly digital "assistant" colour for synthesized speech.

Fork addition (2026-11-14). The film Jarvis is a clear, composed voice with a touch
of processing. This module does not imitate anybody's voice: it only shapes the
voice that the TTS already produced, in four small steps that work on a stream:

1. clean-up   high-pass at 90 Hz (no rumble)
2. tone       warmth (low shelf), presence (peak at 3 kHz), air (high shelf)
3. sheen      a very light, slowly moving doubling (a digital shimmer)
4. space      three short room reflections plus a small echo tail, then a soft limiter

``strength`` (0..1) scales everything at once; 0 leaves the voice untouched.
Settings are read from ``[tts.fx]`` in jarvis.toml and re-read when the file
changes, so the colour can be tuned by ear without restarting the app::

    [tts.fx]
    preset = "jarvis"   # "off" to disable
    strength = 0.6      # 0.0 .. 1.0
    pitch = -1.5        # semitones, -6 .. +3 (works even with preset = "off")

State is carried from chunk to chunk, so processing never clicks at chunk joins.
"""

from __future__ import annotations

import logging
import math
import os
import tomllib
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.signal import sosfilt, sosfilt_zi

log = logging.getLogger(__name__)

_TAIL_S = 0.32
_MAX_STRENGTH = 1.0


@dataclass(frozen=True)
class FxSettings:
    preset: str = "off"
    strength: float = 0.6
    pitch: float = 0.0  # semitones; negative = deeper voice, same speed

    @property
    def active(self) -> bool:
        return self.preset.lower() not in {"", "off", "none", "false"} and self.strength > 0.0


# --------------------------------------------------------------- settings
_cache: tuple[float, FxSettings] | None = None


def current_settings() -> FxSettings:
    """``[tts.fx]`` from the active jarvis.toml; re-read only when the file's mtime changes."""
    global _cache  # noqa: PLW0603
    try:
        from jarvis.core.config import resolve_config_path  # noqa: PLC0415

        path = resolve_config_path()
        mtime = os.path.getmtime(path)
    except Exception:  # noqa: BLE001 - no config file: no effect
        return FxSettings()
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    settings = FxSettings()
    try:
        with open(path, "rb") as fh:
            table = tomllib.load(fh).get("tts", {}).get("fx", {})
        if isinstance(table, dict):
            strength = float(table.get("strength", 0.6))
            pitch = max(-6.0, min(3.0, float(table.get("pitch", 0.0))))
            settings = FxSettings(str(table.get("preset", "off")), max(0.0, min(_MAX_STRENGTH, strength)), pitch)
    except Exception:  # noqa: BLE001 - a broken file must not silence the voice
        log.warning("voice_fx: could not read [tts.fx]", exc_info=True)
    _cache = (mtime, settings)
    return settings


def for_turn() -> VoiceFx | None:
    """A fresh processor for one utterance, or None when the effect is off."""
    settings = current_settings()
    return VoiceFx(settings) if settings.active else None


# --------------------------------------------------------------- biquads (RBJ cookbook)
def _biquad(kind: str, fs: float, f0: float, gain_db: float = 0.0, q: float = 0.707) -> np.ndarray:
    a_gain = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * min(f0, fs * 0.45) / fs
    cos_w, sin_w = math.cos(w0), math.sin(w0)
    alpha = sin_w / (2 * q)
    if kind == "highpass":
        b = [(1 + cos_w) / 2, -(1 + cos_w), (1 + cos_w) / 2]
        a = [1 + alpha, -2 * cos_w, 1 - alpha]
    elif kind == "peak":
        b = [1 + alpha * a_gain, -2 * cos_w, 1 - alpha * a_gain]
        a = [1 + alpha / a_gain, -2 * cos_w, 1 - alpha / a_gain]
    elif kind == "lowshelf":
        sq = 2 * math.sqrt(a_gain) * alpha
        b = [a_gain * ((a_gain + 1) - (a_gain - 1) * cos_w + sq), 2 * a_gain * ((a_gain - 1) - (a_gain + 1) * cos_w),
             a_gain * ((a_gain + 1) - (a_gain - 1) * cos_w - sq)]
        a = [(a_gain + 1) + (a_gain - 1) * cos_w + sq, -2 * ((a_gain - 1) + (a_gain + 1) * cos_w),
             (a_gain + 1) + (a_gain - 1) * cos_w - sq]
    elif kind == "highshelf":
        sq = 2 * math.sqrt(a_gain) * alpha
        b = [a_gain * ((a_gain + 1) + (a_gain - 1) * cos_w + sq), -2 * a_gain * ((a_gain - 1) + (a_gain + 1) * cos_w),
             a_gain * ((a_gain + 1) + (a_gain - 1) * cos_w - sq)]
        a = [(a_gain + 1) - (a_gain - 1) * cos_w + sq, 2 * ((a_gain - 1) - (a_gain + 1) * cos_w),
             (a_gain + 1) - (a_gain - 1) * cos_w - sq]
    else:  # pragma: no cover
        raise ValueError(kind)
    b0, a0 = b[0], a[0]
    return np.array([b[0] / a0, b[1] / a0, b[2] / a0, 1.0, a[1] / a0, a[2] / a0])


class VoiceFx:
    """Streaming processor: ``process(pcm16, rate)`` per chunk, ``flush()`` once at the end."""

    def __init__(self, settings: FxSettings) -> None:
        self.s = settings.strength
        self._rate = 0
        self._sos: np.ndarray | None = None
        self._zi: np.ndarray | None = None
        self._hist = np.zeros(0, dtype=np.float32)
        self._phase = 0.0
        self._echo = np.zeros(0, dtype=np.float32)

    # -- setup once the sample rate is known
    def _setup(self, rate: int) -> None:
        self._rate = rate
        s = self.s
        sections = [
            _biquad("highpass", rate, 90.0, q=0.707),
            _biquad("lowshelf", rate, 180.0, 2.5 * s),
            _biquad("peak", rate, 3000.0, 3.5 * s, q=0.9),
            _biquad("highshelf", rate, 7000.0, 2.0 * s),
        ]
        self._sos = np.vstack(sections)
        self._zi = np.zeros((len(sections), 2))
        for i in range(len(sections)):
            self._zi[i] = sosfilt_zi(self._sos[i:i + 1]) * 0.0
        self._max_delay = int(rate * 0.024)
        self._hist = np.zeros(self._max_delay + 4, dtype=np.float32)
        self._echo_len = int(rate * 0.053)
        self._echo = np.zeros(self._echo_len, dtype=np.float32)
        self._taps = [(int(rate * 0.009), 0.18 * s), (int(rate * 0.017), 0.12 * s), (int(rate * 0.029), 0.08 * s)]
        self._tap_hist = np.zeros(max(t for t, _g in self._taps) + 1, dtype=np.float32)

    def _shimmer(self, x: np.ndarray) -> np.ndarray:
        """Slowly moving doubled copy (12 ms +- 1.2 ms) mixed in lightly: a faint digital sheen."""
        mix = 0.20 * self.s
        if mix <= 0:
            return x
        n = x.size
        buf = np.concatenate([self._hist, x])
        offset = self._hist.size
        t = (np.arange(n) + self._phase) / self._rate
        delay = self._rate * (0.012 + 0.0012 * np.sin(2 * math.pi * 0.35 * t))
        pos = offset + np.arange(n) - delay
        i0 = np.floor(pos).astype(int)
        frac = (pos - i0).astype(np.float32)
        delayed = buf[i0] * (1 - frac) + buf[np.minimum(i0 + 1, buf.size - 1)] * frac
        self._phase += n
        self._hist = buf[-self._hist.size:]
        return x * (1 - 0.5 * mix) + delayed * mix

    def _space(self, x: np.ndarray) -> np.ndarray:
        """Three short reflections and one soft echo (feedback 0.30 * strength) with state across chunks."""
        s = self.s
        n = x.size
        buf = np.concatenate([self._tap_hist, x])
        out = x.copy()
        base = self._tap_hist.size
        for delay, gain in self._taps:
            out += gain * buf[base - delay: base - delay + n]
        self._tap_hist = buf[-self._tap_hist.size:]
        # feedback comb for the tail
        fb = 0.30 * s
        echo = self._echo
        d = self._echo_len
        res = out.copy()
        if fb > 0 and d > 0:
            work = np.concatenate([echo, res])
            for start in range(0, n, d):  # block-wise: each block depends on the previous d samples only
                stop = min(start + d, n)
                work[d + start: d + stop] += fb * work[start: start + (stop - start)]
            res = work[d:]
            self._echo = work[-d:].copy()
        return res

    def _run(self, x: np.ndarray) -> np.ndarray:
        assert self._sos is not None and self._zi is not None
        y, self._zi = sosfilt(self._sos, x, zi=self._zi)
        y = self._shimmer(y.astype(np.float32))
        y = self._space(y)
        drive = 1.0 + 0.6 * self.s  # gentle soft limiter: no clipping, a little density
        makeup = 10 ** (-(1.2 + 3.8 * self.s) / 20)  # the shaping adds level: keep the loudness of the dry voice
        return (np.tanh(y * drive) / math.tanh(drive) * makeup).astype(np.float32)

    # -- public API
    def process(self, pcm: bytes, rate: int) -> bytes:
        if not pcm or rate <= 0:
            return pcm
        if self._rate != rate:
            self._setup(rate)
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        y = self._run(x)
        return (np.clip(y, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()

    def flush(self) -> bytes:
        """The decaying tail of the last words (reflections and echo), so the end does not stop dead."""
        if not self._rate:
            return b""
        silence = np.zeros(int(self._rate * _TAIL_S), dtype=np.float32)
        y = self._run(silence)
        fade = np.linspace(1.0, 0.0, y.size, dtype=np.float32)
        return (np.clip(y * fade, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


__all__: list[Any] = ["FxSettings", "VoiceFx", "current_settings", "for_turn"]
