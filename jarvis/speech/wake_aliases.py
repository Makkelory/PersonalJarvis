"""Extra grammar words for a one-word wake phrase (fork addition 2026-11-14).

A Vosk grammar can only recognise words that exist in the model's lexicon, so a
mispronounced wake word ("Джервис" for "Джарвис") is invisible to the stage-one
spotter unless that sound-alike spelling is offered to it too. This module keeps a
small table of such spellings per wake word:

* built-in defaults (spellings verified to exist in the small Russian model);
* the optional user file ``data/wake_aliases.json``, e.g.
  ``{"джарвис": ["харви", "джаред"]}`` — add a word there when the log shows the
  detector heard it ("free ear heard 'X'").

Pure stdlib. Only unprefixed single-word phrases get variants; anything else
returns an empty tuple so prefixed phrases behave exactly as before.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from jarvis.speech.wake_constants import normalize_phrase_for_match

#: Phonetic-Latin core of the wake word -> sound-alike spellings to add to the grammar.
_DEFAULTS: dict[str, tuple[str, ...]] = {
    # "jarvis": ("джервис",),  # disabled: lowered measured recall (see tests)
}


def _user_file() -> Path:
    base = os.environ.get("JARVIS__MEMORY__DATA_DIR") or "data"
    return Path(base) / "wake_aliases.json"


def _user_aliases(phrase: str) -> tuple[str, ...]:
    try:
        data = json.loads(_user_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not isinstance(data, dict):
        return ()
    wanted = phrase.strip().lower()
    for key, values in data.items():
        if str(key).strip().lower() == wanted and isinstance(values, list):
            return tuple(str(v).strip().lower() for v in values if str(v).strip())
    return ()


def grammar_variants(phrase: str) -> tuple[str, ...]:
    """Additional single-word spellings for ``phrase`` (never includes the phrase itself)."""
    tokens = normalize_phrase_for_match(phrase)
    raw = [t for t in phrase.strip().lower().split() if t]
    if len(tokens) != 1 or len(raw) != 1:  # prefixed / multi-word phrases: unchanged
        return ()
    found = list(_DEFAULTS.get(tokens[0], ())) + list(_user_aliases(phrase))
    out: list[str] = []
    for word in found:
        if " " in word or word == raw[0] or word in out:
            continue
        out.append(word)
    return tuple(out)
