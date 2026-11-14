"""On-device Russian STT: Sber GigaAM v3 (RNN-T) through sherpa-onnx.

Fork addition (2026-11-14). A Russian-only recogniser that is far more accurate on
Russian than the multilingual streaming Nemotron model and ~30x faster than
Whisper on a CPU: ~0.03-0.15 s per voice command on a desktop CPU, int8, no GPU,
no torch. The model (MIT licence, from github.com/salute-developers/GigaAM,
converted by sherpa-onnx) is NOT bundled: download
``sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16`` from the sherpa-onnx
``asr-models`` release and unpack it under ``<data>/models/``.

Offline (utterance-level) decoding: the pipeline already hands this provider
complete VAD-segmented utterances, so no streaming is needed. Output is lower
case without punctuation, which is what command matching wants.

Structural provider (duck-typed against ``STTProvider``). Every heavy import is
lazy and a missing engine or model produces one honest error.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np

from jarvis.audio.capture import pcm_bytes_to_np
from jarvis.core.protocols import AudioChunk, Transcript

log = logging.getLogger(__name__)

#: Directory name the unpacked archive has; the default lookup path under ``<data>/models``.
MODEL_DIRNAME = "sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16"
MODEL_FILES = ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")

#: Silence around the utterance. An offline transducer can clip the last syllable
#: when the audio ends abruptly right after speech; a short pad on both sides
#: costs a few milliseconds and keeps the first and last word intact.
_PAD_S = 0.3
_INT16_FULL_SCALE = 32768.0
#: GigaAM's log-mel front end uses 64 bins (the sherpa-onnx default of 80 would not load).
_FEATURE_DIM = 64


def default_model_dir() -> Path:
    from jarvis.core.config import DATA_DIR  # noqa: PLC0415 — lazy, keeps import cheap

    return Path(DATA_DIR) / "models" / MODEL_DIRNAME


class GigaAMLocalSTT:
    """Local Russian speech-to-text, keyless and on-device."""

    name = "gigaam-local"
    # Nothing this recogniser hears leaves the machine (the dictation polish
    # privacy floor keys on this declaration).
    runs_on_device = True
    supports_streaming = False

    def __init__(
        self,
        *,
        language: str | None = None,
        model: str | None = None,  # accepted for the factory's model pin; there is one model
        model_dir: str | Path | None = None,
        num_threads: int = 4,
        provider: str = "cpu",
        pad_s: float = _PAD_S,
    ) -> None:
        self._language = (language or "").strip() or None
        self._model_dir = Path(model_dir) if model_dir else None
        self._num_threads = max(1, int(num_threads))
        self._provider = provider or "cpu"
        self._pad_s = max(0.0, float(pad_s))
        self._recognizer: Any | None = None
        # One decode at a time per instance (AP-24): a Jarvis turn decodes one
        # utterance at a time, and serialising removes a native-engine race class.
        self._decode_lock = asyncio.Lock()

    # -- construction -------------------------------------------------------
    def _files(self) -> dict[str, Path]:
        base = self._model_dir or default_model_dir()
        return {name: base / name for name in MODEL_FILES}

    def _ensure_model(self) -> None:
        if self._recognizer is not None:
            return
        try:
            import sherpa_onnx  # noqa: PLC0415 — optional engine, lazy on purpose
        except ImportError as exc:
            raise RuntimeError(
                "The local speech runtime (sherpa-onnx) is not installed, so "
                "GigaAM cannot transcribe."
            ) from exc
        files = self._files()
        missing = [name for name, path in files.items() if not path.is_file()]
        if missing:
            raise RuntimeError(
                "The GigaAM model files are not on this machine "
                f"(missing: {', '.join(sorted(missing))} in {next(iter(files.values())).parent}). "
                f"Unpack {MODEL_DIRNAME}.tar.bz2 from the sherpa-onnx asr-models release there."
            )
        log.info("Loading local GigaAM v3 model (%s, %d threads)", self._provider, self._num_threads)
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(files["encoder.int8.onnx"]),
            decoder=str(files["decoder.onnx"]),
            joiner=str(files["joiner.onnx"]),
            tokens=str(files["tokens.txt"]),
            num_threads=self._num_threads,
            sample_rate=16_000,
            feature_dim=_FEATURE_DIM,
            decoding_method="greedy_search",
            provider=self._provider,
            model_type="nemo_transducer",
        )

    # -- transcription ------------------------------------------------------
    async def transcribe(self, audio: AsyncIterator[AudioChunk]) -> Transcript:
        """Collect the whole utterance, then decode it in one pass."""
        pieces: list[np.ndarray] = []
        sample_rate = 16_000
        async for chunk in audio:
            pieces.append(pcm_bytes_to_np(chunk.pcm))
            sample_rate = chunk.sample_rate
        if not pieces:
            return Transcript(text="", language="unknown", confidence=0.0)
        return await self._decode(np.concatenate(pieces), sample_rate)

    async def transcribe_pcm(
        self, pcm_bytes: bytes, sample_rate: int = 16_000, language: str | None = None
    ) -> Transcript:
        """Direct path for VAD output: int16 PCM bytes -> transcript."""
        return await self._decode(pcm_bytes_to_np(pcm_bytes), sample_rate)

    async def stream_transcribe(self, audio: AsyncIterator[AudioChunk]) -> AsyncIterator[Transcript]:
        """One final transcript, matching ``supports_streaming = False``."""
        yield await self.transcribe(audio)

    async def _decode(self, samples: np.ndarray, sample_rate: int) -> Transcript:
        if samples.size == 0:
            return Transcript(text="", language="unknown", confidence=0.0)
        async with self._decode_lock:
            return await asyncio.to_thread(self._decode_blocking, samples, sample_rate)

    def _decode_blocking(self, samples: np.ndarray, sample_rate: int) -> Transcript:
        self._ensure_model()
        recognizer = self._recognizer
        assert recognizer is not None  # _ensure_model raises otherwise
        audio = np.asarray(samples, dtype=np.float32)
        if np.max(np.abs(audio)) > 1.0:  # int16-scaled input -> [-1, 1]
            audio = audio / _INT16_FULL_SCALE
        if self._pad_s > 0:
            pad = np.zeros(int(sample_rate * self._pad_s), dtype=np.float32)
            audio = np.concatenate([pad, audio, pad])
        stream = recognizer.create_stream()
        stream.accept_waveform(sample_rate, audio)
        recognizer.decode_stream(stream)
        raw = (stream.result.text or "").strip()
        from jarvis.plugins.stt.transcript_filter import clean_stt_text  # noqa: PLC0415

        text = clean_stt_text(raw, language="ru")
        return Transcript(
            text=text,
            raw_text=raw,
            language="ru",
            # The transducer exposes no calibrated utterance probability; inventing
            # one would put a fabricated number in front of every confidence check.
            confidence=1.0 if text else 0.0,
        )

    async def aclose(self) -> None:
        """Release the recognizer so a switched-away provider frees its memory."""
        self._recognizer = None
