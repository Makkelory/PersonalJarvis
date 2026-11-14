"""Pure local action gate for low-latency computer-use routing.

Visual-target and desktop-control utterances are routed to the
screenshot-based computer-use harness (:data:`HARNESS_NAME`). The harness
runs the in-process screenshot/click/keyboard loop
(``jarvis/harness/screenshot_only_loop.py``).
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import quote_plus

#: Entry-point name of the screenshot-based computer-use harness (see
#: ``pyproject.toml`` ``[project.entry-points."jarvis.harness"]`` +
#: ``jarvis/plugins/harness/computer_use.py``). Canonical home for the
#: literal so call sites import this constant instead of hardcoding it.
HARNESS_NAME = "screenshot"


class LocalActionMode(str, Enum):
    DIRECT = "DIRECT"
    COMPUTER_USE = "COMPUTER_USE"
    #: Returned when the utterance looks like an action request but no
    #: registered capability covers it.  ``manager.py`` must route this
    #: straight to TTS, skipping brain dispatch entirely.
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class LocalToolCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LocalActionPlan:
    mode: LocalActionMode
    tool_calls: tuple[LocalToolCall, ...] = field(default_factory=tuple)
    harness: str | None = None
    prompt: str | None = None
    #: Populated only when ``mode == UNSUPPORTED``.  A deterministic,
    #: human-readable rejection message in the user's language that
    #: ``manager.py`` forwards verbatim to TTS.
    response_text: str | None = None
    #: Fork addition: a queue of several commands spoken in one breath ("открой стим, включи музыку ...").
    #: The fast path runs every call in order and does not stop at the first failing one.
    compound: bool = False
    steps: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Capability registry protocol
#
# The *real* registry lives in ``jarvis.core.capabilities`` (Agent A).
# We define a structural Protocol here so that:
#   a) production code can do a late import of the real singleton, and
#   b) tests can inject a lightweight fake without importing the real module.
# ---------------------------------------------------------------------------


@runtime_checkable
class _CapabilityRegistryLike(Protocol):
    """Minimal surface of ``CapabilityRegistry`` needed by this module."""

    def has_action_intent(self, utterance: str) -> bool: ...
    def resolve_intent(self, utterance: str) -> object | None: ...


_DIRECT_PATTERNS = (
    # Russian application launch commands
    re.compile(
        r"^(?:эй\s+)?(?:джарвис[,\s]+)?"
        r"(?:открой|открывай|запусти|запускай|включи)\s+"
        r"(?P<app>.+?)\s*[?.!]*$",
        re.I,
    ),
    # ``(?:er)?oeffne(?:t|st|n)?`` covers öffne / öffnet / öffnest / öffnen and the
    # er-prefixed eröffne / eröffnet / … so "Eröffnet den Explorer" also takes the
    # clean DIRECT launch instead of the computer-use vision loop (live 2026-06-08).
    re.compile(
        r"^(?:hey\s+)?(?:jarvis[,\s]+)?(?:er)?oeffne(?:t|st|n)?\s+(?P<app>.+?)\s*[?.!]*$",
        re.I,
    ),
    re.compile(r"^(?:hey\s+)?(?:jarvis[,\s]+)?starte\s+(?P<app>.+?)\s*[?.!]*$", re.I),
    re.compile(
        r"^(?:hey\s+)?(?:jarvis[,\s]+)?mach\s+(?P<app>.+?)\s+(?:auf|auch)\s*[?.!]*$",
        re.I,
    ),
    re.compile(
        r"^(?:hey\s+)?(?:jarvis[,\s]+)?kannst\s+du\s+(?P<app>.+?)\s+aufmachen\s*[?.!]*$",
        re.I,
    ),
)

_VISUAL_TARGET_PATTERNS = (
    re.compile(r"^(?:hey\s+)?(?:jarvis[,\s]+)?(?:klick|click)\s+.+", re.I),
    re.compile(
        r"^(?:hey\s+)?(?:jarvis[,\s]+)?oeffne\s+.+\b"
        r"(?:links|rechts|oben|unten|rote|rotes|roten|gruen|gruene|gruenes|gruenen|"
        r"blaue|blaues|blauen)\b.+\b(?:fenster|button|feld|app)\b",
        re.I,
    ),
    re.compile(
        r"^(?:hey\s+)?(?:jarvis[,\s]+)?schreib\s+.+\s+in\s+.+"
        r"(?:button|feld|eingabefeld|chatgpt|fenster|app|seite)\b",
        re.I,
    ),
)

# Desktop-control commands that need the multi-step GUI loop (computer-use),
# NOT a single deterministic open and NOT a canned "I can't" refusal. Two
# unambiguous shapes:
#   1. Compound "oeffne/starte/mach <app> und <do something>" — the user opened
#      an app to operate it (e.g. "oeffne WhatsApp und schreib Mama hallo").
#   2. A standalone GUI manipulation verb (klick/scroll/tippe/doppelklick/...).
# Ambiguous verbs like "schreib"/"such" alone are deliberately excluded (they
# often mean "write me a poem" / "search the web", i.e. a plain brain answer).
_COMPOUND_OPEN_CONTROL_RE = re.compile(
    r"^(?:hey\s+)?(?:jarvis[,\s]+)?"
    r"(?:oeffne|starte|mach|geh\s+(?:auf|zu)|open|start|launch)\b"
    r".+\b(?:und|and)\b.+",
    re.I,
)
_GUI_VERB_RE = re.compile(
    r"\b(klick|click|doppelklick|rechtsklick|tippe|tipp\b|scroll|scrolle|"
    r"markier|kopier|einfueg|fueg\b.+\bein\b|waehle\s+aus|wechsel\s+(?:zu|auf)\s+fenster|"
    r"druecke?\s+(?:auf|den|die|das)\b)\w*",
    re.I,
)

# Broader Computer-Use commands the narrow GUI-verb + compound-open patterns
# miss but that are unambiguously "operate the desktop" — they MUST reach the
# computer-use loop deterministically, never depend on the LLM talker calling
# computer_use. Live regression 2026-06-09: the talker (brain.primary=codex, CLI
# OAuth path) DROPS ALL TOOLS and can never emit a tool_call, so every CU task
# that fell to it went silent — ~30% of common CU commands (navigate /
# screenshot / window-ops / drag). Each pattern is high-precision; how-to
# questions ("wie navigiere ich…") are excluded in _looks_like_desktop_control
# so they stay normal brain answers. Inputs are pre-_normalize'd (umlauts → ascii:
# "schließ" → "schliess", "nächste" → "naechste", "vergrößer" → "vergroesser").
# Only directional prepositions (zu/auf/nach/in) — "navigiere DURCH das Menü" is
# deliberately excluded (too close to an explain/walk-me-through request).
_NAVIGATE_RE = re.compile(r"\bnavigier\w*\s+(?:zu|auf|nach|in)\b", re.IGNORECASE)
# Screenshot — verb-anchored on BOTH sides so a TAKE imperative matches
# ("mach/nimm/erstell/knips einen Screenshot", "Screenshot machen") but an
# informational or send/show mention does NOT ("ich habe einen Screenshot
# gemacht", "schick mir einen Screenshot", "zeig den letzten Screenshot") —
# review finding 2026-06-09. "gemacht" never matches \bmach\w* (no word boundary
# before "mach" inside "gemacht").
_SCREENSHOT_RE = re.compile(
    r"\b(?:mach|nimm|erstell|knips|capture|take|grab)\w*\b[\w\s]{0,14}?"
    r"\b(?:screenshot|bildschirmfoto|bildschirmaufnahme)\b"
    r"|\b(?:screenshot|bildschirmfoto|bildschirmaufnahme)\b[\w\s]{0,10}?"
    r"\b(?:mach|nimm|erstell|knips)\w*",
    re.IGNORECASE,
)
# "schließ/minimier/maximier … (das) Fenster/Tab/App" — the verb ALONE is too
# ambiguous ("schließ die Tür"), so a desktop-context noun within ~18 chars is
# required. Plus tab-switching ("wechsel zum nächsten Tab", "nächster Tab").
_WINDOW_OP_RE = re.compile(
    r"\b(?:schliess|minimier|maximier|verklein|vergroesser)\w*\b[\w\s]{0,18}?"
    r"\b(?:fenster|tab|app|programm|browser|seite|dialog)\b"
    r"|\bwechsel\w*\s+(?:zu|auf|zum|zur|in|den|das|die|naechste[nr]?)\b[\w\s]{0,12}?"
    r"\b(?:tab|fenster|app|programm)\b"
    r"|\b(?:naechst|vorherig|letzt|erst)\w*\s+tab\b",
    re.IGNORECASE,
)
# Drag / move — "zieh"/"verschieb"/"drag" are common words, so a desktop object
# or a direction is required to avoid "zieh dich an".
_DRAG_RE = re.compile(
    r"\b(?:zieh|drag|verschieb)\w*\b[\w\s]{0,20}?"
    r"\b(?:fenster|datei|icon|maus|cursor|element|nach\s+(?:links|rechts|oben|unten))\b",
    re.IGNORECASE,
)
# Explicit "use Computer-Use to …" — the user NAMES the harness as the instrument
# ("mit Computer-Use … abspielen", "per/über Computer-Use", "nutze/benutze/
# verwende Computer-Use …", "use/with/via/using computer use …"). This is the
# strongest possible deterministic CU signal: the user said WHICH harness to use,
# so the command MUST reach the screenshot loop without depending on the LLM
# talker emitting a computer_use tool_call (the codex CLI talker drops ALL tools
# and never can — see the broader-CU comment above; live repro 2026-06-21: "mit
# Computer-Use in Spotify … abspielen" only ACK'd, never acted, because the
# phrase has no open verb and fell through to the talker). An instrument marker
# must IMMEDIATELY precede the term (optionally an article), so a how-to/explain
# mention without a marker ("erklär mir Computer-Use", "was ist Computer-Use")
# never matches; question-word how-tos are also excluded in
# _looks_like_desktop_control via _OPEN_INSTRUCTIONAL_RE. Input is pre-_normalize'd
# (umlauts → ascii: "über" → "ueber"); the literal hyphen in "computer-use"
# survives normalize, so [-\s]? covers both "computer-use" and "computer use".
_EXPLICIT_COMPUTER_USE_RE = re.compile(
    r"\b(?:mit|per|ueber|via|with|using|use|nutz\w*|benutz\w*|verwend\w*)\s+"
    r"(?:der\s+|die\s+|das\s+|den\s+|the\s+)?"
    r"computer[-\s]?use\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Question / narration guard (maintainer mandate 2026-08-25).
#
# Live bug, flight recorder 2026-08-25 16:50:08: "Wie nennt man das, wenn ...
# dann KLICKE ICH da drauf ... wie nennt sich das in der Fachsprache?" -- a pure
# vocabulary question -- was shipped verbatim to the computer-use harness. Two
# independent defects produced it:
#
#   1. ``_GUI_VERB_RE`` matched the narrated "klicke" and returned True BEFORE
#      the instructional guard below could veto it, so a question that opens
#      with "wie" could never be recognised as one.
#   2. The GUI-verb match ignored WHO acts. "klicke ICH" is the user describing
#      their own habit; a command is addressed to Jarvis and carries no subject.
#
# The rule the maintainer stated: decide whether the turn is a TASK or a
# QUESTION. A polite request in question form stays a task ("Kannst du bitte
# einen Screenshot machen?") -- it carries no interrogative opener. An
# information question carries one and gives no order.
# ---------------------------------------------------------------------------

#: An interrogative that OPENS a clause -- at the start of the utterance or
#: right after a clause break. Trailing question clauses matter as much as
#: leading ones: the live utterance asked its real question in the LAST clause
#: ("..., wie nennt sich das in der Fachsprache?"), long after the narrated GUI
#: verb. Word-boundaried so "wo" never fires inside "worker" or "wonach".
_QUESTION_OPENER_RE = re.compile(
    r"(?:^|[,;:.!?]\s*|\b(?:und|oder|aber|and|or|but)\s+)"
    r"(?:"
    r"wie|was|warum|wieso|weshalb|wozu|wann|wo|wofuer|woran|worin|worum|"
    r"woher|wohin|wonach|welche[rsnm]?|wer|wen|wem|wessen"
    r"|how|what|why|when|where|which|who|whom|whose"
    r")\b",
    re.IGNORECASE,
)

#: 2nd-person imperative stems that address Jarvis. Mirrors the LLM-path guard
#: in ``jarvis.brain.tool_use_loop`` (``_IMPERATIVE_VERB_RE``) -- deliberately
#: kept to the desktop-control verbs this gate can actually route. German
#: infinitives cannot match: "oeffnen"/"klicken" carry no word boundary after
#: the imperative stem.
_ASSISTANT_IMPERATIVE_RE = re.compile(
    r"\b(?:"
    r"mach|mache|oeffne|starte|beende|schliess|schliesse|"
    r"klick|klicke|tipp|tippe|drueck|druecke|scroll|scrolle|"
    r"zeig|zeige|spiel|spiele|schreib|schreibe|such|suche|"
    r"wechsle|wechsel|geh|gehe|nimm|hol|hole|navigier|navigiere|"
    r"minimier|minimiere|maximier|maximiere|zieh|ziehe|verschieb|verschiebe"
    r"|open|close|click|type|press|scroll|show|play|write|search|switch|"
    r"start|launch|take|grab|drag|minimize|maximize|navigate"
    r")\b",
    re.IGNORECASE,
)

#: An explicit subject turns the identical German verb form into a 1st-person
#: indicative: "klicke ICH", "macht MAN", "gehe ICH". A real imperative never
#: carries one. Same token set as ``tool_use_loop._INDICATIVE_SUBJECT_RE`` so
#: the two guards classify the same sentence the same way.
_INDICATIVE_SUBJECT_AFTER_RE = re.compile(
    r"\s*(?:ich|man|wir|i|we|you|yo|uno)\b",
    re.IGNORECASE,
)
#: English (and the German infinitive marker) put the subject in FRONT of the
#: verb: "...how do I click", "...um zu klicken".
_INDICATIVE_SUBJECT_BEFORE_RE = re.compile(
    r"\b(?:i|you|we|they|to|ich|man|wir|zu)\s+$",
    re.IGNORECASE,
)
#: German subordinate clauses push the verb to the END, far from its subject:
#: "…, wenn ICH auf den roten Button KLICKE?". Adjacency alone therefore misses
#: the subject, so the clause in front of the verb is scanned as a whole. Only
#: 1st-person and impersonal subjects count here — never "du"/"you", which can
#: perfectly well address Jarvis ("du sollst mal runterscrollen" is an order,
#: not narration).
_CLAUSE_START_RE = re.compile(r"[,;:.!?]")
_NARRATING_SUBJECT_RE = re.compile(r"\b(?:ich|man|wir|i|we)\b", re.IGNORECASE)


def _is_narrated(text: str, start: int, end: int) -> bool:
    """True when the verb at ``[start:end)`` carries an explicit subject.

    A described action ("dann klicke ich da drauf") is not an order. Same shape
    as ``spawn_gate._REPORTED_VEHICLE_RE``: the vocabulary matches, but the
    grammar says the user is reporting, not requesting.
    """
    before = text[:start]
    clause_breaks = list(_CLAUSE_START_RE.finditer(before))
    clause = before[clause_breaks[-1].end():] if clause_breaks else before
    return bool(
        _INDICATIVE_SUBJECT_AFTER_RE.match(text, end)
        or _INDICATIVE_SUBJECT_BEFORE_RE.search(before)
        or _NARRATING_SUBJECT_RE.search(clause)
    )


def _has_assistant_imperative(text: str) -> bool:
    """True when the turn gives Jarvis an order somewhere in it."""
    return any(
        not _is_narrated(text, match.start(), match.end())
        for match in _ASSISTANT_IMPERATIVE_RE.finditer(text)
    )


def _is_information_question(text: str) -> bool:
    """True when the turn ASKS something instead of ordering desktop work.

    Both halves are required. The interrogative opener alone would veto
    "Warum ist Spotify zu, mach es auf" -- which opens as a question and still
    ends in an order the user wants executed (the same stand-down as
    ``tool_use_loop._is_instructional_question``). An order alone is not enough
    either: "Kannst du bitte einen Screenshot machen?" carries no interrogative
    opener at all and stays a task.
    """
    if not _QUESTION_OPENER_RE.search(text):
        return False
    return not _has_assistant_imperative(text)


def _has_commanding_gui_verb(text: str) -> bool:
    """True when a GUI verb is an ORDER, not the user narrating their own use.

    ``_GUI_VERB_RE`` alone matched "dann klicke ich da drauf" and handed a
    vocabulary question to the vision loop (live 2026-08-25).
    """
    return any(
        not _is_narrated(text, match.start(), match.end())
        for match in _GUI_VERB_RE.finditer(text)
    )


def _looks_like_desktop_control(text: str) -> bool:
    """True for GUI-manipulation commands that should drive the computer-use loop.

    Conservative on purpose: a compound open-and-operate command, an unambiguous
    GUI verb (klick/scroll/tippe/…), or a broader desktop-control verb
    (navigate / screenshot / window-op / drag) — so plain "oeffne chrome" stays
    the fast DIRECT path and "schreib mir ein Gedicht" stays a normal brain
    answer. The broader verbs are guarded against how-to questions so
    "wie navigiere ich…" / "wie mache ich einen Screenshot" stay brain answers.
    """
    # TASK or QUESTION — decided FIRST, for every branch below. Until 2026-08-25
    # the instructional guard sat UNDER the GUI-verb branch, so a question could
    # only ever be recognised when it contained no GUI vocabulary at all; the
    # narrated "klicke" inside a question about download terminology was enough
    # to seize the desktop. Nothing below can be desktop-control when the turn
    # asks a question and gives no order.
    if _is_information_question(text):
        return False
    # A "starte/oeffne X und Y" compound is desktop-control ONLY when X/Y is real
    # GUI work — never when it is heavy worker / research / sub-agent work. Live
    # bug 2026-06-15: "Starte eine Sub-Agent-Mission und recherchiere …" matched
    # _COMPOUND_OPEN_CONTROL_RE and ran on the screenshot harness. Unambiguous GUI
    # verbs (klick/scroll/tippe/…) stay desktop-control regardless of the nouns.
    if _COMPOUND_OPEN_CONTROL_RE.match(text) and not _NOT_OPEN_APP_RE.search(text):
        return True
    # ...but only when the user ORDERS the click; "dann klicke ich da drauf" is
    # the user describing their own workflow (live 2026-08-25).
    if _has_commanding_gui_verb(text):
        return True
    if _OPEN_INSTRUCTIONAL_RE.search(text):
        return False
    # An explicit "use Computer-Use to …" names the harness as the instrument —
    # the strongest deterministic CU signal. Checked AFTER the instructional
    # guard so a how-to "wie nutze ich Computer-Use" stays a brain answer.
    return bool(
        _EXPLICIT_COMPUTER_USE_RE.search(text)
        or _NAVIGATE_RE.search(text)
        or _SCREENSHOT_RE.search(text)
        or _WINDOW_OP_RE.search(text)
        or _DRAG_RE.search(text)
    )


# Open / launch verbs in ANY conjugation. Inputs are pre-normalised
# (``_normalize``: lowercased + umlauts transliterated, so "öffnest" -> "oeffnest"),
# so ``oeffn\w*`` catches öffne/öffnest/öffnet/öffnen alike. ``start\w*`` catches
# starte/startest/start; "mach … auf" is the separable verb handled separately.
# NB: the English open verb is pinned to its real conjugations — a bare
# ``open\w*`` also swallowed PRODUCT NAMES starting with "open" (forensic
# 2026-07-10: "…meinen OpenRouter-Zugang checkst und…" counted as an
# open-verb, the "und" made it a compound open-and-operate command, and a
# plain provider-check request was deterministically shipped to the
# computer-use loop — clicking through the browser instead of calling the
# provider-test endpoint). openrouter/openai/openwakeword must never count
# as "open".
_OPEN_VERB_RE = re.compile(
    r"\b(?:открой|открывай|запусти|запускай|включи|oeffn\w*|aufmach\w*|aufzumach\w*|start\w*|open(?:s|ed|ing)?|launch\w*)\b",
    re.IGNORECASE,
)
# Separable verb "mach … auf" (particle trails the object): "mach mir Spotify auf".
_MACH_AUF_RE = re.compile(r"\bmach(?:e|st|t)?\b[\w\s]*\bauf\b", re.IGNORECASE)
# A coordinating "und"/"and" splits an open command from a follow-up action
# ("…Spotify öffnen UND Shape of You spielen") → the request is multi-step and
# belongs on the computer-use loop, not a single DIRECT open. Word-boundaried so
# it never fires inside a token. Operates on the normalised (transliterated)
# utterance, where "und" and "and" are stable.
_AND_RE = re.compile(r"\b(?:und|and)\b", re.IGNORECASE)
# Negated open ("ich will Spotify NICHT öffnen", "bitte KEIN Chrome starten",
# "öffne Discord lieber nicht") must NEVER launch. The verb-at-end fallback below
# scans the WHOLE utterance for a known app name, so without this guard it would
# happily launch the very app the user said NOT to open (review finding
# 2026-06-09). High-precision, low-cost: a negation token anywhere in the
# utterance suppresses the deterministic launch; the turn then falls to the brain
# (which, with clarify off by default, does not nag the user). Deliberately NOT
# folded into ``is_open_app_intent`` — that predicate is also the force-spawn
# guard, and a negated open must still count as an open there (to stay OFF the
# sub-agent worker path), just not trigger an actual launch here.
_OPEN_NEGATION_RE = re.compile(r"\bnicht\b|\bkein\w*\b|\bniemals\b", re.IGNORECASE)
# Signals that the request is NOT a plain desktop app-open but heavy worker /
# external-system work, which a sandboxed worker (not computer-use) owns. This
# is the single veto consulted by is_open_app_intent (and therefore by both the
# COMPUTER_USE gate at match_local_action AND the force-spawn guard in
# manager.py), so research / sub-agent / mission vocabulary MUST live here:
# "starte eine Sub-Agent-Mission und recherchiere …" overcaptures "startest" as
# an open-verb, and without this veto it was misrouted to the screenshot harness
# ("ich erledige das am Bildschirm") instead of a research worker (live bug
# 2026-06-15, sessions.db session 236877e6).
_NOT_OPEN_APP_RE = re.compile(
    r"\b(?:"
    r"pr|prs|pull\s*request|repo|repository|github|gitlab|issue|issues|branch|"
    r"baue?|baust|baut|implementier\w*|entwickel\w*|refactor\w*|debugg?\w*|"
    r"analysier\w*|analyz\w*|untersuch\w*|programmier\w*|deploy\w*|"
    r"datei|dateien|file|files|funktion\w*|skript|script|landingpage|"
    r"recherch\w*|research\w*|tiefenrecherche\w*|deep[\s-]?dive|"
    r"sub-?agent\w*|subagent\w*|mission\w*"
    r")\b",
    re.IGNORECASE,
)
# Instructional questions ("wie oeffne ich X?") must never launch anything.
_OPEN_INSTRUCTIONAL_RE = re.compile(
    r"^\s*(?:wie|how|was|what|warum|why|wieso|weshalb)\b", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Browser+URL fast-path (2026-06-10 latency collapse, Task 2).
#
# "oeffne chrome und gehe auf x.com" is ONE deterministic argv launch —
# browsers accept a URL argument on win/mac/linux, and open_app already
# whitelists http(s):// targets and forwards ``arguments``. Without this
# branch the compound regex routed the goal into the vision-LLM loop, which
# took ~3 minutes live (log 2026-06-10 20:46) for what is a 1-second launch.
# Both regexes are end-anchored: a site followed by MORE work ("… und poste
# einen tweet") must keep the computer-use loop, which has to act on the page.
# ---------------------------------------------------------------------------
_BROWSER_TOKENS = (
    "chrome", "firefox", "edge", "brave", "opera", "safari", "chromium",
    "vivaldi",
)
#: A bare domain or URL ("x.com", "https://github.com/foo"). Requires a dot +
#: TLD-like tail so "geh auf nummer sicher" never matches.
_SITE_PATTERN = r"(?P<site>(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?)"
_GOTO_VERBS = (
    r"(?:geh(?:e|st)?\s+(?:auf|zu|nach)|navigiere?\s+(?:zu|auf|nach)"
    r"|go\s+to|navigate\s+to|oeffne|open)"
)
_OPEN_BROWSER_GOTO_RE = re.compile(
    r"^(?:hey\s+)?(?:jarvis[,\s]+)?(?:oeffne|starte|open|start|launch)\b[^.!?]*?"
    r"\b(?P<app>" + "|".join(_BROWSER_TOKENS) + r")\b.*?\b"
    + _GOTO_VERBS + r"\s+" + _SITE_PATTERN + r"\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_BARE_GOTO_RE = re.compile(
    r"^(?:hey\s+)?(?:jarvis[,\s]+)?" + _GOTO_VERBS + r"\s+" + _SITE_PATTERN
    + r"\s*[.!?]?\s*$",
    re.IGNORECASE,
)

# Search-query browser launch (live bug 2026-06-15, sessions.db session
# c995c9bc): "Open the Chrome browser for me and Google <query>" is ONE
# deterministic argv launch — open the browser straight to a Google results
# page. It must NOT take the credit-gated Computer-Use vision loop (which
# silently no-ops when the desktop extra is unavailable) and must NOT drop the
# search clause (the old verb-at-end fallback launched only Chrome). Bilingual:
# both the conjunction (and/und) and the search verb (google / search for /
# suche nach) are DE+EN — the German-only _AND_RE never split the English "and",
# so the whole search half was discarded. The search verb is anchored directly
# after the conjunction so a non-search follow-up ("… und schreib Mama hallo")
# stays on the Computer-Use loop. End-anchored on the query.
_BROWSER_SEARCH_VERB_RE = (
    r"(?:google(?:\s+nach)?|search\s+for|search|look\s+up|"
    r"such(?:e|st)?(?:\s+nach)?)"
)
_OPEN_BROWSER_SEARCH_RE = re.compile(
    r"^(?:hey\s+)?(?:jarvis[,\s]+)?(?:oeffne|starte|open|start|launch)\b[^.!?]*?"
    r"\b(?P<app>" + "|".join(_BROWSER_TOKENS) + r")\b[^.!?]*?"
    r"\b(?:und|and)\b\s*" + _BROWSER_SEARCH_VERB_RE + r"\s+"
    r"(?P<query>\S.*?)\s*[.!?]*$",
    re.IGNORECASE,
)

#: Google results endpoint for the search fast-path. open_app forwards the URL
#: verbatim to the browser argv (only ``app_name`` is plausibility-gated), so a
#: ``?q=…`` query string is passed through unchanged — the same contract the
#: browser+URL fast-path already relies on.
_SEARCH_URL_PREFIX = "https://www.google.com/search?q="


def _site_to_url(site: str) -> str:
    return site if site.startswith(("http://", "https://")) else f"https://{site}"


def _search_to_url(query: str) -> str:
    return _SEARCH_URL_PREFIX + quote_plus(query.strip())


# A trailing desktop ACTION smuggled into a browser-search query. The search
# fast-path captures the query non-greedily to the end of the utterance, so
# "...google das wetter UND SCROLLE runter" / "...google cats THEN CLICK the
# first result" folds the follow-up action into the Google query string and runs
# a wrong search (finding 2026-06-26). When the query carries such a follow-up,
# the search fast-path DECLINES (returns None) so the utterance falls to the
# compound-open desktop-control routing → the multi-step computer-use loop, which
# runs BOTH the search and the follow-up action.
#
# A connector (und/and/dann/then/danach/…) is REQUIRED immediately before the
# GUI verb, so a legitimate multi-term search is never mistaken for an action:
# "katzen UND hunde" (no verb after the connector) and "how TO CLICK a button"
# (no connector before the verb) both stay clean DIRECT searches. Operates on
# the pre-``_normalize``'d query (umlauts → ascii), so ascii verb stems match.
_QUERY_FOLLOWUP_ACTION_RE = re.compile(
    r"\b(?:und|and|dann|then|danach|anschliessend|afterwards)\b"
    r"(?:\s+(?:dann|then))?\s+"
    r"(?:"
    r"klick|click|doppelklick|rechtsklick|tipp|scroll|"
    r"markier|kopier|einfueg|waehl|wechsel|drueck|navigier|"
    r"schliess|minimier|maximier|zieh|drag|verschieb|spiel|play"
    r")\w*",
    re.IGNORECASE,
)


def _query_has_followup_action(query: str) -> bool:
    """True when a browser-search query smuggles a trailing desktop action
    (a connector + a GUI verb, e.g. "... und scrolle runter", "... then click").

    A connector is REQUIRED before the verb so a legitimate multi-term search
    ("katzen und hunde", "how to click a button") is never caught. Pure regex,
    no IO (AP-9 / AP-11)."""
    return bool(_QUERY_FOLLOWUP_ACTION_RE.search(query))


def _match_browser_url_fast_path(normalized: str) -> LocalActionPlan | None:
    """Deterministic browser+URL launch, or None to fall through."""
    if _OPEN_NEGATION_RE.search(normalized):
        return None
    if _OPEN_INSTRUCTIONAL_RE.search(normalized):
        return None
    m = _OPEN_BROWSER_GOTO_RE.match(normalized)
    if m:
        return LocalActionPlan(
            mode=LocalActionMode.DIRECT,
            tool_calls=(LocalToolCall(
                name="open_app",
                args={
                    "app_name": m.group("app").lower(),
                    "arguments": _site_to_url(m.group("site")),
                },
            ),),
        )
    m = _BARE_GOTO_RE.match(normalized)
    if m:
        return LocalActionPlan(
            mode=LocalActionMode.DIRECT,
            tool_calls=(LocalToolCall(
                name="open_app",
                args={"app_name": _site_to_url(m.group("site"))},
            ),),
        )
    m = _OPEN_BROWSER_SEARCH_RE.match(normalized)
    # Decline when the query smuggled a trailing desktop action ("...google das
    # wetter UND SCROLLE runter") — folding it into the Google query would run a
    # wrong search and drop the follow-up. Returning None lets the compound-open
    # desktop-control routing send the whole turn to the computer-use loop, which
    # performs the search AND the action (finding 2026-06-26).
    if m and not _query_has_followup_action(m.group("query")):
        return LocalActionPlan(
            mode=LocalActionMode.DIRECT,
            tool_calls=(LocalToolCall(
                name="open_app",
                args={
                    "app_name": m.group("app").lower(),
                    "arguments": _search_to_url(m.group("query")),
                },
            ),),
        )
    return None


def is_open_app_intent(text: str) -> bool:
    """True for a request to OPEN / LAUNCH an application or window on the
    desktop — in any conjugation or phrasing — that a sandboxed sub-agent worker
    could never fulfil (a worker runs in an isolated git worktree and has no
    desktop). Such requests belong to the computer-use harness, NEVER to a
    force-spawned worker.

    Live bug 2026-06-08 (data/jarvis_desktop.log 17:37): "Ich möchte, dass du mir
    Hermes Agent öffnest, also …" force-spawned a worker because the capability
    registry resolves verbs strictly (``\\boeffne\\b``, base form only) while the
    action detector matches conjugations (``\\boeffne\\w*\\b``) — so "öffnest"
    counted as an action no capability resolves, i.e. generic sub-agent work.

    Excludes instructional questions ("wie oeffne ich X"), external-system work
    (PR / repo / GitHub), and heavy build / code / file work that genuinely needs
    a worker. Pure regex, no LLM / no IO (AP-9 / AP-11)."""
    t = _normalize(text)
    if not t:
        return False
    if _OPEN_INSTRUCTIONAL_RE.search(t):
        return False
    if _NOT_OPEN_APP_RE.search(t):
        return False
    return bool(_OPEN_VERB_RE.search(t) or _MACH_AUF_RE.search(t))

_APP_ALIASES = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "firefox": "firefox",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "msedge": "msedge",
    "notepad": "notepad",
    "блокнот": "notepad",
    "editor": "notepad",
    "texteditor": "notepad",
    "explorer": "explorer",
    "dateiexplorer": "explorer",
    "windows explorer": "explorer",
    "rechner": "calc",
    "taschenrechner": "calc",
    "calc": "calc",
    "calculator": "calc",
    "windows terminal": "wt",
    "terminal": "wt",
    "wt": "wt",
    "spotify": "spotify",
    # Chat / communication + media apps a voice user opens by name. Every value
    # is already in open_app's KNOWN_APPS and resolvable by app_resolver
    # (App Paths / PATH / the Start Menu .lnk fallback for Squirrel installs
    # like Discord/Slack, added 2026-06-09). Before this, "öffne Discord" was
    # absent here, fell through to the brain, and the router opened it via
    # computer_use but — producing no narration — was answered with the
    # clarifying question "Wie meinst du das genau?" instead of just launching
    # it on the fast deterministic path (live bug 2026-06-09).
    "discord": "discord",
    "slack": "slack",
    "telegram": "telegram",
    "whatsapp": "whatsapp",
    "whats app": "whatsapp",
    "signal": "signal",
    "teams": "teams",
    "microsoft teams": "teams",
    "zoom": "zoom",
    "skype": "skype",
    "vlc": "vlc",
    "steam": "steam",
    "outlook": "outlook",
}

# An app name that is the OBJECT of a container preposition ("oeffne meine
# Bilder IM Explorer", "IN meinen Explorer meine Bilder oeffnen") is NOT the
# whole goal — the app is a folder/window the user navigates INTO, and the real
# target (the Bilder/Downloads/Dokumente) lives elsewhere in the utterance. A
# bare ``open_app`` launch would drop that target and open a blank window (live
# bug 2026-06-26 voice session: "in meinen Explorer meine Bilder oeffnen"
# launched explorer.exe empty, the pictures never opened, spoken back as the
# fast "Gestartet: explorer"). Such navigate-into-app goals belong on the
# multi-step computer-use loop. Maintainer decision 2026-06-26: ANY target
# beyond the bare app hands off to computer-use. The alternation is built from
# the known-app aliases (longest first, so "windows explorer" wins over
# "explorer"); a leading article/possessive between the preposition and the app
# is tolerated. Inputs are pre-``_normalize``'d (umlauts → ascii), so the
# alias keys (already ascii) match directly.
_APP_ALIAS_ALTERNATION = "|".join(
    re.escape(alias) for alias in sorted(_APP_ALIASES, key=len, reverse=True)
)
_APP_AS_CONTAINER_RE = re.compile(
    r"\b(?:in|im|ins|innerhalb|inside|within|into)\s+"
    r"(?:dem\s+|den\s+|der\s+|die\s+|das\s+|meinem\s+|meinen\s+|meiner\s+|"
    r"mein\s+|the\s+|my\s+)?"
    r"(?:" + _APP_ALIAS_ALTERNATION + r")\b",
    re.IGNORECASE,
)


def _app_used_as_container(normalized: str) -> bool:
    """True when a known app is the object of a container preposition.

    Signals "open <target> in <app>" (the app is a folder/window to navigate
    INTO), so a bare deterministic launch would silently drop the real target.
    Such goals route to the computer-use loop, never a single DIRECT open.
    Pure regex, no IO (AP-9 / AP-11)."""
    return bool(_APP_AS_CONTAINER_RE.search(normalized))


# Filler words / articles voice users sprinkle between the verb and the app
# name ("oeffne MIR chrome", "starte DEN editor", "mach FUER MICH chrome auf").
# Stripped before alias lookup so the canonical app name survives. Without this,
# "oeffne mir chrome" canonicalised to "mir chrome" -> no alias -> the command
# fell through to the brain instead of the deterministic local launch.
_APP_FILLER_WORDS = frozenset({
    "mir", "mal", "bitte", "doch", "den", "die", "das", "dem", "der",
    "ein", "eine", "einen", "mein", "meine", "meinen",
    "fuer", "mich", "uns", "the", "me", "my", "a", "an", "please",
})

_GERMAN_NUMBER_WORDS = {
    "ein": 1,
    "eine": 1,
    "einen": 1,
    "eins": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fuenf": 5,
}

_MAX_SCRIPTED_TERMINALS = 5

# Sentinel used as the default value for the ``_registry`` parameter of
# ``match_local_action``.  Using a dedicated object (not ``None``) lets the
# function distinguish "caller passed no registry → use real singleton" from
# "caller explicitly passed None → skip gate (registry unavailable)".
_SENTINEL: object = object()


# ADR-0016 L2 — voice-driven orb-recovery patterns. The regex is
# deliberately tight: anchored at ``^`` to reject substring matches and
# constrained to a small phrase list so general "wo bist du gerade?" or
# "weißt du wo der Bus ist?" queries do NOT trigger an orb reset.
# Inputs are run through ``_normalize`` first (lowercased, umlauts
# transliterated, leading "hey jarvis," stripped), so the patterns only
# need to match the post-normalised string.
_ORB_RESET_PATTERNS = (
    re.compile(
        r"^(?:hey\s+)?(?:jarvis[,\s]+)?(?:wo\s+bist\s+du|orb\s+zurueck"
        r"|reset\s+(?:den\s+)?orb)\s*[?.!]*$",
        re.I,
    ),
)


def _matches_orb_reset(text: str) -> bool:
    return any(pattern.match(text) for pattern in _ORB_RESET_PATTERNS)


# Fork addition (2026-11-14): read-only questions about running / installed
# programs are answered by the ``list_apps`` tool with REAL data instead of the
# model's imagination. Narrow, whole-phrase patterns only — ordinary
# conversation must never be mistaken for a system query.
_WAKE_PREFIX = r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис|jarvis)?[\s,]*"
_LIST_RUNNING_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:"
    r"(?:какие|что\s+за)\s+(?:сейчас\s+)?(?:программы|приложения|процессы)\s+"
    r"(?:сейчас\s+)?(?:запущены|открыты|работают|идут)"
    r"|что\s+(?:сейчас\s+)?(?:запущено|открыто|работает)(?:\s+на\s+(?:компьютере|пк))?"
    r"|(?:покажи|покажи\s+мне|выведи|перечисли|дай)\s+(?:мне\s+)?(?:все\s+)?"
    r"(?:запущенные|открытые|работающие)\s+(?:программы|приложения|процессы)"
    r"|(?:список|покажи\s+список)\s+(?:запущенных\s+(?:программ|приложений|процессов)|процессов)"
    r"|what(?:'s|\s+is)\s+running|list\s+(?:running\s+)?(?:processes|programs|apps)"
    r")[\s?.!]*$",
    re.I,
)
_LIST_INSTALLED_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:"
    r"(?:какие|что\s+за)\s+(?:программы|приложения)\s+(?:у\s+меня\s+)?(?:установлены|есть)"
    r"(?:\s+на\s+(?:компьютере|пк|дисках?))?"
    r"|(?:покажи|выведи|перечисли)\s+(?:все\s+)?(?:установленные|имеющиеся)\s+(?:программы|приложения)"
    r")[\s?.!]*$",
    re.I,
)
_SCAN_DISKS_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:просканируй|сканируй|проверь|обнови)\s+(?:мои\s+)?(?:диски|диск|компьютер|пк)"
    r"(?:\s+на\s+(?:наличие\s+)?(?:приложений|программ))?[\s?.!]*$",
    re.I,
)
_FIND_APP_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:найди|поищи|есть\s+ли|проверь,?\s+есть\s+ли)\s+(?:у\s+меня\s+)?"
    r"(?:приложение|программу|программа)\s+(?P<q>[\w\-. ]{2,40}?)"
    r"(?:\s+на\s+(?:компьютере|пк|дисках?))?[\s?.!]*$",
    re.I,
)


def _match_list_apps(text: str) -> LocalActionPlan | None:
    """Deterministic plan for 'which programs run / are installed' questions."""
    if _LIST_RUNNING_RE.match(text):
        args: dict[str, object] = {"scope": "running", "brief": True}
    elif _SCAN_DISKS_RE.match(text):
        args = {"scope": "installed", "rescan": True, "brief": True}
    elif _LIST_INSTALLED_RE.match(text):
        args = {"scope": "installed", "brief": True}
    else:
        found = _FIND_APP_RE.match(text)
        if found is None:
            return None
        args = {"scope": "installed", "query": found.group("q").strip()}
    return LocalActionPlan(
        mode=LocalActionMode.DIRECT,
        tool_calls=(LocalToolCall(name="list_apps", args=args),),
    )


# Fork addition (2026-11-14): volume, computer status and closing a program, as
# deterministic whole-phrase commands (voice and chat alike). Narrow on purpose.
_NUM = r"(?P<n>\d{1,3})"
_VOL_SET_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:(?:поставь|установи|сделай|выставь)\s+)?(?:громкость|звук)\s+(?:на\s+)?"
    + _NUM + r"\s*(?:процентов|процента|процент|%)?[\s?.!]*$",
    re.I,
)
_VOL_UP_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:(?:сделай\s+)?(?:по)?громче|прибавь\s+(?:звук|громкость)|увеличь\s+(?:звук|громкость)"
    r"|добавь\s+(?:звук|громкость))[\s?.!]*$",
    re.I,
)
_VOL_DOWN_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:(?:сделай\s+)?(?:по)?тише|убавь\s+(?:звук|громкость)|уменьши\s+(?:звук|громкость))[\s?.!]*$",
    re.I,
)
_VOL_MUTE_RE = re.compile(
    _WAKE_PREFIX + r"(?:(?:выключи|отключи)\s+звук|без\s+звука|mute)[\s?.!]*$", re.I,
)
_VOL_UNMUTE_RE = re.compile(
    _WAKE_PREFIX + r"(?:(?:включи|верни)\s+звук|unmute)[\s?.!]*$", re.I,
)
_VOL_GET_RE = re.compile(
    _WAKE_PREFIX + r"(?:какая\s+(?:сейчас\s+)?громкость|(?:на\s+сколько|сколько)\s+(?:процентов\s+)?громкость)[\s?.!]*$",
    re.I,
)
_STATUS_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:"
    r"(?:какое|покажи|дай)\s+(?:мне\s+)?(?:сейчас\s+)?(?:состояние|статус)\s+(?:компьютера|пк|системы)"
    r"|(?:проверь|покажи)\s+(?:состояние\s+)?(?:компьютер|пк|систему)"
    r"|как\s+(?:там\s+)?(?:мой\s+)?(?:компьютер|пк)(?:\s+себя\s+чувствует)?"
    r"|(?:как|насколько)\s+(?:загружен|нагружен)\w*\s+(?:процессор|компьютер|пк|видеокарта|система)"
    r"|сколько\s+(?:свободно\s+)?(?:памяти|места|оперативки)(?:\s+(?:на\s+(?:диске|дисках)|свободно))?"
    r"|(?:какая\s+)?температура\s+видеокарты|статус\s+системы|system\s+status"
    r")[\s?.!]*$",
    re.I,
)
_CLOSE_APP_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:закрой|закрыть|заверши|завершить)\s+(?:программу\s+|приложение\s+)?(?P<app>[\w\-. ]{2,40}?)[\s?.!]*$",
    re.I,
)


# Spoken name -> process-name fragment for "close X" (close_app matches process
# names and window titles; unknown names fall back to the open-app alias table).
_CLOSE_ALIASES: dict[str, str] = {
    "стим": "steam", "steam": "steam", "яндекс браузер": "browser",
    "яндекс": "browser", "браузер": "browser", "хром": "chrome",
    "гугл хром": "chrome", "телеграм": "telegram", "телеграмм": "telegram",
    "телега": "telegram", "дискорд": "discord", "спотифай": "spotify",
    "спотик": "spotify", "блокнот": "notepad", "диспетчер задач": "taskmgr",
    "обс": "obs64", "калькулятор": "calculator",
}


# Fork addition (2026-11-14): everyday-user commands. Whole-phrase, narrow.
_PC_SIMPLE: tuple[tuple[re.Pattern[str], str, dict[str, object]], ...] = tuple(
    (re.compile(_WAKE_PREFIX + r"(?:" + body + r")[\s?.!]*$", re.I), tool, args)
    for body, tool, args in (
        (r"(?:сделай|сними|сделать)\s+(?:скриншот|снимок\s+экрана)|скриншот|скрин", "pc_control", {"action": "screenshot"}),
        (r"(?:заблокируй|заблокировать)\s+(?:компьютер|пк|экран|систему)", "pc_control", {"action": "lock"}),
        (r"(?:переведи|отправь)\s+(?:компьютер|пк)\s+(?:в\s+)?(?:спящий\s+режим|сон)|режим\s+сна|усыпи\s+(?:компьютер|пк)",
         "pc_control", {"action": "sleep"}),
        (r"(?:выключи|выруби)\s+(?:компьютер|пк)|заверши\s+работу(?:\s+компьютера)?", "pc_control", {"action": "shutdown"}),
        (r"перезагрузи(?:\s+(?:компьютер|пк))?|перезагрузка(?:\s+компьютера)?", "pc_control", {"action": "restart"}),
        (r"отмени\s+(?:выключение|перезагрузку|завершение\s+работы)", "pc_control", {"action": "shutdown_cancel"}),
        (r"сверни\s+все(?:\s+окна)?|покажи\s+рабочий\s+стол", "pc_control", {"action": "show_desktop"}),
        (r"сверни\s+(?:это\s+|текущее\s+|активное\s+)?окно", "pc_control", {"action": "minimize_window"}),
        (r"разверни\s+(?:это\s+|текущее\s+|активное\s+)?окно(?:\s+на\s+весь\s+экран)?", "pc_control", {"action": "maximize_window"}),
        (r"закрой\s+(?:это\s+|текущее\s+|активное\s+)окно|закрой\s+окно", "pc_control", {"action": "close_window"}),
        (r"пауза|поставь\s+на\s+паузу|(?:останови|поставь\s+на\s+паузу|выключи|выруби|заглуши)\s+(?:музыку|песню|трек|воспроизведение|плеер)"
         r"|стоп|стой|тихо|хватит\s+музыки|включи\s+паузу",
         "spotify_control", {"action": "pause"}),
        (r"(?:включи|запусти|поставь|врубай|врубай|вруби|давай)\s+(?:снова\s+|опять\s+|какую-нибудь\s+|мою\s+)?(?:музыку|музон|песни|песню|плеер|спотифай|spotify)(?:\s+(?:в|на)\s+(?:спотифай|spotify))?"
         r"|(?:возобнови|продолжи|продолжай)\s+(?:музыку|воспроизведение|играть)|верни\s+музыку|играй|плей|воспроизведи",
         "spotify_control", {"action": "play_pause"}),
        (r"следующ\w+(?:\s+(?:трек|песн\w+|композици\w+))?|переключи(?:\s+(?:трек|песню|на\s+следующ\w+))?|включи\s+следующ\w+"
         r"|дальше|вперед|вперёд|скип|пропусти(?:\s+(?:трек|песню))?|next|другую\s+(?:песню|трек)|переключи\s+музыку|смени\s+(?:трек|песню)",
         "spotify_control", {"action": "next"}),
        (r"предыдущ\w+(?:\s+(?:трек|песн\w+|композици\w+))?|включи\s+предыдущ\w+|назад|верни\s+(?:трек|песню)|прошл\w+\s+(?:трек|песн\w+)|previous|заново(?:\s+(?:трек|песню))?",
         "spotify_control", {"action": "previous"}),
        (r"который\s+час|сколько\s+(?:сейчас\s+)?времени|какое\s+сегодня\s+число|какая\s+сегодня\s+дата|какой\s+сегодня\s+день",
         "pc_control", {"action": "time"}),
        (r"что\s+(?:сейчас\s+)?в\s+буфере\s+обмена|покажи\s+буфер\s+обмена", "pc_control", {"action": "clipboard_get"}),
        (r"(?:проверь|как)\s+(?:мой\s+)?(?:интернет|сеть|соединение)|есть\s+ли\s+интернет|работает\s+ли\s+интернет",
         "network_status", {}),
    )
)
_OPEN_FOLDER_RE = re.compile(
    _WAKE_PREFIX
    + r"открой\s+(?:папку\s+)?(?P<f>загрузки|документы|рабочий\s+стол|картинки|изображения|музыку|видео)[\s?.!]*$",
    re.I,
)
_FIND_FILE_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:найди|поищи)\s+(?:мне\s+)?(?:файл|папку|документ)\s+(?P<q>[\w\-. ]{2,40}?)"
    r"(?:\s+на\s+(?:компьютере|пк|дисках?))?[\s?.!]*$",
    re.I,
)
_SETTINGS_RE = re.compile(
    _WAKE_PREFIX
    + r"открой\s+настройки(?:\s+(?P<p>wi-?fi|вай-?фай|сети|интернета|bluetooth|блютуз|звука|экрана|обновлени\w+|питания|приложений|хранилища|конфиденциальности))?[\s?.!]*$",
    re.I,
)
_SETTINGS_PAGES = {
    "wifi": "wifi", "wi-fi": "wifi", "вайфай": "wifi", "вай-фай": "wifi", "сети": "network",
    "интернета": "network", "bluetooth": "bluetooth", "блютуз": "bluetooth", "звука": "sound",
    "экрана": "display", "питания": "power", "приложений": "apps", "хранилища": "storage",
    "конфиденциальности": "privacy",
}


def _match_pc_actions(text: str) -> LocalActionPlan | None:
    """Plans for everyday-PC phrases (windows, media, power, files, settings)."""

    def plan(tool: str, args: dict[str, object]) -> LocalActionPlan:
        return LocalActionPlan(
            mode=LocalActionMode.DIRECT,
            tool_calls=(LocalToolCall(name=tool, args=args),),
        )

    for pattern, tool, args in _PC_SIMPLE:
        if pattern.match(text):
            return plan(tool, dict(args))
    folder = _OPEN_FOLDER_RE.match(text)
    if folder is not None:
        return plan("file_ops", {"action": "open", "path": re.sub(r"\s+", " ", folder.group("f"))})
    found = _FIND_FILE_RE.match(text)
    if found is not None:
        return plan("file_ops", {"action": "find", "query": found.group("q").strip()})
    settings = _SETTINGS_RE.match(text)
    if settings is not None:
        raw = (settings.group("p") or "").lower()
        page = "update" if raw.startswith("обновлени") else _SETTINGS_PAGES.get(raw, "main")
        return plan("pc_control", {"action": "open_settings", "page": page})
    return None


_RU_TENS = {
    "ноль": 0, "десять": 10, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100,
    "максимум": 100, "полную": 100,
}
_VOL_WORDS_RE = re.compile(r"громкост|звук")
_VOL_NUM_RE = re.compile(
    r"(?:(?P<prep>на|до)\s+)?(?:(?P<d>\d{1,3})\s*(?:%|процент\w*)?|(?P<w>" + "|".join(_RU_TENS) + r")(?:\s+процент\w*)?)"
)
_VOL_REL_UP = ("увеличь", "прибавь", "добавь", "повысь", "подними")
_VOL_REL_DOWN = ("уменьши", "убавь", "понизь", "снизь", "опусти")


def _volume_flex(text: str) -> LocalActionPlan | None:
    """Free-word volume commands with a number: 'уменьши громкость до 50%',
    'громкость звука на пк установи 50%', 'увеличь громкость на 20'.

    Short utterances only (<= 9 words) and never questions, so ordinary talk
    about sound ("расскажи про звук 5.1") is left to the model."""
    if not _VOL_WORDS_RE.search(text) or "?" in text or len(text.split()) > 9:
        return None
    if re.search(r"какая|сколько|почему|как\s+(?:работает|настро)", text):
        return None
    num = _VOL_NUM_RE.search(text)
    if num is None:
        return None
    value = int(num.group("d")) if num.group("d") else _RU_TENS[num.group("w")]
    if not 0 <= value <= 100:
        return None
    relative = num.group("prep") == "на" and any(v in text for v in _VOL_REL_UP + _VOL_REL_DOWN)
    if relative:
        action = "up" if any(v in text for v in _VOL_REL_UP) else "down"
        args: dict[str, object] = {"action": action, "step": max(1, value)}
    else:
        args = {"action": "set", "level": value}
    return LocalActionPlan(
        mode=LocalActionMode.DIRECT,
        tool_calls=(LocalToolCall(name="set_volume", args=args),),
    )


def _match_system_control(text: str) -> LocalActionPlan | None:
    """Plans for volume / status / close-program phrases (see module note above)."""

    def plan(tool: str, args: dict[str, object]) -> LocalActionPlan:
        return LocalActionPlan(
            mode=LocalActionMode.DIRECT,
            tool_calls=(LocalToolCall(name=tool, args=args),),
        )

    flex = _volume_flex(text)
    if flex is not None:
        return flex
    found = _VOL_SET_RE.match(text)
    if found is not None:
        level = int(found.group("n"))
        if 0 <= level <= 100:
            return plan("set_volume", {"action": "set", "level": level})
        return None
    if _VOL_UP_RE.match(text):
        return plan("set_volume", {"action": "up"})
    if _VOL_DOWN_RE.match(text):
        return plan("set_volume", {"action": "down"})
    if _VOL_MUTE_RE.match(text):
        return plan("set_volume", {"action": "mute"})
    if _VOL_UNMUTE_RE.match(text):
        return plan("set_volume", {"action": "unmute"})
    if _VOL_GET_RE.match(text):
        return plan("set_volume", {"action": "get"})
    if _STATUS_RE.match(text):
        return plan("system_status", {})
    closing = _CLOSE_APP_RE.match(text)
    if closing is not None:
        spoken = re.sub(r"\s+", " ", closing.group("app").strip().lower())
        app = _CLOSE_ALIASES.get(spoken) or _canonical_app(closing.group("app"))
        if app is not None:
            return plan("close_app", {"name": app})
    return None


# Fork addition (2026-11-14): timers, weather, rates, calculator, notes, latest
# download and help. ``original`` keeps the user's casing for note/label text.
_RU_NUM_ATOMS = {
    "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "пятнадцать": 15,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
}
_NUM_TOKEN = r"(?:\d+|(?:" + "|".join(_RU_NUM_ATOMS) + r")(?:\s+(?:" + "|".join(_RU_NUM_ATOMS) + r"))?)"
_DUR_PART = r"(?:" + _NUM_TOKEN + r"\s*(?:секунд\w*|сек\w*|минут\w*|мин\w*|час\w*)|полчаса|(?:час|минуту|секунду)(?!\w))"
_DURATION = _DUR_PART + r"(?:\s+(?:и\s+)?" + _DUR_PART + r")*"
_DUR_PART_RE = re.compile(r"(?P<n>" + _NUM_TOKEN + r")\s*(?P<u>секунд\w*|сек\w*|минут\w*|мин\w*|час\w*)|(?P<half>полчаса)|(?P<one>час|минуту|секунду)(?!\w)")
_TIMER_SET_RE = re.compile(
    _WAKE_PREFIX + r"(?:(?:поставь|установи|заведи|запусти|сделай)\s+)?таймер\s+(?:на\s+)?(?P<d>" + _DURATION + r")[\s?.!]*$",
    re.I,
)
_REMIND_IN_RE = re.compile(
    _WAKE_PREFIX + r"напомни(?:\s+мне)?\s+через\s+(?P<d>" + _DURATION + r")(?:[,\s]+(?P<label>.+?))?[\s?.!]*$",
    re.I,
)
_REMIND_AT_RE = re.compile(
    _WAKE_PREFIX + r"напомни(?:\s+мне)?\s+в\s+(?P<h>\d{1,2})[:.\s](?P<m>\d{2})(?:[,\s]+(?P<label>.+?))?[\s?.!]*$",
    re.I,
)
_TIMER_LIST_RE = re.compile(
    _WAKE_PREFIX + r"(?:какие\s+(?:у\s+меня\s+)?(?:таймеры|напоминания)|сколько\s+осталось(?:\s+до\s+таймера)?|покажи\s+таймеры)[\s?.!]*$",
    re.I,
)
_TIMER_CANCEL_RE = re.compile(
    _WAKE_PREFIX + r"(?:отмени|сбрось|удали)\s+(?:все\s+|мой\s+)?(?:таймер\w*|напоминани\w+)[\s?.!]*$", re.I,
)
_WEATHER_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:(?:посмотри|покажи|узнай|скажи)\s+(?:мне\s+)?)?(?:(?:в\s+интернете|в\s+сети)\s+)?(?:какая\s+)?(?:сейчас\s+)?погод[ауы]"
    r"(?:\s+на\s+улице)?(?:\s+(?P<w1>сейчас|сегодня|завтра))?(?:\s+(?:в|во)\s+(?P<city>[\w\- ]{2,30}?))?(?:\s+(?P<w2>сейчас|сегодня|завтра))?[\s?.!]*$",
    re.I,
)
_RATES_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:(?:какой|скажи|покажи)\s+(?:мне\s+)?)?(?:сегодняшний\s+)?(?:курс\s+(?P<cur>доллара|доллар|евро|юаня|юань|фунта|тенге|лиры|иены)|курс\s+валют"
    r"|сколько\s+(?:сейчас\s+)?стоит\s+(?P<cur2>доллар|евро|юань))(?:\s+(?:сегодня|сейчас|к\s+рублю))?[\s?.!]*$",
    re.I,
)
_CALC_RE = re.compile(_WAKE_PREFIX + r"(?:посчитай|сколько\s+будет|вычисли|реши|calc)\s+(?P<e>.+?)[\s?.!]*$", re.I)
_NOTE_ADD_RE = re.compile(
    _WAKE_PREFIX + r"(?:запиши|добавь|сохрани|запомни)\s+(?:мне\s+)?(?:в\s+)?(?:заметк\w+|заметки)[\s:,\-]*(?P<t>.+?)[\s]*$", re.I,
)
_NOTE_READ_RE = re.compile(_WAKE_PREFIX + r"(?:прочитай|покажи|открой)\s+(?:мои\s+)?заметки[\s?.!]*$", re.I)
_LATEST_OPEN_RE = re.compile(
    _WAKE_PREFIX + r"открой\s+последни\w+\s+(?:скачанн\w+\s+)?файл[\s?.!]*$", re.I,
)
_LATEST_SHOW_RE = re.compile(
    _WAKE_PREFIX + r"(?:что\s+я\s+(?:последним\s+)?скачал(?:\s+последним)?|какой\s+последний\s+скачанн\w+\s+файл|последний\s+скачанный\s+файл)[\s?.!]*$",
    re.I,
)
_HELP_RE = re.compile(
    _WAKE_PREFIX + r"(?:что\s+ты\s+умеешь|что\s+ты\s+можешь|помощь|справка|список\s+команд|help)[\s?.!]*$", re.I,
)


def _num_value(token: str) -> int:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return sum(_RU_NUM_ATOMS.get(word, 0) for word in token.split())


def _duration_seconds(text: str) -> float:
    total = 0.0
    for m in _DUR_PART_RE.finditer(text):
        if m.group("half"):
            total += 1800.0
            continue
        if m.group("one"):
            total += {"час": 3600.0, "минуту": 60.0, "секунду": 1.0}[m.group("one")]
            continue
        n = _num_value(m.group("n"))
        unit = m.group("u")
        total += n * (3600.0 if unit.startswith("час") else 60.0 if unit.startswith("мин") else 1.0)
    return total


def _clean_label(raw: str | None) -> str:
    label = (raw or "").strip()
    label = re.sub(r"^(?:что|чтобы|о\s+том,?\s+что|про|насчёт|насчет)\s+", "", label, flags=re.I)
    return label[:120]


def _match_extras(text: str, original: str) -> LocalActionPlan | None:
    """Plans for timers, weather, rates, calculator, notes, latest file and help."""

    def plan(tool: str, args: dict[str, object]) -> LocalActionPlan:
        return LocalActionPlan(
            mode=LocalActionMode.DIRECT,
            tool_calls=(LocalToolCall(name=tool, args=args),),
        )

    found = _TIMER_SET_RE.match(text)
    if found is not None:
        seconds = _duration_seconds(found.group("d"))
        return plan("set_timer", {"action": "set", "seconds": seconds}) if seconds >= 1 else None
    orig = _REMIND_IN_RE.match(original)
    if orig is not None:
        seconds = _duration_seconds(_normalize(orig.group("d")))
        if seconds >= 1:
            return plan("set_timer", {"action": "set", "seconds": seconds, "label": _clean_label(orig.group("label"))})
        return None
    orig = _REMIND_AT_RE.match(original)
    if orig is not None:
        return plan("set_timer", {"action": "set", "at": f"{int(orig.group('h')):02d}:{orig.group('m')}",
                                  "label": _clean_label(orig.group("label"))})
    if _TIMER_LIST_RE.match(text):
        return plan("set_timer", {"action": "list"})
    if _TIMER_CANCEL_RE.match(text):
        return plan("set_timer", {"action": "cancel"})
    weather = _WEATHER_RE.match(text)
    if weather is not None:
        when = weather.group("w1") or weather.group("w2") or "сегодня"
        args: dict[str, object] = {"day": "tomorrow" if when == "завтра" else "today"}
        city = (weather.group("city") or "").strip()
        if city:
            args["city"] = city
        return plan("get_weather", args)
    rates = _RATES_RE.match(text)
    if rates is not None:
        cur = rates.group("cur") or rates.group("cur2") or ""
        return plan("get_rates", {"currency": cur})
    note = _NOTE_ADD_RE.match(original)
    if note is not None and note.group("t").strip():
        from datetime import datetime

        stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
        notes_path = "документы" + chr(92) + "Jarvis" + chr(92) + "Заметки.txt"
        return plan("file_ops", {"action": "write", "path": notes_path,
                                 "text": f"[{stamp}] {note.group('t').strip()}" + chr(10), "append": True})
    if _NOTE_READ_RE.match(text):
        return plan("file_ops", {"action": "read", "path": "документы" + chr(92) + "Jarvis" + chr(92) + "Заметки.txt"})
    if _LATEST_OPEN_RE.match(text):
        return plan("file_ops", {"action": "latest", "path": "загрузки", "open": True})
    if _LATEST_SHOW_RE.match(text):
        return plan("file_ops", {"action": "latest", "path": "загрузки"})
    if _HELP_RE.match(text):
        return plan("pc_control", {"action": "help"})
    calc = _CALC_RE.match(original)
    if calc is not None and re.search(r"\d", calc.group("e")):
        return plan("calculate", {"expression": calc.group("e")})
    return None


# Fork addition (2026-11-14): Spotify, Steam and Happ phrases. Whole-phrase and narrow.
_LETTER_FIX = str.maketrans("СКЕФВАНМОРТХсскефванморт", "CKEFBAHMOPTXcckefbahmopt")
_STEAM_WORD = r"(?:стим\w*|steam)"
_HAPP_WORD = r"(?:happ|хапп\w*|хэпп\w*|хепп\w*)"
_SPOTIFY_ACTIONS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(_WAKE_PREFIX + r"(?:" + body + r")[\s?.!]*$", re.I), action)
    for body, action in (
        (r"что\s+(?:сейчас\s+)?играет|как\s+называется\s+(?:эта\s+|текущая\s+)?(?:песня|трек|композиция)|какой\s+(?:сейчас\s+)?трек(?:\s+играет)?",
         "now_playing"),
        (r"(?:включи|поставь)\s+(?:случайн\w+\s+(?:трек|песню|композицию))|случайн\w+\s+(?:трек|песня|песню)|включи\s+что-нибудь\s+случайное",
         "random_track"),
        (r"(?:включи|включить)\s+(?:перемешивание|шафл|шаффл|случайный\s+порядок)|перемешай\s+(?:треки|музыку)", "shuffle_on"),
        (r"(?:выключи|отключи|выключить)\s+(?:перемешивание|шафл|шаффл|случайный\s+порядок)", "shuffle_off"),
        (r"(?:включи|включить)\s+повтор(?:\s+трека|\s+песни)?|зацикли(?:\s+трек|\s+песню)?", "repeat_on"),
        (r"(?:выключи|отключи|выключить)\s+повтор(?:\s+трека|\s+песни)?", "repeat_off"),
        (r"добавь\s+(?:этот\s+|текущий\s+)?(?:трек|песню)\s+в\s+(?:любимое|любимые|избранное)|лайкни(?:\s+(?:трек|песню))?|поставь\s+лайк(?:\s+треку)?",
         "like"),
        (r"(?:убери|удали|сними)\s+(?:этот\s+|текущий\s+)?(?:трек|песню)\s+из\s+(?:любимого|любимых|избранного)|сними\s+лайк(?:\s+с\s+(?:трека|песни))?",
         "unlike"),
        (r"(?:включи|запусти|поставь)\s+радио\s+по\s+(?:этому\s+|текущему\s+)?(?:треку|песне)|радио\s+по\s+(?:этому\s+|текущему\s+)?(?:треку|песне)",
         "radio"),
    )
)
_STEAM_INSTALL_DRIVE_RE = re.compile(
    _WAKE_PREFIX + r"(?:скачай|установи|загрузи|поставь)\s+(?P<g>[^,]+?)\s+(?:в\s+" + _STEAM_WORD + r"\s+)?(?:на|в)\s+(?:диск\s+)?(?P<d>[a-zа-я])(?![a-zа-яё0-9])(?:\s+диск)?[\s?.!]*$",
    re.I,
)
_STEAM_INSTALL_RE = re.compile(
    _WAKE_PREFIX + r"(?:скачай|установи|загрузи|поставь)\s+(?P<g>[^,]+?)\s+(?:в|через)\s+" + _STEAM_WORD + r"(?:\s+на\s+диск\s+по\s+умолчанию)?",
    re.I,
)
_STEAM_RUN_RE = re.compile(
    _WAKE_PREFIX + r"(?:запусти|включи|открой)\s+(?:игру\s+)?(?P<g>[^,]+?)\s+(?:в|через)\s+" + _STEAM_WORD, re.I,
)
# "открой в стиме Dota 2" / "открой стим игру Dota 2": the game comes after Steam.
_STEAM_RUN_AFTER_RE = re.compile(
    _WAKE_PREFIX + r"(?:запусти|включи|открой)\s+(?:(?:в|через)\s+" + _STEAM_WORD + r"|" + _STEAM_WORD + r")\s+(?:игру\s+)?(?P<g>[^,]+?)[\s?.!]*$", re.I,
)
# Just opening the client: "открой стим", "открой игру в стиме" (no game named).
_STEAM_OPEN_RE = re.compile(
    _WAKE_PREFIX + r"(?:запусти|включи|открой|открыть|запустить)\s+(?:(?:игру|игры|библиотеку)\s+(?:в|через)\s+)?(?:(?:в|через)\s+)?" + _STEAM_WORD + r"(?:\s+(?:игру|игры))?[\s?.!]*$", re.I,
)
_STEAM_DOWNLOADS_RE = re.compile(
    _WAKE_PREFIX + r"(?:(?:открой|покажи)\s+загрузки\s+(?:в\s+)?(?:" + _STEAM_WORD + r"|стима)|что\s+(?:сейчас\s+)?качается\s+в\s+" + _STEAM_WORD + r")",
    re.I,
)
_STEAM_PAUSE_RE = re.compile(
    _WAKE_PREFIX + r"(?:останови|приостанови|поставь\s+на\s+паузу)\s+загрузки(?:\s+в\s+" + _STEAM_WORD + r"|\s+стима)?", re.I,
)
_STEAM_RESUME_RE = re.compile(
    _WAKE_PREFIX + r"(?:возобнови|продолжи|запусти)\s+загрузки(?:\s+в\s+" + _STEAM_WORD + r"|\s+стима)?", re.I,
)
_STEAM_LIBS_RE = re.compile(
    _WAKE_PREFIX + r"(?:какие\s+(?:у\s+меня\s+)?библиотеки\s+(?:в\s+)?" + _STEAM_WORD + r"|на\s+каких\s+дисках\s+" + _STEAM_WORD + r"|сколько\s+места\s+(?:для\s+игр|в\s+" + _STEAM_WORD + r"))[\s?.!]*$",
    re.I,
)
_STEAM_STATUS_WORDS = (
    r"в\s+сети|онлайн|нет\s+на\s+месте|отошёл|отошел|невидимк\w+|невидим\w+|не\s+в\s+сети|оффлайн|офлайн|не\s+беспокоить"
)
_STEAM_STATUS_RE = re.compile(
    _WAKE_PREFIX
    + r"(?:(?:поставь|смени|измени|переключи)\s+(?:мой\s+)?статус\s+(?:в\s+" + _STEAM_WORD + r"\s+)?(?:на\s+)?(?P<s1>" + _STEAM_STATUS_WORDS + r")"
    r"(?:\s+в\s+" + _STEAM_WORD + r")?"
    r"|(?:стань|будь)\s+(?P<s2>" + _STEAM_STATUS_WORDS + r")\s+в\s+" + _STEAM_WORD +
    r"|в\s+" + _STEAM_WORD + r"\s+(?:стань|будь|поставь\s+статус)\s+(?P<s3>" + _STEAM_STATUS_WORDS + r"))[\s?.!]*$",
    re.I,
)
_STATUS_CANON = {
    "онлайн": "в сети", "отошёл": "нет на месте", "отошел": "нет на месте", "оффлайн": "не в сети", "офлайн": "не в сети",
}
_HAPP_PING_RE = re.compile(
    _WAKE_PREFIX + r"(?:запусти|проверь|сделай|замерь)\s+пинг(?:\s+серверов)?(?:\s+(?:в|во)\s+" + _HAPP_WORD + r")?[\s?.!]*$", re.I,
)
_HAPP_FASTEST_RE = re.compile(
    _WAKE_PREFIX + r"(?:подключись|подключи|подключиться)\s+(?:(?:впн|vpn)\s+)?(?:к|на)\s+(?:самому\s+быстрому|лучшему|ближайшему|быстрому)"
    r"(?:\s+серверу)?(?:\s+(?:в|во)\s+" + _HAPP_WORD + r")?[\s?.!]*$"
    r"|" + _WAKE_PREFIX + r"(?:подключись|подключи|подключиться)\s+к\s+серверу\s+с\s+(?:минимальн\w+|наименьш\w+|самой\s+низкой)\s+(?:задержк\w+|пинг\w*)"
    r"(?:\s+(?:в|во)\s+" + _HAPP_WORD + r")?[\s?.!]*$",
    re.I,
)
_HAPP_CONNECT_RE = re.compile(
    _WAKE_PREFIX + r"(?:подключись|подключи|подключиться)\s+к\s+серверу\s+(?P<s>[\w\-| ]{2,40}?)(?:\s+(?:в|во)\s+" + _HAPP_WORD + r")?[\s?.!]*$",
    re.I,
)
_HAPP_STATUS_RE = re.compile(
    _WAKE_PREFIX + r"(?:включ[её]н\s+ли\s+(?:впн|vpn)|работает\s+ли\s+(?:впн|vpn)|статус\s+(?:впн|vpn)|подключ[её]н\s+ли\s+(?:впн|vpn))[\s?.!]*$", re.I,
)


_STEAM_FLEX_STATES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.I), state)
    for pattern, state in (
        (r"не\s+в\s+сети|офф?лайн|offline", "не в сети"),
        (r"н[еи]в[еи]дим\w*|invisible", "невидимка"),
        (r"нет\s+на\s+месте|отошёл|отошел|афк|away", "нет на месте"),
        (r"не\s+беспокоить|занят\w*|busy", "не беспокоить"),
        (r"в\s+сети|онлайн|online", "в сети"),
    )
)
_STEAM_FLEX_SKIP = re.compile(r"почему|зачем|как\s|кто\s|сколько|что\s|когда|где\s", re.I)


def _steam_status_flex(text: str) -> str | None:
    """Free-word Steam status commands ("статус друзей в стиме в сети",
    "поставь в стиме невидимка", "статус стим нет на месте").

    Needs the word Steam, "статус" or an imperative, one state word, a short
    sentence and no question word: talk ABOUT Steam statuses is left to the model.
    """
    if "?" in text or len(text.split()) > 10 or _STEAM_FLEX_SKIP.search(text):
        return None
    if not re.search(_STEAM_WORD, text):
        return None
    if not re.search(r"статус|поставь|смени|измени|переключи|стань|будь|сделай", text):
        return None
    for pattern, state in _STEAM_FLEX_STATES:
        if pattern.search(text):
            return state
    return None


def _match_apps(text: str, original: str) -> LocalActionPlan | None:
    """Plans for Spotify, Steam and Happ phrases."""

    def plan(tool: str, args: dict[str, object]) -> LocalActionPlan:
        return LocalActionPlan(
            mode=LocalActionMode.DIRECT,
            tool_calls=(LocalToolCall(name=tool, args=args),),
        )

    for pattern, action in _SPOTIFY_ACTIONS:
        if pattern.match(text):
            return plan("spotify_control", {"action": action})
    flex_state = _steam_status_flex(text)
    if flex_state is not None:
        return plan("steam_control", {"action": "friend_status", "state": flex_state})
    st = _STEAM_STATUS_RE.match(text)
    if st is not None:
        state = next(g for g in (st.group("s1"), st.group("s2"), st.group("s3")) if g)
        state = re.sub(r"\s+", " ", state.strip())
        if state.startswith("невидим"):
            state = "невидимка"
        return plan("steam_control", {"action": "friend_status", "state": _STATUS_CANON.get(state, state)})
    if _STEAM_DOWNLOADS_RE.match(text):
        return plan("steam_control", {"action": "open_downloads"})
    if _STEAM_PAUSE_RE.match(text):
        return plan("steam_control", {"action": "pause_downloads"})
    if _STEAM_RESUME_RE.match(text):
        return plan("steam_control", {"action": "resume_downloads"})
    if _STEAM_LIBS_RE.match(text):
        return plan("steam_control", {"action": "libraries"})
    drive = _STEAM_INSTALL_DRIVE_RE.match(original)
    if drive is not None:
        letter = drive.group("d").translate(_LETTER_FIX).upper()
        game = re.sub(r"\s+(?:в|через)\s+" + _STEAM_WORD + r"$", "", drive.group("g").strip(), flags=re.I)
        return plan("steam_control", {"action": "install", "game": game, "drive": letter})
    inst = _STEAM_INSTALL_RE.match(original)
    if inst is not None:
        return plan("steam_control", {"action": "install", "game": inst.group("g").strip()})
    if _STEAM_OPEN_RE.match(text):
        return plan("open_app", {"app_name": "steam"})
    run = _STEAM_RUN_RE.match(original) or _STEAM_RUN_AFTER_RE.match(original)
    if run is not None:
        return plan("steam_control", {"action": "run", "game": run.group("g").strip()})
    if _HAPP_PING_RE.match(text):
        return plan("happ_vpn", {"action": "ping"})
    if _HAPP_FASTEST_RE.match(text):
        return plan("happ_vpn", {"action": "connect_fastest"})
    if _HAPP_STATUS_RE.match(text):
        return plan("happ_vpn", {"action": "status"})
    conn = _HAPP_CONNECT_RE.match(original)
    if conn is not None:
        return plan("happ_vpn", {"action": "connect", "server": conn.group("s").strip()})
    return None


def _unsupported_response(text: str, lang: str) -> str:
    """Return a deterministic, no-LLM rejection message.

    The phrasing is taken verbatim from the capability-coupling spec so that
    TTS reads it aloud and the user knows *why* Jarvis declined instead of
    hearing a confused silence.

    Parameters
    ----------
    text:
        The normalised utterance (used only for potential future interpolation;
        currently unused in the message body).
    lang:
        ``"de"`` (default) or ``"en"``.
    """
    if lang == "en":
        return (
            "I can't do that yet. "
            "I don't have a registered tool for it. "
            "Tell me which MCP or integration should handle it and I can learn."
        )
    return (
        "Das kann ich noch nicht. "
        "Mir fehlt dafür ein Werkzeug — wenn du mir verrätst welches MCP "
        "oder welche Integration zuständig wäre, kann ich's lernen."
    )


# A DISPATCH verb: the user wants to drive a real external system (send, play,
# post, book, order, call), not merely build/analyse/parse something. This is
# the disambiguator that separates "schick eine Email" (real dispatch) from
# "implementier eine Email-Validation" (coding task that just NAMES the domain).
# Deliberately excludes ambiguous build/write verbs (schreib/baue/mach) — those
# are generic sub-agent work even next to an integration name.
_EXTERNAL_DISPATCH_VERB_RE = re.compile(
    r"\b("
    r"schick\w*|sende[nt]?|send|verschick\w*|"          # send
    r"poste[nt]?|post|tweete[nt]?|"                      # post
    r"spiel\w*|play|"                                    # play media
    r"trag\w*|eintrag\w*|book|buch\w*|bestell\w*|order[ns]?|"  # book/order/enter
    r"reservier\w*|reserve|"
    r"ruf\w*\s+an|ruf\s+\w+\s+an|call|anruf\w*|"         # call
    r"like[nt]?|liken|folge[nt]?|abonnier\w*"            # social interact
    r")\b",
    re.I,
)

# A SPECIFIC external integration noun a generic sub-agent worker cannot reach
# without a dedicated MCP/connector — a real inbox, calendar, Spotify session,
# social account, or delivery service. The noun ALONE is not enough (a coding
# task may merely mention it); a dispatch verb must also be present. git/GitHub
# is deliberately absent — the worker has git + gh natively.
_EXTERNAL_INTEGRATION_NOUN_RE = re.compile(
    r"\b("
    # Mail
    r"e-?mails?|gmail|outlook|postfach|"
    # Messaging
    r"whats-?app|telegram|signal|imessage|sms|"
    # Calendar
    r"kalender|calendar|termine?|appointments?|"
    # Music
    r"spotify|youtube\s*music|yt\s*music|"
    # Social
    r"tweets?|twitter|instagram|facebook|linkedin|tiktok|"
    # Real-world commerce / transport
    r"pizza|uber\s*eats|lyft|doordash|lieferando|"
    # Travel / lodging / ticketing — no booking integration exists, so
    # "book a trip/flight/hotel" must refuse honestly, not 3-loop-fail a worker
    # (live gap 2026-06-14, mission_019ec761 critic_loop_exhausted).
    r"flights?|fl[uü]ge?|trips?|reisen?|hotels?|unterk[uü]nfte?|airbnb|motel|"
    r"tickets?|zugticket|bahnticket|flugticket|mietwagen|rental\s+car|"
    # Dining reservation
    r"tisch|restaurant|reservierung"
    r")\b",
    re.I,
)


def requires_external_integration(text: str) -> bool:
    """True iff the utterance asks to DRIVE a specific external system a generic
    sub-agent worker cannot reach (send mail, play Spotify, post a tweet, book a
    table, order food).

    Both signals are required: a SPECIFIC integration noun AND a real DISPATCH
    verb. The noun alone is not enough — a coding task that merely mentions the
    domain ("implementier eine Email-Validation", "baue einen Kalender-Parser",
    "schreib Code der Spotify-Playlists liest") is generic sub-agent work and
    must NOT be refused. git/GitHub is never matched (the worker drives them).

    Used by the capability gate AND the force-spawn heuristic to draw the single
    line between "refuse honestly (no tool exists)" and "delegate to the
    sub-agent (the universal capability for generic work)".
    """
    t = text or ""
    if not _EXTERNAL_DISPATCH_VERB_RE.search(t):
        return False
    if _EXTERNAL_INTEGRATION_NOUN_RE.search(t):
        return True
    # Twitter/X rebrand: bare "x" is too ambiguous (math variable, letter),
    # but "auf X" / "on X" + a posting dispatch verb unambiguously means the
    # social platform. The preposition is the minimal disambiguator.
    if re.search(r"\b(?:auf|on)\s+x\b", t, re.I):
        return True
    return False


def external_integration_terms(text: str) -> tuple[str, ...]:
    """Name the SPECIFIC external systems an utterance asks for.

    Companion to :func:`requires_external_integration`: that function answers
    *whether* a dedicated integration is needed, this one answers *which* one.
    The terms are what the live tool surface is checked against before any
    refusal is spoken (GT-16) — the registry may be stale, an attached tool
    named after the system is not.
    """
    t = text or ""
    terms = [m.group(0).lower() for m in _EXTERNAL_INTEGRATION_NOUN_RE.finditer(t)]
    if re.search(r"\b(?:auf|on)\s+x\b", t, re.I):
        # The Twitter/X rebrand hides the platform behind a single letter —
        # hand the matcher the names a connected tool would actually carry.
        terms.extend(("twitter", "x"))
    return tuple(dict.fromkeys(terms))


def _live_surface_covers(terms: Sequence[str], tool_names: Sequence[str]) -> bool:
    """Thin wrapper around ``evidence_gate.live_surface_covers``.

    Late import for the same reason as :func:`_get_capability_registry` — this
    module stays importable without pulling in ``jarvis.core.capabilities``.
    A fault here must never turn into a refusal, so it degrades to "not
    covered" only when the helper itself is missing.
    """
    if not tool_names or not terms:
        return False
    try:
        from jarvis.brain.evidence_gate import live_surface_covers
    except ImportError:  # pragma: no cover — module always ships with the gate
        return False
    return live_surface_covers(terms, tool_names)


def _get_capability_registry() -> _CapabilityRegistryLike | None:
    """Return the global capability registry singleton, or *None* if Agent A's
    module has not been installed yet.

    Using a late import here keeps the gate fast-importable even before
    ``jarvis.core.capabilities`` exists on disk.
    """
    try:
        from jarvis.core.capabilities import get_registry  # type: ignore[import]

        return get_registry()
    except (ImportError, AttributeError):
        return None


_WIN_MOVE_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис|jarvis)?[\s,]*(?:пожалуйста\s+)?(?:перенеси|перемести|передвинь|перекинь|кинь|отправь|закинь|переставь|двигай|перетащи)\s+"
    r"(?P<w>.*?)\s*(?:на|в|во)\s+(?P<tail>(?:[\wё]+\s+)?(?:монитор|экран|дисплей)(?:\s+(?:номер\s+)?[\wё]+)?)[\s?.!]*$",
    re.I,
)
_WIN_SNAP_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис|jarvis)?[\s,]*(?:пожалуйста\s+)?(?:прижми|сдвинь|поставь|привяжи|разместить|размести|пристыкуй|подвинь)\s+"
    r"(?P<w>.*?)\s*(?:(?:к|на)\s+)?(?P<s>влево|вправо|налево|направо|слева|справа|лев\w+|прав\w+|вверх|вниз|по\s+центру)(?:\s+(?:половин\w+|сторон\w+|кра\w+))?[\s?.!]*$",
    re.I,
)
_WIN_LIST_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис|jarvis)?[\s,]*(?:какие\s+(?:у\s+меня\s+)?мониторы|сколько\s+(?:у\s+меня\s+)?мониторов|покажи\s+мониторы|"
    r"какие\s+(?:у\s+меня\s+)?экраны)[\s?.!]*$",
    re.I,
)
_WIN_WORD_FILLER = re.compile(r"\b(?:окно|окна|это|эту|текущее|текущую|активное|активную|мое|моё|мой|программу|приложение|пожалуйста)\b")
_MONITOR_WORDS = {"первый": 1, "первом": 1, "один": 1, "второй": 2, "втором": 2, "два": 2, "третий": 3, "три": 3}
_SIDE_CANON = (("лев", "left"), ("слева", "left"), ("влево", "left"), ("налево", "left"), ("прав", "right"), ("справа", "right"),
               ("вправо", "right"), ("направо", "right"), ("верх", "top"), ("низ", "bottom"))


def _window_name(raw: str) -> str:
    return re.sub(r"\s+", " ", _WIN_WORD_FILLER.sub(" ", raw.lower())).strip()


def _match_window_control(original: str) -> LocalActionPlan | None:
    """Window moves between monitors, snapping and the monitor list (fork addition)."""

    def plan(args: dict[str, object]) -> LocalActionPlan:
        return LocalActionPlan(mode=LocalActionMode.DIRECT, tool_calls=(LocalToolCall(name="window_control", args=args),))

    if _WIN_LIST_RE.match(original):
        return plan({"action": "list_monitors"})
    m = _WIN_MOVE_RE.match(original)
    if m is not None:
        tail = m.group("tail").lower()
        monitor = "next"
        digits = re.search(r"\d+", tail)
        if digits:
            monitor = digits.group()
        else:
            for word, number in _MONITOR_WORDS.items():
                if re.search(rf"\b{word}\b", tail):
                    monitor = str(number)
                    break
            if monitor == "next" and re.search(r"основн|главн", tail):
                monitor = "primary"
        return plan({"action": "move_to_monitor", "window": _window_name(m.group("w")), "monitor": monitor})
    m = _WIN_SNAP_RE.match(original)
    if m is not None:
        side_raw = m.group("s").lower()
        if side_raw.startswith("по"):
            return plan({"action": "center", "window": _window_name(m.group("w"))})
        side = next((canon for key, canon in _SIDE_CANON if key in side_raw), "left")
        return plan({"action": "snap", "window": _window_name(m.group("w")), "side": side})
    return None


_PRE = r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис|jarvis)?[\s,]*(?:пожалуйста\s+)?"
_BR_SEARCH_RE = re.compile(
    _PRE + r"(?:(?:найди|поищи|ищи|покажи)\s+(?:в\s+|на\s+)(?P<e>яндексе|гугле|google|ютубе|ютюбе|youtube)\s+(?P<q>.+?)"
    r"|(?:загугли|погугли|гугли)\s+(?P<q2>.+?)|(?:яндексни|найди\s+в\s+яндексе)\s+(?P<q3>.+?))[\s?.!]*$", re.I,
)
_BR_OPEN_SITE_RE = re.compile(
    _PRE + r"(?:открой|открыть|зайди\s+на|перейди\s+на|запусти)\s+(?:сайт\s+)?(?P<s>[\w .\-]{2,40}?)[\s?.!]*$", re.I,
)
_BR_TAB_RE = tuple(
    (re.compile(_PRE + body + r"[\s?.!]*$", re.I), action)
    for body, action in (
        (r"(?:открой|создай|новая|нужна)\s+(?:новую\s+)?вкладк\w*|новая\s+вкладка", "new_tab"),
        (r"закрой\s+(?:эту\s+|текущую\s+)?вкладк\w*", "close_tab"),
        (r"(?:верни|открой\s+снова|восстанови)\s+(?:закрытую\s+)?вкладк\w*", "reopen_tab"),
        (r"(?:следующая|следующую|вперёд|вперед)\s+вкладк\w*|переключи\s+вкладк\w*|на\s+следующую\s+вкладку", "next_tab"),
        (r"(?:предыдущая|предыдущую|прошлая|прошлую)\s+вкладк\w*|на\s+предыдущую\s+вкладку", "prev_tab"),
        (r"обнови\s+(?:эту\s+)?страницу|перезагрузи\s+страницу|обнови\s+вкладку", "reload"),
        (r"(?:вернись|назад)\s+(?:на\s+)?(?:предыдущую\s+)?страниц\w*|страница\s+назад", "back"),
        (r"открой\s+(?:новое\s+)?окно\s+браузера|новое\s+окно\s+браузера", "new_window"),
        (r"(?:открой\s+)?(?:режим\s+)?инкогнито|приватное\s+окно", "incognito"),
        (r"(?:открой\s+)?историю\s+браузера", "history"),
        (r"(?:открой\s+)?загрузки\s+браузера", "downloads"),
    )
)
_WIN_NAMED_RE = re.compile(
    _PRE + r"(?P<v>сверни|разверни|переключись\s+на|перейди\s+в|перейди\s+на|покажи\s+окно|вернись\s+в|вернись\s+на)\s+"
    r"(?:окно\s+|программу\s+|приложение\s+)?(?P<w>[\w .\-]{2,40}?)[\s?.!]*$", re.I,
)
_WIN_NAMED_SKIP = re.compile(r"^(?:все|всё|это|этот|текущее|активное|окно|окна|экран|рабочий стол)(?:\s+окна)?$")
_WIN_TILE_RE = re.compile(
    _PRE + r"(?:расположи|размести|разложи|выстрой)\s+(?:все\s+)?окна\s*(?:рядом|плиткой|сеткой|друг\s+с\s+другом)?"
    r"(?:\s+(?P<n>два|три|четыре|\d))?[\s?.!]*$|" + _PRE + r"окна\s+(?:рядом|плиткой|сеткой)[\s?.!]*$", re.I,
)
_WIN_LAYOUT_SAVE_RE = re.compile(
    _PRE + r"запомни\s+(?:расположение|раскладку)\s+окон(?:\s+как)?\s*(?P<n>[\w .\-]{0,30}?)[\s?.!]*$", re.I,
)
_WIN_LAYOUT_LOAD_RE = re.compile(
    _PRE + r"(?:верни|восстанови|включи)\s+(?:расположение|раскладку)\s+окон\s*(?P<n>[\w .\-]{0,30}?)[\s?.!]*$", re.I,
)
_MEM_REMEMBER_RE = re.compile(_PRE + r"(?:запомни|запиши\s+в\s+память|запомни\s+пожалуйста)(?:\s*,)?\s+(?:что\s+)?(?P<t>.{4,300}?)[\s?.!]*$", re.I)
_MEM_RECALL_RE = re.compile(
    _PRE + r"(?:что\s+ты\s+(?:помнишь|знаешь)\s+(?:про|о|об|обо)|вспомни(?:\s+(?:про|о|об))?|что\s+я\s+(?:тебе\s+)?(?:говорил|рассказывал)\s+(?:про|о|об))\s+"
    r"(?P<q>.{2,100}?)[\s?.!]*$", re.I,
)
_MEM_HISTORY_RE = re.compile(
    _PRE + r"что\s+(?:я\s+(?:тебя\s+)?(?:просил|делал|говорил|командовал)|мы\s+(?:делали|сделали)|ты\s+(?:сделал|делал))\s*"
    r"(?P<d>сегодня|вчера|позавчера)?[\s?.!]*$", re.I,
)
_SCENE_RE = re.compile(
    _PRE + r"(?:включи|запусти|активируй|давай|поставь|врубай)\s+(?:(?:режим|сценарий)\s+(?P<a>[\w\- ]{2,30}?)|(?P<b>[\w\-]{2,30}?)\s+(?:режим|сценарий))[\s?.!]*$", re.I,
)
_WORD_NUM = {"два": 2, "три": 3, "четыре": 4}


_COURSE_STATUS_RE = re.compile(
    _PRE + r"(?:(?:какой\s+)?статус\s+(?:курса|обучения|изучения)|как\s+(?:идёт|идет|продвигается)\s+(?:курс|обучение|изучение)|что\s+ты\s+(?:сейчас\s+)?изучаешь|что\s+ты\s+(?:уже\s+)?изучил|сколько\s+(?:ты\s+)?(?:уже\s+)?изучил)[\s?.!]*$",
    re.I,
)
_COURSE_PAUSE_RE = re.compile(
    _PRE + r"(?:останови|приостанови|поставь\s+на\s+паузу)\s+(?:курс|обучение)|пауза\s+(?:курса|обучения)[\s?.!]*$", re.I,
)


_APP_STUDY_ALL_RE = re.compile(
    _PRE + r"(?:изучи|изучай|освой|научись\s+пользоваться)\s+(?:все\s+|всеми\s+|всё\s+)?(?:программы|программами|приложения|приложениями|"
    r"все\s+программы|что\s+(?:ты\s+)?видишь|всем,?\s+что\s+(?:ты\s+)?видишь|компьютером)[\s!.?]*$", re.I,
)
_APP_STUDY_RE = re.compile(
    _PRE + r"(?:изучи|изучай|освой|научись\s+пользоваться|научись\s+работать\s+(?:в|с))\s+(?:программой\s+|программу\s+|приложением\s+|приложение\s+)?"
    r"(?P<n>[\w .\-+]{2,40}?)[\s!.?]*$", re.I,
)
_APP_INSPECT_RE = re.compile(
    _PRE + r"(?:что\s+есть\s+в|покажи\s+(?:кнопки|элементы|меню)\s+(?:в\s+)?|какие\s+(?:кнопки|элементы)\s+(?:есть\s+)?в)\s*(?:программе\s+)?(?P<p>[\w .\-+]{2,40}?)[\s?.!]*$", re.I,
)
_APP_CLICK_RE = re.compile(
    _PRE + r"(?:нажми|кликни|клацни)\s+(?:на\s+|по\s+)?(?:кнопку\s+)?(?P<e>[^,]{1,60}?)\s+(?:в|во|на)\s+(?P<p>[\w .\-+]{2,40}?)[\s?.!]*$", re.I,
)
_APP_TYPE_RE = re.compile(
    _PRE + r"(?:введи|напиши|набери|впиши)\s+(?P<t>.{1,200}?)\s+(?:в|во)\s+(?P<p>[\w .\-+]{2,40}?)[\s?.!]*$", re.I,
)
_APP_HOTKEY_RE = re.compile(
    _PRE + r"нажми\s+(?P<k>(?:ctrl|alt|shift|win|control|f\d{1,2})(?:\s*\+\s*[\w+\-,./]+)+)(?:\s+(?:в|во)\s+(?P<p>[\w .\-+]{2,40}?))?[\s?.!]*$", re.I,
)
_NOT_PROGRAM = re.compile(r"^(?:файл\w*|курс\w*|материал\w*|документ\w*|навык\w*|знани\w+|папк\w+|сайт\w*)(?:\s|$)", re.I)


def _match_program_use(original: str) -> LocalActionPlan | None:
    """Studying programs and operating any program through its interface (fork addition)."""

    def plan(tool: str, args: dict[str, object]) -> LocalActionPlan:
        return LocalActionPlan(mode=LocalActionMode.DIRECT, tool_calls=(LocalToolCall(name=tool, args=args),))

    if _APP_STUDY_ALL_RE.match(original):
        return plan("study_app", {"action": "study_next"})
    m = _APP_HOTKEY_RE.match(original)
    if m is not None:
        keys = re.sub(r"\s+", "", m.group("k"))
        args: dict[str, object] = {"action": "hotkey", "keys": keys}
        if m.group("p"):
            args["program"] = _window_name(m.group("p"))
        return plan("app_control", args)
    m = _APP_INSPECT_RE.match(original)
    if m is not None:
        return plan("app_control", {"action": "inspect", "program": _window_name(m.group("p"))})
    m = _APP_TYPE_RE.match(original)
    if m is not None:
        return plan("app_control", {"action": "type", "program": _window_name(m.group("p")), "text": m.group("t").strip(" \"'")})
    m = _APP_CLICK_RE.match(original)
    if m is not None and not _NOT_PROGRAM.match(m.group("e")):
        return plan("app_control", {"action": "click", "program": _window_name(m.group("p")), "element": m.group("e").strip(" \"'")})
    m = _APP_STUDY_RE.match(original)
    if m is not None and not _NOT_PROGRAM.match(m.group("n")) and not re.match(
            r"^(?:computer\s+science|компьютер\s+саенс|информатик\w*)$", m.group("n").strip(), re.I):
        return plan("study_app", {"action": "study", "program": _window_name(m.group("n"))})
    return None


def _match_new_skills(original: str) -> LocalActionPlan | None:
    """Browser, named windows, layouts, memory and scenes (fork addition 2026-11-14)."""

    def plan(tool: str, args: dict[str, object]) -> LocalActionPlan:
        return LocalActionPlan(mode=LocalActionMode.DIRECT, tool_calls=(LocalToolCall(name=tool, args=args),))

    m = _WINDOWS_COURSE_RE.match(original)
    if m is not None:
        return plan("study_course", {"action": "study", "course": "windows"})
    if _COURSE_RUN_RE.match(original):
        return plan("study_course", {"action": "study"})
    if _INBOX_STUDY_RE.match(original):
        return plan("study_course", {"action": "ingest"})
    m = _SKILL_CREATE_RE.match(original)
    if m is not None:
        topic = (m.group("t") or m.group("t2") or "").strip(" \"'")
        return plan("skill_from_knowledge", {"action": "create", "topic": topic} if topic else {"action": "auto"})
    m = _SKILL_ACTIVATE_RE.match(original)
    if m is not None:
        return plan("skill_from_knowledge", {"action": "activate", "name": m.group("n").strip(" \"'")})
    m = _FILE_STUDY_RE.match(original)
    if m is not None and not re.match(r"^(?:computer\s+science|компьютер\s+саенс|информатик\w*)$", m.group("n").strip(), re.I):
        return plan("study_course", {"action": "study", "path": m.group("n").strip(" \"'")})
    if _COURSE_STATUS_RE.match(original):
        return plan("study_course", {"action": "status"})
    if _COURSE_PAUSE_RE.match(original):
        return plan("study_course", {"action": "pause"})
    m = _MEM_HISTORY_RE.match(original)
    if m is not None:
        back = {"вчера": 1, "позавчера": 2}.get((m.group("d") or "").lower(), 0)
        return plan("memory_notes", {"action": "history", "days_back": back})
    m = _MEM_RECALL_RE.match(original)
    if m is not None:
        return plan("memory_notes", {"action": "recall", "query": m.group("q")})
    m = _WIN_LAYOUT_SAVE_RE.match(original)
    if m is not None:
        return plan("window_control", {"action": "save_layout", "name": (m.group("n") or "основная").strip() or "основная"})
    m = _WIN_LAYOUT_LOAD_RE.match(original)
    if m is not None:
        return plan("window_control", {"action": "load_layout", "name": (m.group("n") or "основная").strip() or "основная"})
    m = _MEM_REMEMBER_RE.match(original)
    if m is not None:
        return plan("memory_notes", {"action": "remember", "text": m.group("t").strip()})
    m = _SCENE_RE.match(original)
    if m is not None:
        from jarvis.plugins.tool import scenes  # noqa: PLC0415

        spoken = (m.group("a") or m.group("b") or "").strip()
        if scenes.resolve(spoken) is not None:
            return plan("scenes", {"action": "run", "name": spoken})
    for pattern, action in _BR_TAB_RE:
        if pattern.match(original):
            return plan("browser_control", {"action": action})
    m = _BR_SEARCH_RE.match(original)
    if m is not None:
        engine = (m.group("e") or "").lower()
        engine = "youtube" if engine.startswith(("ютуб", "ютюб", "youtube")) else "google" if engine in {"гугле", "google"} else "yandex"
        if m.group("q2"):
            engine = "google"
        query = (m.group("q") or m.group("q2") or m.group("q3") or "").strip()
        return plan("browser_control", {"action": "search", "query": query, "engine": engine})
    m = _WIN_TILE_RE.match(original)
    if m is not None:
        n = (m.groupdict().get("n") or "2").lower()
        return plan("window_control", {"action": "tile", "count": _WORD_NUM.get(n, int(n) if n.isdigit() else 2)})
    m = _WIN_NAMED_RE.match(original)
    if m is not None:
        name = _window_name(m.group("w"))
        if name and not _WIN_NAMED_SKIP.match(name):
            verb = m.group("v").lower()
            action = "minimize" if verb == "сверни" else "maximize" if verb == "разверни" else "focus"
            return plan("window_control", {"action": action, "window": name})
    m = _BR_OPEN_SITE_RE.match(original)
    if m is not None:
        from jarvis.plugins.tool import browser_control  # noqa: PLC0415

        site = m.group("s").strip()
        if browser_control.site_url(site) is not None and "." in site or site.lower().replace("ё", "е") in browser_control.SITES:
            return plan("browser_control", {"action": "open_url", "site": site})
    return None


_SKILL_CREATE_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис[\s,]+)?(?:пожалуйста\s+)?(?:создай|сделай|оформи|собери|напиши)\s+"
    r"(?:(?:себе|мне)\s+)?(?:навык|навыки|скилл|скиллы)\s+(?:из|на\s+основе|по)\s+"
    r"(?:(?:изученн\w+|знани\w+|курс\w*|файл\w*|материал\w*|выученн\w+)\s*)(?:(?:про|по|о|об|обо)\s+)?(?P<t>.*?)[\s!.?]*$"
    r"|^(?:(?:hey|хей|эй)\s+)?(?:джарвис[\s,]+)?(?:оформи|преврати)\s+(?:знания|изученное|курс)\s+(?:(?:про|по|о|об)\s+)?(?P<t2>.*?)\s+в\s+(?:навык|скилл)[\s!.?]*$",
    re.I,
)
_SKILL_ACTIVATE_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис[\s,]+)?(?:пожалуйста\s+)?(?:включи|активируй|запусти|подключи)\s+(?:навык|скилл)\s+(?P<n>.{2,80}?)[\s!.?]*$",
    re.I,
)
_FILE_STUDY_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис[\s,]+)?(?:пожалуйста\s+)?(?:изучи|изучай|выучи|прочитай|освой|разбери|загрузи)\s+"
    r"(?:мой\s+|мои\s+)?(?:файл|файлы|курс|курсы|материал|материалы|документ|документы)\s+(?P<n>.{2,120}?)[\s!.?]*$", re.I,
)
_INBOX_STUDY_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис[\s,]+)?(?:пожалуйста\s+)?(?:изучи|изучай|загрузи|прочитай)\s+"
    r"(?:(?:всё|все)\s+)?(?:(?:новые|мои|загруженные)\s+)?(?:(?:папку|файлы|курсы|материалы)(?:\s+(?:с\s+курсами|курсов|загрузок))?|что\s+я\s+(?:загрузил|положил))[\s!.?]*$",
    re.I,
)
_WINDOWS_WORDS = (
    r"windows|виндовс\w*|винд\w+|powershell|пауэршелл\w*|павершелл\w*|cmd|командн\w+\s+строк\w+|командн\w+\s+строк\w+\s+и\s+powershell"
)
_WINDOWS_COURSE_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис[\s,]+)?(?:пожалуйста\s+)?(?:начни|начинай|запусти|продолжи|продолжай|возобнови|давай|изучи|изучай|освой|научись)\s+"
    r"(?:(?:курс|обучение|учёбу|учебу)\s+(?:по\s+)?|пользоваться\s+|работать\s+(?:в|с|на)\s+|администрировать\s+)?"
    r"(?:" + _WINDOWS_WORDS + r")(?:\s+и\s+(?:" + _WINDOWS_WORDS + r"))?[\s!.?]*$", re.I,
)
_COURSE_RUN_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис[\s,]+)?(?:пожалуйста\s+)?(?:начни|начинай|запусти|продолжи|продолжай|возобнови|давай|изучи|изучай)\s+"
    r"(?:следующий\s+блок|следующий\s+урок|(?:курс|обучение)(?:\s+(?:по\s+)?(?:computer\s+science|компьютер\s+саенс|информатике|информатика|computer-science))?|"
    r"computer\s+science|компьютер\s+саенс)[\s!.?]*$", re.I,
)
_STOP_STUDY_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис[\s,]+)?(?:стоп|стой|остановись|хватит|прекрати|довольно|достаточно|"
    r"останови(?:\s+(?:обучение|изучение|курс|учёбу|учебу))?|стоп\s+(?:обучение|изучение|курс))[\s!.?]*$", re.I,
)
_CONTINUE_WORD_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис[\s,]+)?(?:давай\s+)?(?:продолжай|продолжи|продолжить|да|ага|"
    r"погнали|поехали)[\s!.?]*$", re.I,
)
_OPEN_ANY_RE = re.compile(
    r"^(?:(?:hey|хей|эй)\s+)?(?:джарвис|jarvis)?[\s,]*(?:пожалуйста\s+)?(?:открой|открыть|запусти|запустить|стартани|включи|запускай|вруби)\s+"
    r"(?:мне\s+)?(?:программу\s+|приложение\s+|игру\s+)?(?P<app>[^,.!?]{2,40}?)(?:\s+пожалуйста)?[\s?.!]*$",
    re.I,
)
_OPEN_ANY_STOP = re.compile(r"\b(?:и|а|потом|затем|после|then|and|в\s+котором|там|нём|нем|ней|файл|папк\w*|сайт|ссылк\w*|страниц\w*)\b", re.I)


def _match_open_from_catalog(original: str) -> LocalActionPlan | None:
    """"открой <программа>" for ANY program in the PC catalog (fork addition)."""
    m = _OPEN_ANY_RE.match(original)
    if m is None:
        return None
    name = m.group("app").strip()
    if not name or _OPEN_ANY_STOP.search(name):
        return None
    from jarvis.plugins.tool import app_catalog  # noqa: PLC0415

    if app_catalog.lookup(name) is None:
        canonical = app_catalog.spoken_to_canonical(name)
        if canonical == name:
            return None
    return LocalActionPlan(
        mode=LocalActionMode.DIRECT,
        tool_calls=(LocalToolCall(name="open_app", args={"app_name": name}),),
    )


# Fork addition: the wake word (and its common mishearings) can sit anywhere in
# an utterance - the pipeline sometimes glues the previous, unanswered phrase in
# front ("включи музыку джарвис следующий трек"). The LAST command wins.
_WAKE_TOKEN_RE = re.compile(
    r"(?<![\w])(?:джа?р[ае]?в[иы]?[сзш]\w*|джар[еи]с|жар[ао]?в[иы]?[сз]|жарвис|дарвис|чарвис|ярвис|джервис|джарви|jarvis|jarvys|hey\s+jarvis)(?![\w])",
    re.I,
)


def _drop_wake_tokens(text: str) -> str:
    """Remove wake-word tokens; keep only what follows the last one if non-empty."""
    parts = _WAKE_TOKEN_RE.split(text)
    if len(parts) == 1:
        return text
    tail = parts[-1].strip(" ,.!?;:-")
    if tail:
        return tail
    head = " ".join(p.strip(" ,.!?;:-") for p in parts[:-1] if p.strip(" ,.!?;:-"))
    return head




# ---------------------------------------------------------------------------
# Fork addition: a stream of tasks in one utterance
#
# "открой стим, открой проводник, включи музыку, включи радио по этому треку, поставь громкость 60%"
# The recogniser writes lower-case text without punctuation, so the boundaries are the command verbs
# themselves ("... стим открой проводник включи ..."). The utterance is only treated as a queue when EVERY
# segment is a command the gate already understands; anything else goes the normal way unchanged.
# ---------------------------------------------------------------------------
_CMD_VERBS = frozenset({
    "открой", "открыть", "запусти", "запустить", "включи", "выключи", "выруби", "вруби", "закрой", "закрыть",
    "поставь", "сделай", "найди", "покажи", "переключи", "останови", "перенеси", "перемести", "сверни", "разверни",
    "нажми", "введи", "напиши", "набери", "скачай", "установи", "загрузи", "подключись", "отключись", "подключи",
    "отключи", "активируй", "сохрани", "создай", "смени", "измени", "добавь", "убери", "увеличь", "уменьши",
    "прибавь", "убавь", "пропусти", "вернись", "перейди", "переключись", "изучи", "продолжи", "запомни",
    "прижми", "расположи", "заблокируй", "перезагрузи", "возобнови", "приостанови", "помоги", "проверь",
    "посчитай", "напомни", "засеки", "поищи", "загугли", "дай", "верни",
})
_OPEN_LIKE = frozenset({"открой", "запусти", "закрой", "включи", "выключи", "сверни", "разверни", "переключись"})
_SEP_WORDS = frozenset({"и", "а", "потом", "затем", "дальше", "далее", "следом", "также", "ещё", "еще", "теперь", "сначала",
                        "после", "этого", "же", "да", "ну", "пожалуйста", "тоже"})
_SPLIT_RE = re.compile(r"\s*(?:,|;|\bа\s+потом\b|\bпосле\s+этого\b|\bпотом\b|\bзатем\b|\bследом\b|\bа\s+также\b)\s*", re.I)
_AND_LIST_RE = re.compile(r"^(?P<v>открой|запусти|закрой|включи|выключи)\s+(?P<a>[^,]+?)\s+и\s+(?P<b>[^,]+)$", re.I)
_MAX_STEPS = 12


def _trim_separators(words: list[str]) -> list[str]:
    while words and words[0].lower() in _SEP_WORDS:
        words = words[1:]
    while words and words[-1].lower() in _SEP_WORDS:
        words = words[:-1]
    return words


def split_into_commands(text: str) -> list[str]:
    """Cut an utterance into command segments at punctuation, "потом/затем" and at every command verb."""
    segments: list[str] = []
    last_verb = ""
    for part in _SPLIT_RE.split(text.strip()):
        words = part.split()
        if not words:
            continue
        bounds = [0] + [i for i, w in enumerate(words) if i > 0 and w.lower().strip(".,!?") in _CMD_VERBS]
        bounds.append(len(words))
        for start, end in zip(bounds, bounds[1:], strict=False):
            chunk = _trim_separators(words[start:end])
            if not chunk:
                continue
            first = chunk[0].lower().strip(".,!?")
            if first in _CMD_VERBS:
                last_verb = first if first in _OPEN_LIKE else ""
            elif last_verb and len(chunk) <= 4:
                chunk = [last_verb, *chunk]  # "открой стим, проводник, блокнот": the verb carries over
            segments.append(" ".join(chunk))
    expanded: list[str] = []
    for seg in segments:
        m = _AND_LIST_RE.match(seg)
        if m is not None:
            expanded += [f"{m.group('v')} {m.group('a')}", f"{m.group('v')} {m.group('b')}"]
        else:
            expanded.append(seg)
    return expanded


def match_local_action(
    text: str,
    lang: Literal["de", "en"] = "de",
    *,
    _registry: _CapabilityRegistryLike | None = _SENTINEL,  # type: ignore[assignment]
    live_tool_names: Sequence[str] = (),
) -> LocalActionPlan | None:
    """Plan for one command, or for a queue of commands spoken in one breath (see split_into_commands)."""
    cleaned = _drop_wake_tokens(text.strip())
    segments = split_into_commands(cleaned) if cleaned else []
    if 2 <= len(segments) <= _MAX_STEPS:
        plans = [_match_single(seg, lang, _registry=_registry, live_tool_names=live_tool_names) for seg in segments]
        if all(p is not None and p.mode == LocalActionMode.DIRECT and p.tool_calls for p in plans):
            calls = tuple(call for p in plans for call in p.tool_calls)  # type: ignore[union-attr]
            return LocalActionPlan(mode=LocalActionMode.DIRECT, tool_calls=calls, compound=True, steps=tuple(segments))
    return _match_single(text, lang, _registry=_registry, live_tool_names=live_tool_names)


def _match_single(
    text: str,
    lang: Literal["de", "en"] = "de",
    *,
    _registry: _CapabilityRegistryLike | None = _SENTINEL,  # type: ignore[assignment]
    live_tool_names: Sequence[str] = (),
) -> LocalActionPlan | None:
    """Return a deterministic local action plan for narrow local commands.

    Parameters
    ----------
    text:
        Raw utterance from the voice pipeline.
    lang:
        Language hint (``"de"`` or ``"en"``) used to select the
        ``UNSUPPORTED`` response copy.
    _registry:
        Capability registry override — **for tests only**.  Pass a fake
        registry to exercise the UNSUPPORTED path without importing the real
        ``jarvis.core.capabilities`` module.  When omitted the production
        singleton is loaded via a late import.
    live_tool_names:
        Names of the tools attached to the CURRENT turn, read live by the
        caller.  They veto the UNSUPPORTED refusal: a tool named after the
        requested system is proof the capability exists, whatever the registry
        believes (GT-16).  Empty means "caller knows of no tools" and leaves
        the historical registry-only behaviour untouched.
    """
    original = _drop_wake_tokens(text.strip())
    if not original:
        return None
    if _STOP_STUDY_RE.match(original):
        from jarvis.plugins.tool import study_runner  # noqa: PLC0415

        if study_runner.is_running():
            return LocalActionPlan(
                mode=LocalActionMode.DIRECT,
                tool_calls=(LocalToolCall(name="study_course", args={"action": "stop"}),),
            )
    if _CONTINUE_WORD_RE.match(original):
        from jarvis.plugins.tool import study_course  # noqa: PLC0415

        target = study_course.awaiting_target()
        if target is not None:
            # a study asked "continue?": this answer continues it (not Spotify)
            return LocalActionPlan(
                mode=LocalActionMode.DIRECT,
                tool_calls=(LocalToolCall(name=target[0], args=dict(target[1])),),
            )

    normalized = _normalize(original)

    # ------------------------------------------------------------------
    # Capability gate (SPEC §Layer 2, insertion point a)
    #
    # Resolve the registry once per call.  ``_SENTINEL`` (not None) means
    # "use the real singleton"; ``None`` means "registry unavailable, skip
    # gate"; a passed object means "test fake, use directly".
    # ------------------------------------------------------------------
    if _registry is _SENTINEL:
        _registry = _get_capability_registry()

    # Defensive: an empty registry (e.g. unit tests that don't seed it) means
    # "boot has not populated capabilities yet" → skip the gate to avoid false
    # positives.  The production boot path always calls
    # ``capabilities_seed.seed_registry(get_registry())`` before any voice
    # turn, so this no-op only triggers in isolated unit tests.
    if _registry is not None and getattr(_registry, "all", lambda: ())():
        if (
            _registry.has_action_intent(normalized)
            and _registry.resolve_intent(normalized) is None
            # Desktop-control commands are NEVER "unsupported" — computer-use is
            # the universal GUI integration, so route them there instead of the
            # canned refusal (live bug 2026-05-25: "oeffne WhatsApp und schreib"
            # was refused with "das kann ich noch nicht").
            and not _looks_like_desktop_control(normalized)
            # 2026-06-01: only a SPECIFIC external integration (mail/calendar/
            # Spotify/social/delivery) is genuinely unsupported. Generic work
            # (analyse/build/fix/code/research/git) is sub-agent-fulfillable, so
            # it must NOT be refused here — it falls through to the force-spawn
            # path (the sub-agent is the universal capability for generic work).
            and requires_external_integration(normalized)
            # GT-16: the registry is a cache, the attached tools are the truth.
            # A plugin/CLI/MCP server connected mid-session is callable long
            # before ``resolve_intent`` learns about it — refusing then is the
            # live complaint ("I'm told it's impossible while the capability is
            # right there"). A tool named after the requested system stands the
            # refusal down; the turn proceeds and the tool answers for itself.
            and not _live_surface_covers(
                external_integration_terms(normalized), live_tool_names
            )
        ):
            return LocalActionPlan(
                mode=LocalActionMode.UNSUPPORTED,
                response_text=_unsupported_response(normalized, lang),
            )
    # ADR-0016 L2: check the orb-recovery patterns BEFORE the scripted /
    # direct-open-app / visual-target gates so a "wo bist du?" never
    # accidentally falls through to a different branch.
    if _matches_orb_reset(normalized):
        return LocalActionPlan(
            mode=LocalActionMode.DIRECT,
            tool_calls=(LocalToolCall(name="reset_orb_position", args={}),),
        )
    list_apps_plan = _match_list_apps(normalized)
    if list_apps_plan is not None:
        return list_apps_plan
    system_plan = _match_system_control(normalized)
    if system_plan is not None:
        return system_plan
    pc_plan = _match_pc_actions(normalized)
    if pc_plan is not None:
        return pc_plan
    extras_plan = _match_extras(normalized, original)
    if extras_plan is not None:
        return extras_plan
    apps_plan = _match_apps(normalized, original)
    if apps_plan is not None:
        return apps_plan
    window_plan = _match_window_control(original)
    if window_plan is not None:
        return window_plan
    skills_plan = _match_new_skills(original)
    if skills_plan is not None:
        return skills_plan
    use_plan = _match_program_use(original)
    if use_plan is not None:
        return use_plan
    catalog_plan = _match_open_from_catalog(original)
    if catalog_plan is not None:
        return catalog_plan
    scripted = _match_scripted_local_plan(normalized)
    if scripted is not None:
        return scripted

    direct = _match_direct_open_app(normalized)
    if direct is not None:
        return direct

    # Browser+URL goals are a single argv launch — checked BEFORE the
    # visual-target / desktop-control branches, which would otherwise send
    # them into the multi-minute vision loop (2026-06-10 latency collapse).
    browser_url = _match_browser_url_fast_path(normalized)
    if browser_url is not None:
        return browser_url

    if _matches_visual_target(normalized):
        return LocalActionPlan(
            mode=LocalActionMode.COMPUTER_USE,
            harness=HARNESS_NAME,
            prompt=original,
        )

    # Compound open-and-operate / GUI-manipulation commands → multi-step
    # computer-use loop. Checked AFTER _match_direct_open_app so a plain
    # "oeffne chrome" stays the fast DIRECT path; only commands that actually
    # need to drive the UI ("oeffne WhatsApp und schreib Mama hallo",
    # "scroll runter", "tippe ... und druecke enter") land here.
    if _looks_like_desktop_control(normalized):
        return LocalActionPlan(
            mode=LocalActionMode.COMPUTER_USE,
            harness=HARNESS_NAME,
            prompt=original,
        )

    # Robust open-app fallback (live bug 2026-06-09 17:36): an open-app intent in
    # ANY phrasing the strict verb-first gates above missed — verb-at-end +
    # filler-heavy ("Kannst du bitte für mich einmal mein Spotify öffnen?",
    # "Spotify aufmachen", "öffnest du mir mal Discord"). That utterance matched
    # no DIRECT pattern, fell to the router LLM, which produced no speech (it did
    # not even call computer_use) → "Wie meinst du das genau?" and Spotify NEVER
    # opened. Execute deterministically instead, cross-platform (open_app →
    # app_resolver branches per OS), with no LLM in the loop:
    #   • "…öffnen UND <do more>"  → multi-step → computer-use offload (same path
    #     as the verb-first compounds above) so the follow-up isn't dropped.
    #   • known app, single step    → instant DIRECT open_app.
    #   • app NOT in the known set  → fall through to the brain (unchanged): the
    #     screenshot loop would have to hunt an unknown name on screen, the path
    #     that stalled live 2026-06-08. The force-spawn guard (is_open_app_intent)
    #     still keeps such a turn off the sub-agent path.
    if is_open_app_intent(original) and not _OPEN_NEGATION_RE.search(normalized):
        if _AND_RE.search(normalized):
            # "...öffnen und <do more>" → multi-step. (A benign "und zwar …" also
            # lands here; acceptable — the CU loop still opens the app and handles
            # the qualifier, at the cost of one screenshot cycle.)
            return LocalActionPlan(
                mode=LocalActionMode.COMPUTER_USE,
                harness=HARNESS_NAME,
                prompt=original,
            )
        # "<target> in/im <app>" — the app is a CONTAINER the user navigates INTO
        # ("öffne meine Bilder im Explorer", "in meinen Explorer meine Bilder
        # öffnen"). A bare open_app(<app>) would launch an empty window and drop
        # the real target, so the whole goal goes to the multi-step computer-use
        # loop instead (live bug 2026-06-26). Checked BEFORE the bare-app extract
        # so the dropped-target case never reaches a DIRECT launch.
        if _app_used_as_container(normalized):
            return LocalActionPlan(
                mode=LocalActionMode.COMPUTER_USE,
                harness=HARNESS_NAME,
                prompt=original,
            )
        # Gate already confirmed by is_open_app_intent (+ negation excluded) above,
        # so a bare app mention in a non-open sentence can never reach here.
        app = _extract_known_app(normalized)
        if app is not None:
            return LocalActionPlan(
                mode=LocalActionMode.DIRECT,
                tool_calls=(LocalToolCall(name="open_app", args={"app_name": app}),),
            )

    return None


def _match_scripted_local_plan(text: str) -> LocalActionPlan | None:
    terminal_count = _parse_terminal_count(text)

    if terminal_count is not None:
        return LocalActionPlan(
            mode=LocalActionMode.DIRECT,
            tool_calls=_open_terminal_calls(terminal_count),
        )

    return None


def _extract_known_app(text: str) -> str | None:
    """Return the canonical app for the FIRST known app name in a normalised
    open-app utterance, else ``None``.

    Scans 2-word then 1-word windows so a multi-word alias ("microsoft teams",
    "windows terminal", "whats app", "google chrome") wins over a 1-word
    sub-match. This catches the verb-at-end and filler-heavy phrasings the strict
    verb-first ``_DIRECT_PATTERNS`` miss ("Kannst du bitte für mich einmal mein
    Spotify öffnen?", "Spotify aufmachen", "öffnest du mir mal Discord"). Callers
    MUST first confirm :func:`is_open_app_intent` so a bare app mention in a
    non-open sentence ("ich höre Spotify gern") never launches anything.
    """
    tokens = [t for t in text.split() if t]
    for window in (2, 1):
        for i in range(len(tokens) - window + 1):
            phrase = " ".join(tokens[i:i + window])
            app = _APP_ALIASES.get(phrase)
            if app is not None:
                return app
    return None


def _match_direct_open_app(text: str) -> LocalActionPlan | None:
    for pattern in _DIRECT_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        app = _canonical_app(match.group("app"))
        if app is None:
            return None
        return LocalActionPlan(
            mode=LocalActionMode.DIRECT,
            tool_calls=(LocalToolCall(name="open_app", args={"app_name": app}),),
        )
    return None


def _matches_visual_target(text: str) -> bool:
    # Same TASK-or-QUESTION rule as _looks_like_desktop_control: the leading
    # "klick" pattern also matches a narrated "Klick ich da jetzt drauf oder
    # nicht?", which is a question about the screen, not an order to touch it.
    if _is_information_question(text):
        return False
    return any(pattern.match(text) for pattern in _VISUAL_TARGET_PATTERNS)


def _canonical_app(raw: str) -> str | None:
    app = re.sub(r"\s+", " ", raw.strip(" \t\r\n?.!")).lower()
    app = re.sub(r"\s+app$", "", app).strip()
    tokens = [t for t in app.split(" ") if t]
    # Drop leading filler/articles: "mir chrome" -> "chrome", "den editor" ->
    # "editor", "fuer mich chrome" -> "chrome". Keep at least one token.
    while len(tokens) > 1 and tokens[0] in _APP_FILLER_WORDS:
        tokens.pop(0)
    # Drop trailing politeness/articles too: "explorer fuer mich" -> "explorer",
    # "notepad fuer mich bitte" -> "notepad". Without this the trailing "fuer
    # mich" left a known app unresolved and the command fell to the computer-use
    # vision loop instead of the clean DIRECT launch (live 2026-06-08).
    while len(tokens) > 1 and tokens[-1] in _APP_FILLER_WORDS:
        tokens.pop()
    cleaned = " ".join(tokens)
    direct = _APP_ALIASES.get(cleaned)
    if direct is not None:
        return direct
    # Trailing qualifier: "chrome browser" -> "chrome".
    if len(tokens) > 1 and tokens[-1] == "browser":
        return _APP_ALIASES.get(" ".join(tokens[:-1]))
    return None


def _parse_terminal_count(text: str) -> int | None:
    patterns = (
        r"^(?:hey\s+)?(?:jarvis[,\s]+)?(?:oeffne|starte)\s+(?P<count>\d+|[a-z]+)\s+terminals?\b",
        r"^(?:hey\s+)?(?:jarvis[,\s]+)?mach\s+(?P<count>\d+|[a-z]+)\s+terminals?\s+auf\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        count = _parse_count(match.group("count"))
        if count is None:
            return None
        return max(1, min(count, _MAX_SCRIPTED_TERMINALS))
    return None


def _parse_count(raw: str) -> int | None:
    if raw.isdigit():
        return int(raw)
    return _GERMAN_NUMBER_WORDS.get(raw)


def _open_terminal_calls(count: int) -> tuple[LocalToolCall, ...]:
    return tuple(
        LocalToolCall(name="open_app", args={"app_name": "wt"}) for _ in range(count)
    )


def _normalize(text: str) -> str:
    return (
        text.strip()
        .lower()
        .replace("\u00f6", "oe")
        .replace("\u00d6", "oe")
        .replace("\u00e4", "ae")
        .replace("\u00c4", "ae")
        .replace("\u00fc", "ue")
        .replace("\u00dc", "ue")
        .replace("\u00df", "ss")
    )
