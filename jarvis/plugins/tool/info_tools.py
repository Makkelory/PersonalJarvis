"""info_tools: weather, currency rates and a safe calculator.

Fork addition (2026-11-14). Real numbers instead of the model's memory:

* ``get_weather`` — Open-Meteo (no key). The city is sent to the geocoder, the
  coordinates to the forecast API; nothing else leaves the machine. The home
  city is NOT hard-coded: it comes from ``data/assistant_settings.json``
  (``{"home_city": "..."}``), so the repository carries no personal location.
* ``get_rates`` — official Bank of Russia daily rates (cbr.ru XML).
* ``calculate`` — arithmetic through a restricted ``ast`` walker (numbers and
  ``+ - * / // % **`` only; never ``eval``).
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import operator
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

from jarvis.core.protocols import ExecutionContext, ToolResult

logger = logging.getLogger(__name__)

_SETTINGS_PATH = Path("data") / "assistant_settings.json"
_HTTP_TIMEOUT = 10.0

_WMO = {
    0: "ясно", 1: "в основном ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь", 51: "лёгкая морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "сильная ледяная морось", 61: "небольшой дождь", 63: "дождь",
    65: "сильный дождь", 66: "ледяной дождь", 67: "сильный ледяной дождь", 71: "небольшой снег",
    73: "снег", 75: "сильный снег", 77: "снежные зёрна", 80: "небольшие ливни", 81: "ливни",
    82: "сильные ливни", 85: "снегопад", 86: "сильный снегопад", 95: "гроза",
    96: "гроза с градом", 99: "сильная гроза с градом",
}


def home_city() -> str:
    """The configured home city, or '' when the user has not set one."""
    try:
        data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        return str(data.get("home_city") or "").strip()
    except (OSError, ValueError):
        return ""


def _signed(value: float) -> str:
    rounded = round(value)
    return f"{rounded:+d}".replace("-", "−") + "°"


def _rank_places(city: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only places whose name is the spoken form or the form minus an
    inflection ending (at most two extra letters after a stem), exact matches
    first, then by population. "казани" -> Казань, never Казахстан; "сочи" ->
    Сочи, never Сочимилько; "москве" -> Москва, never the village Москверо."""
    spoken = city.casefold().strip()
    stems = {spoken[: len(spoken) - cut] if cut else spoken for cut in range(4)}
    stems = {s for s in stems if len(s) >= 3}
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for place in candidates:
        name = str(place.get("name") or "").casefold()
        if not name:
            continue
        if not any(name.startswith(stem) and len(name) - len(stem) <= 2 for stem in stems):
            continue
        ranked.append((0 if name == spoken else 1, -(place.get("population") or 0), place))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [place for _a, _b, place in ranked]


def _weather(city: str, day: str) -> str:
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        # A spoken city is usually in the prepositional case ("в Мытищах",
        # "в Москве"). The geocoder matches name prefixes, so besides the exact
        # form we also try it shortened by up to three letters ("Мытищ" ->
        # Мытищи). The exact form alone can hit a tiny village ("москве" ->
        # Москверо), so among all candidates the most populous place wins.
        candidates: list[dict[str, Any]] = []
        for cut in (0, 1, 2, 3):
            query = city[: len(city) - cut] if cut else city
            if len(query) < 3:
                break
            geo = client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": query, "count": 3, "language": "ru"},
            ).json()
            candidates.extend(geo.get("results") or [])
        places = _rank_places(city, candidates)
        if not places:
            raise ValueError(f"город «{city}» не найден")
        place = places[0]
        forecast = client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"], "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation,cloud_cover",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
                "wind_speed_unit": "kmh", "timezone": "auto", "forecast_days": 3,
            },
        ).json()
    name = place.get("name") or city
    cur = forecast["current"]
    daily = forecast["daily"]
    lines: list[str] = []
    if day == "today":
        sky = _WMO.get(int(cur["weather_code"]), "без описания")
        rain = "осадков нет" if not cur.get("precipitation") else f"осадки {cur['precipitation']} мм"
        lines.append(
            f"{name} сейчас: {_signed(cur['temperature_2m'])} (ощущается как {_signed(cur['apparent_temperature'])}), "
            f"{sky}, ветер {round(cur['wind_speed_10m'])} км/ч, облачность {round(cur['cloud_cover'])}%, {rain}."
        )
    idx = 1 if day == "tomorrow" else 0
    label = "Завтра" if day == "tomorrow" else "Сегодня"
    sky_day = _WMO.get(int(daily["weather_code"][idx]), "без описания")
    prob = daily["precipitation_probability_max"][idx]
    lines.append(
        f"{label}, {name}: от {_signed(daily['temperature_2m_min'][idx])} до {_signed(daily['temperature_2m_max'][idx])}, "
        f"{sky_day}, вероятность осадков {prob if prob is not None else '?'}%."
    )
    return "\n".join(lines)


class GetWeatherTool:
    name: str = "get_weather"
    risk_tier: str = "safe"
    description: str = (
        "Real weather from Open-Meteo for a city (default: the owner's home "
        "city). day: today (current + forecast) or tomorrow. Always call it "
        "for any weather question; never answer weather from memory."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name; empty = home city"},
            "day": {"type": "string", "enum": ["today", "tomorrow"], "default": "today"},
        },
    }

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        city = str(args.get("city") or "").strip() or home_city()
        if not city:
            return ToolResult(success=False, output=None, error="Укажи город: домашний город не задан.")
        day = "tomorrow" if str(args.get("day") or "").lower() == "tomorrow" else "today"
        logger.info("[WEATHER] day=%s trace=%s", day, ctx.trace_id)
        try:
            return ToolResult(success=True, output=await asyncio.to_thread(_weather, city, day))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[WEATHER] failed: %s", exc)
            return ToolResult(success=False, output=None, error=f"Погода недоступна: {exc}")


