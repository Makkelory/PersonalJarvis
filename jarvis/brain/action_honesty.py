"""Execution-state backstop for model promises and false completions.

Models occasionally end a turn with "I'll check and get back to you" or
with a finished-looking result ("I'm playing you a playlist") without a
tool call. Jarvis has no autonomous continuation after that response, so
the sentence is not harmless filler: it is an ungrounded claim that work
is running or already done. This module detects both shapes with regex
only and provides a localized honest fallback.

The judgement is made per clause, never over the whole text. An answer that
opens with "Let me check." and signs off with "I'll get back to you when that
changes." matches the promise vocabulary at both ends while the delivered
result sits between them; replacing that text would destroy the answer. A
clause that carries an independent, unconditional statement is therefore
treated as substance: the promise clauses around it are dropped and the
substance is kept. Only a text that is promise wording end to end is replaced.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from typing import NamedTuple


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


# Present-tense / perfect claims that the action is already happening or done
# FOR the user. Distinct from the deferred "I'll look later" vocabulary
# below: these sentences carry a long result-shaped tail ("a playlist of
# 2010s songs") that ``_analyse`` treats as substance, so they never look
# "bare". Live 2026-08-19 17:13 (vertex-live hybrid): the model announced
# a 2010s playlist in German with function_calls=0 — the user had to say
# it again before the tool ran.
_FALSE_COMPLETION_RE = re.compile(
    r"(?:"
    r"\bich\s+(?:spiele?|offne|starte|sende|schicke|speichere|"  # i18n-allow: German output matcher
    r"lege\s+an|setze|mache)\s+"  # i18n-allow: German output matcher
    r"(?:dir|fur\s+dich)\b|"  # i18n-allow: German output matcher
    r"\bich\s+habe\s+(?:dir|fur\s+dich)\s+"  # i18n-allow: DE output
    r"(?:gerade\s+)?.{0,48}"
    r"(?:gespielt|abgespielt|angemacht|angeschaltet|"  # i18n-allow: DE output
    r"eingeschaltet|aufgelegt|rausgesucht|herausgesucht|"  # i18n-allow: DE output
    r"geoffnet|gestartet|gesendet|"  # i18n-allow: DE output
    r"geschickt|gespeichert|erledigt)\b|"  # i18n-allow: DE output
    r"\bi(?:'m|\s+am)\s+(?:playing|opening|starting|sending|saving)\s+"
    r"(?:you\s+)?(?:a|the|your)\b|"
    r"\bi(?:'ll|\s+will)\s+(?:play|open|start|send|save)\s+"
    r"(?:you\s+)?(?:a|the|your)\b|"
    r"\bi(?:'ve|\s+have)\s+(?:just\s+)?(?:played|opened|started|sent|saved)\s+"
    r"(?:you\s+)?(?:a|the|your|it)\b|"
    r"\bplaying\s+you\s+(?:a|the)\b|"
    r"\bte\s+(?:pongo|reproduzco)\s+(?:una?\s+)?"  # i18n-allow: ES output
    r"(?:playlist|cancion|musica)\b|"  # i18n-allow: ES output
    r"\bte\s+(?:envio|abro)\s+(?:el|la|un|una|tu)\b|"  # i18n-allow: ES output
    r"\bestoy\s+(?:reproduciendo|abriendo|iniciando|enviando)\b|"  # i18n-allow
    r"\bhe\s+(?:reproducido|abierto|iniciado|enviado|guardado)\b|"  # i18n-allow
    # World-state completions name no actor at all ("it is playing now"),
    # so none of the first-person alternatives above can see them. Live
    # 2026-08-22 18:22: "Sie laufen jetzt auf YouTube Music" cleared every
    # guard while the turn had made zero function calls.
    r"\b(?:laufen|lauft|spielt|spielen|"  # i18n-allow: DE output
    r"ertont|erklingt)\s+(?:jetzt|gerade|nun|"  # i18n-allow: DE output
    r"bereits|schon)\b|"  # i18n-allow: DE output
    r"\b(?:jetzt|gerade|nun)\s+(?:am\s+)?"  # i18n-allow: DE output
    r"(?:laufen|lauft|spielt|spielen)\b|"  # i18n-allow: DE output
    r"\b(?:is|are)\s+(?:playing|running)\s+(?:now|already)\b|"
    r"\b(?:now|already)\s+(?:playing|running)\b|"
    r"\b(?:ya\s+)?(?:esta|estan)\s+sonando\b|"  # i18n-allow: ES output
    # Russian (fork addition). Matched on NORMALISED text, where "й" has
    # become "и" and "ё" has become "е", so the stems below are written in that
    # folded form. A first-person completion with no tool call is a false claim.
    r"\bя\s+(?:уже\s+|только\s+что\s+|успешно\s+|для\s+вас\s+|для\s+тебя\s+)?"  # i18n-allow: RU
    r"(?:открыл|запустил|вывел|включил|выключил|проверил|нашел|выполнил|"  # i18n-allow: RU
    r"отправил|сохранил|установил|закрыл|сделал)[аи]?\b|"  # i18n-allow: RU
    r"\b(?:проверил|нашел|открыл|запустил|выполнил)[аи]?\s+"  # i18n-allow: RU
    r"(?:погоду|браузер|приложение|программу|страницу|сайт|"  # i18n-allow: RU
    r"информацию|результаты|стим|steam)\b|"  # i18n-allow: RU
    # World-state completions without an actor ("браузер открыт", "оно
    # запустилось и готово"), the Russian twin of the German ones above.
    r"\b(?:браузер|браузера|приложение|программа|окно|страница|сайт|steam|стим)\s+"  # i18n-allow: RU
    r"(?:уже\s+)?(?:открыт|запущен|открыта|запущена|открыто|запущено)[аоы]?\b|"  # i18n-allow: RU
    r"\b(?:оно|она|он)\s+(?:уже\s+)?(?:запустил|открыл)(?:ось|ся|ась)\b|"  # i18n-allow: RU
    r"\b(?:поиск|онлаин-?поиск|интернет)\s+(?:показыва|показал|говор|сообща)\w*"  # i18n-allow: RU
    r")"
)


_ACTION_COMMITMENT_RE = re.compile(
    r"(?:"
    r"\b(?:das|es)\s+kann\s+ich(?:\s+gerne)?"  # i18n-allow: German output matcher
    r"(?:\s+f(?:u|ue)r\s+dich)?\s+"  # i18n-allow: German output matcher
    r"(?:nachschauen|nachsehen|pr(?:u|ue)fen|checken|"  # i18n-allow: German output matcher
    r"lesen|holen|(?:o|oe)ffnen|speichern)|"  # i18n-allow: German output matcher
    r"\bich\s+(?:werde\s+|werd\s+|will\s+|kann\s+)?"  # i18n-allow: German output matcher
    r"(?:schaue|schau|gucke|nachschauen|nachsehen|"  # i18n-allow: German output matcher
    r"pr(?:u|ue)fen|checken|nachforschen|recherchieren|"  # i18n-allow: German output matcher
    r"lesen|holen|(?:o|oe)ffnen|speichern|eintragen|"  # i18n-allow: German output matcher
    r"starten)|"  # i18n-allow: German output matcher
    r"\bich\s+werfe(?:\s+(?:kurz|mal))?\s+"  # i18n-allow: German output matcher
    r"einen?\s+blick|"  # i18n-allow: German output matcher
    r"\b(?:let\s+me|i(?:'ll|\s+will|'m\s+going\s+to|\s+am\s+going\s+to|\s+can))\s+"
    r"(?:look|check|review|read|fetch|open|save|enter|start|research|inspect)|"
    r"\b(?:voy\s+a|dejame)\s+"
    r"(?:mirar|revisar|consultar|leer|buscar|abrir|"  # i18n-allow: Spanish output matcher
    r"guardar|anotar|iniciar)|"  # i18n-allow: Spanish output matcher
    # Russian (fork addition), normalised form (й→и, ё→е).
    r"\b(?:я\s+)?(?:сейчас\s+)?(?:открою|запущу|проверю|посмотрю|поищу|найду|"  # i18n-allow: RU
    r"выполню|включу|отправлю|сохраню)\b|"  # i18n-allow: RU
    r"\b(?:давай|давайте)\s+(?:проверим|посмотрим|откроем|найдем|запустим)\b"  # i18n-allow: RU
    r")"
)

_DEFER_MARKER_RE = re.compile(
    r"(?:"
    r"\b(?:einen?\s+moment|warte(?:\s+kurz)?|"  # i18n-allow: German output matcher
    r"gleich|sp(?:a|ae)ter|danach)\b|"  # i18n-allow: German output matcher
    r"\b(?:sage|melde)\s+(?:ich\s+)?dir\b|"  # i18n-allow: German output matcher
    r"\b(?:one\s+moment|give\s+me\s+(?:a|one)\s+moment|later|shortly)\b|"
    r"\b(?:get|come|report)\s+back\b|"
    r"\b(?:un\s+momento|espera|enseguida|luego|"  # i18n-allow: Spanish output matcher
    r"despues)\b|"  # i18n-allow: Spanish output matcher
    r"\b(?:te\s+digo|te\s+cuento|"  # i18n-allow: Spanish output matcher
    r"vuelvo\s+contigo)\b|"  # i18n-allow: Spanish output matcher
    # Russian (fork addition). "сейчас" is deliberately NOT a marker: it opens
    # real answers too ("Сейчас в Мытищах +4").
    r"\b(?:одну\s+секунду|секундочку|минутку|подождите|подожди|"  # i18n-allow: RU
    r"чуть\s+позже|позже|сообщу|вернусь)\b|"  # i18n-allow: RU
    r"\bскажу\s+(?:вам|тебе)\b"  # i18n-allow: RU
    r")"
)

# Clause boundaries. A period between digits (20.5) and a colon between digits
# (14:30) are not boundaries, and a comma only separates when whitespace
# follows it, so decimal commas (20,5) stay inside one clause. The pattern is
# a capturing group: ``re.split`` keeps every separator, so the clauses
# reassemble into the original string byte for byte.
_CLAUSE_SPLIT_RE = re.compile(
    r"((?:[!?…]|(?<!\d)\.(?!\d))+[\s\"'“”)\]]*"
    r"|[;\n\r]+\s*"
    r"|,\s+"
    r"|(?<!\d):(?!\d)\s*"
    r"|\s+[—–]+\s+)"
)

# ASCII plus Cyrillic: without the Cyrillic range every Russian word vanished
# from the token count, so no Russian clause could ever count as substance.
_TOKEN_RE = re.compile(r"[0-9a-zа-я]+")  # i18n-allow: Cyrillic range

# Closed-class words plus the politeness and hedging filler that a promise
# clause is built from. Everything outside this set counts as content, so
# substance is recognised by structure (an independent clause that carries
# content words) instead of by a handful of fixed result phrasings.
_FUNCTION_WORDS: frozenset[str] = frozenset(
    (
        "der die das den dem des dessen deren ein eine einen einem "  # i18n-allow: matcher
        "einer eines ich du er sie es wir ihr man mich dich sich uns "  # i18n-allow: matcher
        "euch mir dir ihm ihn ihnen mein meine meinen meinem dein "  # i18n-allow: matcher
        "deine deinen deinem sein ihre unser euer und oder aber denn "  # i18n-allow: matcher
        "doch dann noch nur auch schon mal kurz gerne gern eben "  # i18n-allow: matcher
        "schnell sofort bitte danke ja nein nicht kein keine keinen "  # i18n-allow: matcher
        "nichts etwas alles ist sind bin bist seid war waren sei habe "  # i18n-allow: matcher
        "hast hat haben hatte hatten wird werden werde wurde wurden "  # i18n-allow: matcher
        "kann kannst konnen konnte soll sollte muss mussen darf fur "  # i18n-allow: matcher
        "mit vom von zu zum zur in im am an auf aus bei nach uber "  # i18n-allow: matcher
        "unter vor durch ohne um als wie so dass da hier dort jetzt "  # i18n-allow: matcher
        "gleich bescheid sage sagen sag melde melden moment "  # i18n-allow: matcher
        "the a an this that these those i you he she it we they me "
        "him her us them my your his its our their and or but so then "
        "just also only quick quickly right now please thanks ok okay "
        "yes no not sure is are was were be been being am do does did "
        "doing have has had will would shall should can could may "
        "might must to of in on at for with from by about into over "
        "under after before as than here there when while "
        "el la los las un una unos unas lo yo tu ella nosotros ellos "  # i18n-allow: matcher
        "me te se nos les le mi mis tus su sus y o pero entonces ya "  # i18n-allow: matcher
        "solo tambien claro por favor gracias si no nada algo todo es "  # i18n-allow: matcher
        "son era eran ser estar estoy esta estan estaba fue fueron "  # i18n-allow: matcher
        "hay he ha hemos han de en a con para sobre desde hasta como "  # i18n-allow: matcher
        "que aqui alli ahora luego "  # i18n-allow: matcher
        # Russian (fork addition), folded form.
        "я ты он она оно мы вы они мне тебе вам мой твой наш ваш и или но "  # i18n-allow: matcher
        "да нет не ни ли же бы уже еще только также просто это то как "  # i18n-allow: matcher
        "что в на с со к ко по из за от до для у о об при про бы был "  # i18n-allow: matcher
        "была было были есть будет пожалуйста спасибо давай давайте "  # i18n-allow: matcher
        "хорошо ладно ок ну вот тут там сейчас"  # i18n-allow: matcher
    ).split()
)

# A clause introduced by a subordinating conjunction states a condition, not a
# result ("once the report is ready" / "when it changes"), so it never
# counts as delivered substance and it never survives on its own once the
# clause it depends on is dropped.
_SUBORDINATE_OPENER_RE = re.compile(
    r"^(?:(?:und|oder|aber|and|or|but|y|pero)\s+)?"  # i18n-allow: German/Spanish output matcher
    r"(?:"
    r"wenn|sobald|falls|weil|damit|bevor|nachdem|"  # i18n-allow: German output matcher
    r"bis|solange|sofern|obwohl|dass|ob|sowie|"  # i18n-allow: German output matcher
    r"once|if|until|till|because|unless|whether|while|when|"
    r"as\s+soon\s+as|so\s+that|"
    r"cuando|apenas|porque|mientras|aunque|que|"  # i18n-allow: Spanish output matcher
    r"en\s+cuanto|para\s+que|"  # i18n-allow: Spanish output matcher
    r"когда|как\s+только|если|пока|потому\s+что|чтобы|хотя"  # i18n-allow: RU matcher
    r")\b"
)

# Two clause shapes that look like delivered content to the structural
# substance test but can never BE delivered content. They are discounted only
# in the false-completion path (``has_false_completion_claim``), never in
# ``_analyse`` — a real answer must keep every chance to survive.
#
# A relative continuation elaborates the claim it hangs off ("… songs, which
# you can focus to"); it has no meaning once that claim is gone. Only the
# unambiguous openers are listed: in German a bare definite article and a
# relative pronoun are the same word, so treating one as a dependent tail
# would discard a real result that merely opens with an article.
_DEPENDENT_CONTINUATION_RE = re.compile(
    r"^(?:(?:und|oder|aber|and|or|but|y|pero)\s+)?"  # i18n-allow: DE/ES matcher
    r"(?:"
    r"(?:bei|mit|auf|in|zu|von|fur|durch|uber)\s+"  # i18n-allow: German output matcher
    r"(?:denen|dem|der|die|das|welchen|welcher|welche)\b|"  # i18n-allow: DE
    r"wo(?:bei|mit|rauf|ran|durch|von)?\b|"  # i18n-allow: German output matcher
    r"so\s+dass\b|"  # i18n-allow: German output matcher
    r"(?:which|where|whom)\b|"
    r"(?:so|that)\s+you\s+can\b|"
    r"(?:con|en|para|sobre)\s+(?:los|las|el|la)\s+que\b|"  # i18n-allow: ES
    r"donde\b|"  # i18n-allow: Spanish output matcher
    r"(?:которои|которая|которое|которые|где)\b|"  # i18n-allow: RU matcher (й folded to и)
    # "Теперь оно активно и готово к работе" elaborates a claimed launch.
    r"(?:теперь\s+)?(?:оно|она|он|они)\s+(?:уже\s+)?"  # i18n-allow: RU matcher
    r"(?:активн|готов|работа|запущен|открыт|доступн)\w*\b"  # i18n-allow: RU matcher
    r")"
)

# Russian claims of having LOOKED something up. With no tool call behind them
# the figures that follow ("Сейчас там +25") are invented, so, unlike the
# generic completion claims, the trailing clauses are not treated as an
# independent delivered answer. Callers only apply the guard when the turn
# executed no tool at all. Normalised form (й→и, ё→е).
_RU_LOOKUP_CLAIM_RE = re.compile(
    r"(?:"
    r"\bя\s+(?:уже\s+|только\s+что\s+)?(?:проверил|нашел)[аи]?\b|"  # i18n-allow: RU
    r"\b(?:проверил|нашел)[аи]?\s+(?:погоду|информацию|результаты|"  # i18n-allow: RU
    r"в\s+интернете)\b|"  # i18n-allow: RU
    r"\b(?:поиск|онлаин-?поиск|интернет)\s+(?:показыва|показал|говор|сообща)\w*"  # i18n-allow: RU
    r")"
)

# A send-off carries goodwill, not a result. Without this, a two-word
# "enjoy!" clears the content bar and rescues the false completion in
# front of it.
_COURTESY_RE = re.compile(
    r"(?:"
    r"\bviel\s+(?:spass|vergnugen|freude|erfolg)\b|"  # i18n-allow: DE output
    r"\bich\s+hoffe\b|\bhoffentlich\b|"  # i18n-allow: German output matcher
    r"\bes\s+gefallt\s+dir\b|"  # i18n-allow: German output matcher
    r"\b(?:lass\s+es\s+dir\s+gefallen|gute\s+unterhaltung)\b|"  # i18n-allow: DE
    r"\b(?:enjoy|have\s+fun)\b|"
    r"\bi\s+hope\b|\bhope\s+you\b|"
    r"\b(?:que\s+lo\s+disfrutes|espero\s+que|"  # i18n-allow: Spanish output matcher
    r"buena\s+escucha)\b|"  # i18n-allow: Spanish output matcher
    r"\b(?:приятного\s+(?:просмотра|прослушивания|использования)|надеюсь)\b"  # i18n-allow: RU
    r")"
)


_TRAILING_CONJUNCTION_RE = re.compile(
    r"\s+(?:und|oder|aber|and|or|but|y|pero)$"  # i18n-allow: German/Spanish output matcher
)

# Two content words are enough for a delivered result ("Es sind 20 Grad"), and
# few enough that a promise clause never reaches the bar once its own promise
# wording is discounted.
_MIN_SUBSTANCE_TOKENS = 2

# Length of the tail a bare commitment may carry before it stops looking like
# a promise. Kept from the original guard so the acknowledgement path (which
# shares this predicate) keeps its established suppression boundary.
_MAX_BARE_COMMITMENT_TAIL = 24

_ACTION_NOT_STARTED_PHRASES: dict[str, str] = {
    "de": (
        "Ich habe dafür gerade keine Aktion gestartet "  # i18n-allow: runtime voice phrase
        "und deshalb noch kein Ergebnis. "  # i18n-allow: runtime voice phrase
        "Bitte sag es noch einmal."  # i18n-allow: runtime voice phrase
    ),  # i18n-allow: German runtime voice/chat output
    "en": (
        "I did not start an action for that, so I do not have a result yet. Please ask me again."
    ),
    "es": (
        "No inicié ninguna acción para eso, así que todavía no tengo un "
        "resultado. Pídemelo de nuevo."
    ),  # i18n-allow: Spanish runtime voice/chat output
    "ru": (
        "Я не запускал для этого никакого действия, поэтому результата пока нет. "  # i18n-allow: RU voice phrase
        "Повторите, пожалуйста, просьбу."  # i18n-allow: RU voice phrase
    ),  # i18n-allow: Russian runtime voice/chat output
}


class _Clause(NamedTuple):
    """One clause of the response plus the separator that closed it."""

    body: str
    separator: str
    commitment: bool
    defer: bool
    substance: bool


class _Analysis(NamedTuple):
    """What the promise vocabulary means for this particular text.

    ``kind`` is one of:

    * ``"none"``     — no commitment at all; the text is not this guard's business.
    * ``"answered"`` — a commitment plus at least one clause of real substance.
      ``kept`` holds the text with the promise clauses removed.
    * ``"bare"``     — promise wording end to end, nothing delivered.
    * ``"open"``     — a commitment whose tail is too long to read as a bare
      promise and that carries no defer marker; left untouched, as before.
    """

    kind: str
    kept: str


def _content_tokens(normalized: str) -> list[str]:
    return [token for token in _TOKEN_RE.findall(normalized) if token not in _FUNCTION_WORDS]


def _split_clauses(text: str) -> list[_Clause]:
    """Split ``text`` into classified clauses that reassemble losslessly."""
    parts = _CLAUSE_SPLIT_RE.split(str(text or ""))
    pairs: list[list[str]] = []
    for index in range(0, len(parts), 2):
        body = parts[index]
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        if not body.strip() and pairs:
            # An empty body means two separators met (". — "); keep the run
            # attached to the clause before it rather than inventing a clause.
            pairs[-1][1] += body + separator
            continue
        pairs.append([body, separator])

    clauses: list[_Clause] = []
    for body, separator in pairs:
        normalized = _normalize(body).strip()
        commitment = bool(_ACTION_COMMITMENT_RE.search(normalized))
        defer = bool(_DEFER_MARKER_RE.search(normalized))
        substance = (
            not commitment
            and not defer
            and _SUBORDINATE_OPENER_RE.match(normalized) is None
            and len(_content_tokens(normalized)) >= _MIN_SUBSTANCE_TOKENS
        )
        clauses.append(_Clause(body, separator, commitment, defer, substance))
    return clauses


def _tidy(text: str) -> str:
    """Repair the seams left behind when promise clauses are cut out."""
    cleaned = text.strip().lstrip(",;:—–- ").strip()
    cleaned = cleaned.rstrip().rstrip(",;:—–-").rstrip()
    cleaned = _TRAILING_CONJUNCTION_RE.sub("", cleaned).rstrip()
    if not cleaned:
        return ""
    if cleaned[-1] not in ".!?…":
        cleaned += "."
    if cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _keep_substance(clauses: list[_Clause]) -> str:
    """Drop the promise clauses and everything that only depends on them."""
    kept: list[str] = []
    previous_kept = False
    for clause in clauses:
        if clause.commitment or clause.defer:
            previous_kept = False
            continue
        if not clause.substance and not previous_kept:
            # A dependent tail ("when that changes") whose clause is gone
            # would be left dangling, so it goes with it.
            continue
        kept.append(clause.body + clause.separator)
        previous_kept = True
    return _tidy("".join(kept))


def _analyse(text: str) -> _Analysis:
    normalized = _normalize(text).strip()
    if not normalized:
        return _Analysis("none", "")

    commitment = _ACTION_COMMITMENT_RE.search(normalized)
    if commitment is None:
        return _Analysis("none", "")

    clauses = _split_clauses(text)
    if any(clause.substance for clause in clauses):
        kept = _keep_substance(clauses)
        if kept:
            return _Analysis("answered", kept)

    if _DEFER_MARKER_RE.search(normalized):
        return _Analysis("bare", "")

    # A bare commitment with no delivered result is still terminal: after the
    # model response closes there is no hidden continuation that will do it.
    tail = normalized[commitment.end() :].strip(" .,!?:;-")
    if len(tail) <= _MAX_BARE_COMMITMENT_TAIL:
        return _Analysis("bare", "")
    return _Analysis("open", "")


def has_deferred_action_claim(text: str) -> bool:
    """Return whether ``text`` ends the turn on uncompleted future work."""
    return _analyse(text).kind == "bare"


def _clause_is_false_completion(clause: _Clause) -> bool:
    body = clause.body.rstrip()
    if not body or body.endswith("?"):
        return False
    return _FALSE_COMPLETION_RE.search(_normalize(body)) is not None


def has_false_completion_claim(text: str) -> bool:
    """Return whether ``text`` is a false completion with no other substance.

    Used when the model narrates the outcome ("I'm playing you a playlist")
    without a tool call. The deferred-promise analyser cannot see these:
    the fake result is the tail, so they look delivered. A list or answer
    in another clause is substance and must not be replaced.

    Two clause shapes are discounted before that substance question is asked,
    because neither can be a delivered result: a relative continuation of the
    false completion itself, and a send-off. Live 2026-08-22 18:22 — a turn
    that picked out songs, said they were now playing on YouTube Music and
    wished the user fun was spoken with zero function calls. Both trailing
    clauses cleared the structural substance bar, so the guard read the whole
    turn as a delivered answer. The verbatim text is in the regression test.
    """
    clauses = _split_clauses(text)
    if not any(_clause_is_false_completion(clause) for clause in clauses):
        return False
    if any(
        _RU_LOOKUP_CLAIM_RE.search(_normalize(clause.body)) is not None
        for clause in clauses
    ):
        # A Russian "I checked / search shows" claim carries invented figures.
        return True
    previous_was_claim = False
    for clause in clauses:
        if _clause_is_false_completion(clause):
            previous_was_claim = True
            continue
        if not clause.substance:
            continue
        normalized = _normalize(clause.body).strip()
        if _COURTESY_RE.search(normalized):
            continue
        if previous_was_claim and _DEPENDENT_CONTINUATION_RE.match(normalized):
            # It elaborates the claim in front of it and dies with it.
            continue
        previous_was_claim = False
        return False
    return True


# Fork addition: claims about STUDYING and WRITING NOTES. The small local model once announced, over many turns, that a
# psychology course was being studied "in the background" and written to Obsidian while no tool had run at all (the real
# study tool says "N из M блоков" and creates files, so a claim without a tool call is simply false). Matched on the
# normalised text (й->и, ё->е). Applied only when the turn executed NO tool.
_RU_STUDY_CLAIM_RE = re.compile(
    r"(?:"
    r"\bя\s+(?:уже\s+|только\s+что\s+|успешно\s+|сейчас\s+)?"  # i18n-allow: RU
    r"(?:создал|записал|добавил|изучил|прошел|выучил|законспектировал|обновил)[аи]?\b|"  # i18n-allow: RU
    r"\bя\s+(?:сейчас\s+|уже\s+|теперь\s+)?"  # i18n-allow: RU
    r"(?:продолжаю|изучаю|учу|записываю|конспектирую|прохожу|перехожу|разбираю|"  # i18n-allow: RU
    r"изучу|выучу|запишу|законспектирую)\b|"  # i18n-allow: RU
    r"\b(?:теперь|сейчас|далее)\s+(?:изучу|выучу|запишу|законспектирую|буду\s+изучать|буду\s+записывать)\b|"  # i18n-allow: RU
    r"\bперехожу\s+к\b|"  # i18n-allow: RU
    r"\b(?:изучение|обучение|курс)\s+(?:идет|продолжается|запущен\w*|начат\w*|в\s+процессе)\b|"  # i18n-allow: RU
    r"\b(?:идет|идут|работает)\s+(?:автоматическое\s+|автоматически\s+)?(?:изучение|обучение)\b|"  # i18n-allow: RU
    r"\b(?:материал|курс|блок|раздел|тема)\s+(?:разбирается|изучается|изучен\w*|записан\w*|"  # i18n-allow: RU
    r"законспектирован\w*)\b|"  # i18n-allow: RU
    r"\bуже\s+(?:изучен|записан|пройден)\w*\b"  # i18n-allow: RU
    r")"
)

_RU_STUDY_NOT_STARTED = (
    "Обучение не запущено: я не вызывал для этого ни одного инструмента, поэтому ничего не изучено и не записано. "  # i18n-allow: RU
    "Приложите файл в чат и скажите «изучи этот курс», или скажите «изучи файл <имя>» для файла из папки "  # i18n-allow: RU
    "data/courses_inbox."  # i18n-allow: RU
)


def has_false_study_claim(text: str) -> bool:
    """A Russian claim of studying / note-writing (call only when no tool ran this turn)."""
    return bool(_RU_STUDY_CLAIM_RE.search(_normalize(text)))


def has_unbacked_action_claim(text: str) -> bool:
    """Either shape of an action claim that has no execution evidence."""
    return has_deferred_action_claim(text) or has_false_completion_claim(text)


def action_not_started_phrase(language: str) -> str:
    """Return the honest fallback in one resolved runtime output language."""
    return _ACTION_NOT_STARTED_PHRASES.get(
        str(language or "").strip().lower(),
        _ACTION_NOT_STARTED_PHRASES["en"],
    )


def replace_unbacked_action_claim(
    text: str,
    *,
    executed_tools: Collection[str],
    language: str,
) -> str:
    """Drop an unbacked promise without ever dropping a delivered answer."""
    if executed_tools:
        return text
    if has_false_study_claim(text):
        return _RU_STUDY_NOT_STARTED
    if has_false_completion_claim(text):
        return action_not_started_phrase(language)
    analysis = _analyse(text)
    if analysis.kind == "bare":
        return action_not_started_phrase(language)
    if analysis.kind == "answered":
        return analysis.kept
    return text


__all__ = [
    "action_not_started_phrase",
    "has_deferred_action_claim",
    "has_false_completion_claim",
    "has_false_study_claim",
    "has_unbacked_action_claim",
    "replace_unbacked_action_claim",
]