_CURRENCY_ALIASES = {
    "доллар": "USD", "доллара": "USD", "долларов": "USD", "usd": "USD", "евро": "EUR", "eur": "EUR",
    "юань": "CNY", "юаня": "CNY", "юаней": "CNY", "cny": "CNY", "фунт": "GBP", "фунта": "GBP", "gbp": "GBP",
    "тенге": "KZT", "kzt": "KZT", "лира": "TRY", "лиры": "TRY", "try": "TRY", "йена": "JPY", "иена": "JPY",
    "jpy": "JPY", "франк": "CHF", "франка": "CHF", "chf": "CHF", "белорусский рубль": "BYN", "byn": "BYN",
}


def _rates(query: str) -> str:
    resp = httpx.get("https://www.cbr.ru/scripts/XML_daily.asp", timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    date = root.attrib.get("Date", "")
    table: dict[str, tuple[str, float, int]] = {}
    for item in root.findall("Valute"):
        code = item.findtext("CharCode") or ""
        nominal = int(item.findtext("Nominal") or 1)
        value = float((item.findtext("Value") or "0").replace(",", "."))
        table[code] = (item.findtext("Name") or code, value, nominal)
    wanted = _CURRENCY_ALIASES.get(query.strip().lower(), query.strip().upper()) if query.strip() else ""
    codes = [wanted] if wanted else ["USD", "EUR", "CNY"]
    lines = []
    for code in codes:
        if code not in table:
            raise ValueError(f"валюта «{query}» не найдена в списке ЦБ")
        name, value, nominal = table[code]
        per = f"{nominal} " if nominal != 1 else ""
        lines.append(f"{per}{name} ({code}): {value:.2f} ₽".replace(".", ","))
    return f"Курс ЦБ РФ на {date}:\n" + "\n".join(lines)


class GetRatesTool:
    name: str = "get_rates"
    risk_tier: str = "safe"
    description: str = (
        "Official Bank of Russia exchange rates in rubles. currency: USD, EUR, "
        "CNY, ... or a Russian word (доллар, евро, юань); empty = USD, EUR, CNY. "
        "Always call it for rate questions."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"currency": {"type": "string", "description": "Code or Russian name; empty = main three"}},
    }

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        logger.info("[RATES] query=%r trace=%s", args.get("currency"), ctx.trace_id)
        try:
            return ToolResult(success=True, output=await asyncio.to_thread(_rates, str(args.get("currency") or "")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RATES] failed: %s", exc)
            return ToolResult(success=False, output=None, error=f"Курсы недоступны: {exc}")


_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_WORD_OPS = (
    (r"процент(?:ов|а)?\s+от", "%of"), (r"умножить\s+на|умножь\s+на|помножить\s+на", "*"),
    (r"разделить\s+на|раздели\s+на|делить\s+на", "/"), (r"в\s+степени|возвести\s+в", "**"),
    (r"плюс", "+"), (r"минус", "-"), (r"[×х]", "*"), (r"[÷:]", "/"), (r"\^", "**"),
)


def _prepare(expr: str) -> str:
    text = expr.lower().replace(",", ".").replace(" ", " ")
    text = re.sub(r"(\d)\s+(\d{3})(?!\d)", r"\1\2", text)  # "3 400" -> "3400"
    for pattern, repl in _WORD_OPS:
        text = re.sub(pattern, f" {repl} ", text)
    # "15 %of 3400" -> "(15/100)*3400"
    text = re.sub(r"([\d.]+)\s*%of\s*([\d.]+)", r"((\1/100)*\2)", text)
    return text.strip()


def _safe_eval(node: ast.AST, depth: int = 0) -> float:
    if depth > 20:
        raise ValueError("выражение слишком сложное")
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body, depth + 1)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _safe_eval(node.operand, depth + 1)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _safe_eval(node.left, depth + 1), _safe_eval(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
            raise ValueError("слишком большая степень")
        return _BINOPS[type(node.op)](left, right)
    raise ValueError("разрешены только числа и арифметика")


def calculate_expression(expr: str) -> str:
    """Evaluate arithmetic safely and format the result in Russian style."""
    prepared = _prepare(expr)
    if not prepared or len(prepared) > 200:
        raise ValueError("пустое или слишком длинное выражение")
    value = _safe_eval(ast.parse(prepared, mode="eval"))
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    text = f"{value:.6g}" if isinstance(value, float) else str(value)
    return f"{expr.strip()} = {text.replace('.', ',')}"


class CalculateTool:
    name: str = "calculate"
    risk_tier: str = "safe"
    description: str = (
        "Exact arithmetic: + - * / // % ** and parentheses; Russian words work "
        "('15 процентов от 3400', '12 умножить на 7'). Use it for any calculation "
        "instead of computing in your head."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"expression": {"type": "string", "description": "Arithmetic expression"}},
        "required": ["expression"],
    }

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        try:
            return ToolResult(success=True, output=calculate_expression(str(args.get("expression") or "")))
        except ZeroDivisionError:
            return ToolResult(success=False, output=None, error="Делить на ноль нельзя.")
        except (ValueError, SyntaxError, OverflowError) as exc:
            return ToolResult(success=False, output=None, error=f"Не смог посчитать: {exc}")
