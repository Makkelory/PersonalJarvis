"""study_course: a step-by-step course Jarvis studies on its own, one block at a time.

Fork addition (2026-11-14). The owner ("Сэр") wants Jarvis to become well versed in
Computer Science by studying a full course in chat on command, stopping after every
block to report what it learned and to ask whether to continue.

The tool keeps the progress (``data/courses.json``) and the Obsidian notes; the
*studying* (web search, reading, forming an understanding) is done by the model
in chat, driven by the text ``next`` returns. ``complete`` records the model's own
summary as a linked knowledge note (block -> previous block -> course hub), so the
Obsidian graph shows how the topics connect.

Flow per block: ``next`` -> model searches and studies -> ``complete`` -> the model
tells the owner the result and asks "continue?". It never calls ``next`` again
without the owner's go-ahead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from html.parser import HTMLParser
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult

logger = logging.getLogger(__name__)

_STORE = Path("data") / "courses.json"
_INBOX = Path("data") / "courses_inbox"
_TEXTS = Path("data") / "course_texts"
_DOC_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".log", ".html", ".htm", ".pdf"}
_DEFAULT = "computer-science"
_TITLES = {"computer-science": "Computer Science", "windows": "Windows"}
#: Courses whose blocks are practical: commands are collected into a playbook and each finished module becomes a skill.
PRACTICAL = {"windows"}

#: Built-in syllabus: module -> blocks. Order is the study order.
SYLLABI: dict[str, list[tuple[str, list[str]]]] = {
    "windows": [
        ("Основы Windows и оболочка", [
            "Устройство Windows: ядро, оболочка, учётные записи, редакции",
            "Проводник, Пуск, поиск и рабочие столы",
            "Параметры, Панель управления и ms-settings",
            "Горячие клавиши Windows",
            "Буфер обмена, история буфера и вставка",
            "Файлы, расширения, ярлыки и ассоциации",
        ]),
        ("Командная строка cmd", [
            "Основы cmd: запуск, синтаксис, справка /?",
            "Навигация и файлы: cd, dir, copy, move, del, ren, md, rd",
            "Переменные среды: set, PATH, %VAR%",
            "Перенаправление и конвейер: >, >>, |, &&, ||",
            "Bat-файлы: if, for, goto, параметры",
            "Утилиты: tasklist, taskkill, ipconfig, ping, where",
            "Ассоциации и запуск: start, assoc, ftype",
        ]),
        ("PowerShell", [
            "Основы PowerShell: командлеты Глагол-Существительное, Get-Help, Get-Command",
            "Объекты и конвейер: Select-Object, Where-Object, Sort-Object, ForEach-Object",
            "Вывод и экспорт: Format-Table, Export-Csv, ConvertTo-Json, Out-File",
            "Переменные, массивы, хеш-таблицы и строки",
            "Условия, циклы, функции и параметры",
            "Скрипты и политика выполнения",
            "Модули, PowerShell Gallery и Get-Module",
            "Обработка ошибок: try/catch, ErrorAction",
            "Удалённое управление: Invoke-Command, WinRM, PSSession",
        ]),
        ("Файловая система и диски", [
            "NTFS: структура, потоки и атрибуты",
            "Пути, символические ссылки, junction и hardlink",
            "Диски и разделы: Get-Disk, diskpart, форматирование",
            "Права доступа: ACL, icacls, Get-Acl, наследование",
            "Шифрование: BitLocker и EFS",
            "Поиск и копирование: Get-ChildItem -Recurse, robocopy, xcopy",
            "Архивы и сжатие: Compress-Archive, tar, 7-Zip",
            "Теневые копии, история файлов и корзина",
        ]),
        ("Процессы и службы", [
            "Процессы: tasklist, Get-Process, Stop-Process",
            "Диспетчер задач: приоритеты, привязка к ядрам, детали",
            "Службы: sc, Get-Service, services.msc, типы запуска",
            "Автозагрузка: реестр, папки, планировщик",
            "Планировщик заданий: schtasks, Register-ScheduledTask",
            "Фоновые задания: Start-Job, Start-Process",
        ]),
        ("Реестр Windows", [
            "Структура реестра: ветви, разделы, параметры, типы",
            "Работа с реестром: reg.exe, Get-ItemProperty, Set-ItemProperty",
            "Резервные копии и импорт: reg export, reg import",
            "Типичные настройки через реестр и их риски",
        ]),
        ("Сеть", [
            "Настройка TCP/IP: ipconfig, Get-NetIPConfiguration, DHCP",
            "DNS: nslookup, Resolve-DnsName, кэш и flushdns",
            "Диагностика: ping, tracert, pathping, Test-NetConnection",
            "Порты и соединения: netstat, Get-NetTCPConnection",
            "Wi-Fi: netsh wlan, профили и пароли",
            "Брандмауэр: netsh advfirewall, New-NetFirewallRule",
            "Общие папки и SMB: net use, New-SmbShare",
            "Удалённый рабочий стол RDP и mstsc",
            "Прокси и VPN: netsh winhttp, Add-VpnConnection",
        ]),
        ("Пользователи и безопасность", [
            "Локальные учётные записи и группы: net user, Get-LocalUser",
            "UAC и запуск от имени администратора",
            "Microsoft Defender: Get-MpComputerStatus, Start-MpScan",
            "Локальная политика безопасности и групповые политики: gpedit, gpresult",
            "Журнал безопасности и аудит",
            "Диспетчер учётных данных и пароли",
            "Практики безопасности Windows для администратора",
        ]),
        ("Обновления, драйверы и восстановление", [
            "Windows Update: управление и диагностика",
            "Драйверы: pnputil, Get-PnpDevice, откат",
            "Проверка целостности: sfc /scannow и DISM",
            "Точки восстановления и откат системы",
        ]),
        ("Установка и управление программами", [
            "winget: поиск, установка, обновление, экспорт",
            "Chocolatey и Scoop",
            "Тихая установка MSI и EXE и ключи установщиков",
            "Список и удаление программ: Get-Package, Get-AppxPackage",
            "Переменная PATH и портативные программы",
        ]),
        ("Мониторинг и диагностика", [
            "Журналы событий: eventvwr, Get-WinEvent, фильтры",
            "Производительность: perfmon, Get-Counter, typeperf",
            "Сведения о системе: systeminfo, Get-ComputerInfo, msinfo32",
            "Диски и их состояние: Get-PhysicalDisk, SMART",
            "Синие экраны и дампы памяти",
            "Электропитание и батарея: powercfg, отчёт батареи",
            "Монитор надёжности и диагностика зависаний",
        ]),
        ("Загрузка и питание", [
            "Процесс загрузки: UEFI, BCD, bcdedit",
            "Безопасный режим и среда восстановления WinRE",
            "Схемы электропитания, гибернация, быстрый запуск",
            "Мультизагрузка и диск восстановления",
        ]),
        ("Виртуализация и WSL", [
            "WSL: установка, дистрибутивы, интеграция с Windows",
            "Hyper-V: виртуальные машины и Get-VM",
            "Windows Sandbox и контейнеры",
            "Docker Desktop на Windows",
        ]),
        ("Автоматизация", [
            "Когда cmd, PowerShell или Python",
            "WMI и CIM: Get-CimInstance, запросы",
            "Текст и регулярные выражения: Select-String, findstr, -match",
            "Горячие клавиши и AutoHotkey",
            "Управление окнами и UI Automation",
            "Планировщик плюс скрипты: типовые сценарии администратора",
        ]),
        ("Встроенные программы Windows", [
            "Проводник: возможности, поиск, быстрый доступ",
            "Блокнот, Paint, Ножницы и калькулятор",
            "Консоли mmc: управление компьютером, диски, устройства",
            "Редактор реестра и редактор групповой политики",
            "Магазин и приложения UWP",
            "Диспетчер задач, монитор ресурсов и системный монитор",
        ]),
        ("Резервное копирование", [
            "История файлов и резервные копии: wbadmin",
            "Образ системы и восстановление",
            "OneDrive и синхронизация папок",
            "robocopy для зеркалирования и бэкапов",
        ]),
        ("Устройства и печать", [
            "Принтеры и очередь печати: Get-Printer",
            "USB и Bluetooth: Get-PnpDevice",
            "Звук и аудиоустройства",
        ]),
        ("Типичные неполадки", [
            "Windows медленно работает: диагностика по шагам",
            "Нет интернета: диагностика сети по шагам",
            "Программа зависла или не запускается",
            "Диск заполнен: поиск больших файлов и очистка",
            "Windows не загружается: восстановление",
            "Ошибки обновлений и повреждение системных файлов",
        ]),
    ],
    "computer-science": [
        ("Основы вычислений", [
            "Что такое вычисление и информация: биты и байты",
            "Системы счисления и двоичная арифметика",
            "Булева логика и логические элементы",
            "Кодирование текста и чисел: ASCII, Unicode, числа с плавающей точкой",
        ]),
        ("Архитектура компьютера", [
            "Процессор: регистры, АЛУ, цикл выборки и исполнения",
            "Память: иерархия, кэш, оперативная память, накопители",
            "Система команд, ассемблер и машинный код",
            "Конвейер, параллелизм на уровне команд, многоядерность",
            "Ввод-вывод, шины и прерывания",
        ]),
        ("Операционные системы", [
            "Ядро, системные вызовы, режимы работы процессора",
            "Процессы и потоки, планирование",
            "Виртуальная память и страничная организация",
            "Файловые системы",
            "Синхронизация: мьютексы, семафоры, взаимные блокировки",
        ]),
        ("Структуры данных", [
            "Массивы, связные списки, стеки и очереди",
            "Хеш-таблицы",
            "Деревья: двоичные, сбалансированные, B-деревья",
            "Кучи и очереди с приоритетом",
            "Графы и способы их представления",
        ]),
        ("Алгоритмы и сложность", [
            "Асимптотический анализ: O-нотация",
            "Поиск и сортировки",
            "Рекурсия и разделяй-и-властвуй",
            "Динамическое программирование и жадные алгоритмы",
            "Алгоритмы на графах: обходы, кратчайшие пути, остовные деревья",
        ]),
        ("Языки программирования", [
            "Парадигмы: императивная, функциональная, объектно-ориентированная",
            "Компиляция, интерпретация, виртуальные машины",
            "Типы данных и системы типов",
            "Управление памятью: стек, куча, сборка мусора",
            "Замыкания, области видимости, исключения",
        ]),
        ("Теория вычислений", [
            "Конечные автоматы и регулярные выражения",
            "Контекстно-свободные грамматики и разбор",
            "Машина Тьюринга и разрешимость",
            "Классы P и NP, NP-полнота",
        ]),
        ("Компьютерные сети", [
            "Модели OSI и TCP/IP",
            "IP-адресация, маршрутизация, NAT",
            "TCP и UDP: надёжность и управление перегрузкой",
            "DNS, HTTP, HTTPS и TLS",
        ]),
        ("Базы данных", [
            "Реляционная модель и SQL",
            "Нормализация и проектирование схем",
            "Индексы и планы запросов",
            "Транзакции, ACID и изоляция",
            "NoSQL и когда он нужен",
        ]),
        ("Информационная безопасность и криптография", [
            "Симметричное и асимметричное шифрование",
            "Хеш-функции, подписи, сертификаты",
            "Аутентификация и авторизация",
            "Типичные уязвимости и защита",
        ]),
        ("Программная инженерия", [
            "Системы контроля версий и Git",
            "Тестирование и качество кода",
            "Архитектура ПО и шаблоны проектирования",
            "CI/CD и сопровождение в эксплуатации",
        ]),
        ("Распределённые системы", [
            "Репликация, шардирование и согласованность",
            "Теорема CAP и консенсус (Paxos, Raft)",
            "Очереди сообщений и событийные системы",
            "Отказоустойчивость, наблюдаемость, масштабирование",
        ]),
        ("Искусственный интеллект и машинное обучение", [
            "Основы машинного обучения: обучение с учителем и без",
            "Нейронные сети и обратное распространение",
            "Трансформеры и большие языковые модели",
            "Оценка моделей, переобучение, этика ИИ",
        ]),
    ],
}


def awaiting_target() -> tuple[str, dict[str, Any]] | None:
    """What the owner's bare "continue" refers to: the course or the program study that asked last."""
    import time  # noqa: PLC0415

    best: tuple[float, str, dict[str, Any]] | None = None
    for course in _load().values():
        stamp = course.get("awaiting") if isinstance(course, dict) else None
        if isinstance(stamp, (int, float)) and time.time() - stamp < 3 * 3600 and course.get("state") == "active":
            if best is None or stamp > best[0]:
                best = (float(stamp), "study_course", {"action": "study"})
    try:
        from jarvis.plugins.tool import app_study  # noqa: PLC0415

        stamp = app_study._load().get("awaiting")  # noqa: SLF001
        if isinstance(stamp, (int, float)) and time.time() - stamp < 3 * 3600 and (best is None or stamp > best[0]):
            best = (float(stamp), "study_app", {"action": "study_next"})
    except Exception:  # noqa: BLE001
        pass
    return (best[1], best[2]) if best else None


def awaiting_continue(max_age_s: float = 3 * 3600) -> bool:
    """True while a finished block waits for the owner's "continue?" answer.

    The deterministic command gate uses it so that a bare "дальше" / "продолжай" in that
    moment goes to the model (the course) and not to Spotify's next track / play.
    """
    import time  # noqa: PLC0415

    for course in _load().values():
        stamp = course.get("awaiting") if isinstance(course, dict) else None
        if isinstance(stamp, (int, float)) and time.time() - stamp < max_age_s and course.get("state") == "active":
            return True
    return False


def _load() -> dict[str, Any]:
    try:
        blob = json.loads(_STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(blob, dict):
        return {}
    for k, c in blob.items():
        if isinstance(c, dict) and c.get("title"):
            _TITLES[k] = str(c["title"])
    return blob


def _save(blob: dict[str, Any]) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(blob, ensure_ascii=False, indent=1), encoding="utf-8")


def _flat(syllabus: list[tuple[str, list[str]]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for mi, (module, titles) in enumerate(syllabus, start=1):
        for bi, title in enumerate(titles, start=1):
            blocks.append({"id": f"{mi}.{bi}", "module": module, "title": title})
    return blocks


def _slug(name: str) -> str:
    text = " ".join((name or _DEFAULT).lower().replace("ё", "е").split())
    if re.search(r"windows|виндовс|винд\w*|powershell|пауэршелл\w*|павершелл\w*|\bcmd\b|командн\w+ строк\w+|терминал\w*", text):
        return "windows"
    return " ".join(text.replace("компьютер саенс", "computer-science")
                    .replace("computer science", "computer-science").replace("информатика", "computer-science").split())


def _course_title(key: str) -> str:
    return _TITLES.get(key, key.replace("-", " ").title())


def _note_name(block: dict[str, Any]) -> str:
    """File-safe note title (the same characters memory_notes._learn would strip)."""
    cleaned = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", f"{block['id']} {block['title']}")
    return " ".join(cleaned.split())[:80]


def _hub_name(key: str) -> str:
    return f"Курс {_course_title(key)}"


def _write_hub(key: str, course: dict[str, Any]) -> None:
    vault = Path("wiki/obsidian-vault") / "jarvis-knowledge" / "Знания"
    vault.mkdir(parents=True, exist_ok=True)
    done = {b: v for b, v in course["done"].items()}
    lines = ["---", "type: jarvis-course", f"course: {_course_title(key)}", f"started: {course['started']}",
             f"done: {len(done)}", f"total: {len(course['blocks'])}", f"state: {course.get('state', 'active')}", "---", "",
             f"# {_hub_name(key)}", "",
             f"Курс, который Jarvis изучает по просьбе Сэра, блок за блоком. Прогресс: {len(done)} из {len(course['blocks'])}.", "",
             "Как это устроено: [[jarvis-knowledge/Обучение|Обучение]]. Все знания: [[jarvis-knowledge/Знания|Знания]].", ""]
    playbook = f"Приёмы {_course_title(key)}"
    if (vault / f"{playbook}.md").exists():
        lines += [f"Справочник команд и приёмов: [[jarvis-knowledge/Знания/{playbook}|{playbook}]]", ""]
    module = None
    for b in course["blocks"]:
        if b["module"] != module:
            module = b["module"]
            lines += ["", f"## {module}", ""]
        mark = "x" if b["id"] in done else " "
        name = _note_name(b)
        target = f"[[jarvis-knowledge/Знания/{name}|{b['title']}]]" if b["id"] in done else b["title"]
        lines.append(f"- [{mark}] {b['id']} {target}")
    (vault / f"{_hub_name(key)}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _status(course: dict[str, Any], key: str) -> str:
    total, done = len(course["blocks"]), len(course["done"])
    cur = next((b for b in course["blocks"] if b["id"] not in course["done"]), None)
    state = {"active": "идёт", "paused": "на паузе", "finished": "завершён"}.get(course.get("state", "active"), "")
    if cur is None:
        return f"Курс «{_course_title(key)}» завершён: изучено {done} из {total} блоков."
    return (f"Курс «{_course_title(key)}» ({state}): изучено {done} из {total} блоков "
            f"({100 * done // max(total, 1)}%). Следующий блок: {cur['id']} «{cur['title']}» (модуль «{cur['module']}»).")


# ---------------------------------------------------------------------------
# Self-contained research: the 9B chat model cannot be trusted to chain
# search -> read -> write -> record, so the tool does the whole block itself.
# ---------------------------------------------------------------------------
_UA = {"User-Agent": "Mozilla/5.0 (Jarvis study course)"}
_PAGE_CHARS = 8000


class _ParagraphText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self._in_p = False
        self._pre = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:  # noqa: ANN401
        if tag in {"script", "style", "nav", "footer", "header", "aside", "noscript"}:
            self._skip += 1
        elif tag == "pre" and not self._skip:
            self._in_p = True
            self._pre += 1
            self.parts.append("\n")
        elif tag in {"p", "li", "h2", "h3"} and not self._skip:
            self._in_p = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "footer", "header", "aside", "noscript"} and self._skip:
            self._skip -= 1
        elif tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._in_p = False
            self.parts.append("\n")
        elif tag in {"p", "li", "h2", "h3"}:
            self._in_p = False
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._in_p or self._skip:
            return
        if self._pre:
            self.parts.append(data)  # code keeps its line breaks
        elif data.strip():
            self.parts.append(data.strip() + " ")


def _page_text(url: str) -> str:
    """Readable text of a page: Wikipedia through its plain-text API, others by <p>/<li> extraction."""
    import httpx  # noqa: PLC0415

    host = urllib.parse.urlparse(url).netloc
    try:
        if host.endswith("wikipedia.org"):
            title = urllib.parse.unquote(url.rsplit("/wiki/", 1)[-1].split("#")[0])
            api = f"https://{host}/w/api.php"
            r = httpx.get(api, params={"action": "query", "prop": "extracts", "explaintext": 1, "format": "json",
                                       "titles": title, "redirects": 1}, headers=_UA, timeout=15)
            pages = r.json().get("query", {}).get("pages", {})
            return next(iter(pages.values()), {}).get("extract", "")[:_PAGE_CHARS]
        r = httpx.get(url, headers=_UA, timeout=15, follow_redirects=True)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return ""
        parser = _ParagraphText()
        parser.feed(r.text)
        text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
        return re.sub(r"\n\s*\n+", "\n", text).strip()[:_PAGE_CHARS]
    except Exception:  # noqa: BLE001 - a dead page is simply skipped
        return ""


def _wiki_results(query: str) -> list[dict[str, str]]:
    """Wikipedia article hits (ru, then en) for a query: a reliable backbone next to the web search."""
    import httpx  # noqa: PLC0415

    out: list[dict[str, str]] = []
    for lang in ("ru", "en"):
        try:
            r = httpx.get(f"https://{lang}.wikipedia.org/w/api.php", headers=_UA, timeout=10, params={
                "action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": 2})
            for hit in r.json().get("query", {}).get("search", []):
                title = hit["title"]
                out.append({"title": title, "snippet": re.sub(r"<[^>]+>", "", hit.get("snippet", "")),
                            "url": f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))})
        except Exception:  # noqa: BLE001
            continue
    return out


def _learn_results(query: str) -> list[dict[str, str]]:
    """Microsoft Learn hits (ru, then en): the authoritative source for Windows, PowerShell and cmd."""
    import httpx  # noqa: PLC0415

    out: list[dict[str, str]] = []
    for locale in ("ru-ru", "en-us"):
        try:
            r = httpx.get("https://learn.microsoft.com/api/search", headers=_UA, timeout=10,
                          params={"search": query, "locale": locale, "$top": 3})
            for hit in r.json().get("results", []):
                if hit.get("url"):
                    out.append({"title": hit.get("title", ""), "snippet": hit.get("description", ""), "url": hit["url"]})
        except Exception:  # noqa: BLE001
            continue
    return out


def _queries(block: dict[str, Any]) -> list[str]:
    title = block["title"]
    parts = [p.strip() for p in re.split(r"[:,]", title) if len(p.strip()) > 3]
    variants = [title, *parts, f"{parts[0] if parts else title} computer science"]
    return list(dict.fromkeys(variants))[:4]


async def _gather(block: dict[str, Any], queries: list[str] | None = None, learn: bool = False) -> tuple[str, list[str]]:
    """Search the web for the block and read the best pages; returns (materials, source urls)."""
    from jarvis.plugins.tool.search_web import SearchWebTool  # noqa: PLC0415

    tool = SearchWebTool()
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(items: list[dict[str, str]]) -> None:
        for item in items:
            url = item.get("url")
            if url and url not in seen:
                seen.add(url)
                results.append(item)

    qs = queries or _queries(block)
    if learn:  # documentation first: it is fast and has the exact commands
        for query in qs[:3]:
            _add(await asyncio.to_thread(_learn_results, query))
        for query in sorted(qs, key=len)[:2]:
            _add(await asyncio.to_thread(_wiki_results, query))
    for query in ([] if len(results) >= 4 else qs):
        for attempt in range(2 if not results else 1):  # the backend answers "empty" now and then: retry only while nothing is found
            res = await tool.execute({"query": query}, None)
            out = getattr(res, "output", None)
            if isinstance(out, dict) and out.get("results"):
                _add(list(out.get("results", [])))
                break
            await asyncio.sleep(1.0)
        if len(results) >= 6:
            break
        await asyncio.sleep(0.5)
    for query in sorted(qs, key=len)[:2]:
        _add(await asyncio.to_thread(_wiki_results, query))
    results.sort(key=lambda r: 0 if "learn.microsoft.com" in r["url"] else 1 if "wikipedia.org" in r["url"] else 2)
    snippets = "\n".join(f"- {r.get('title', '')}: {r.get('snippet', '')}" for r in results[:10])
    pages: list[str] = []
    sources: list[str] = []
    for r in results[:8]:
        if len(pages) >= 2:
            break
        text = await asyncio.to_thread(_page_text, r["url"])
        if len(text) > 400:
            pages.append(f"[{r['url']}]\n{text}")
            sources.append(r["url"])
    if not sources:
        sources = [r["url"] for r in results[:2]]
    return (snippets + "\n\n" + "\n\n".join(pages)).strip(), sources


def _ollama_model() -> str:
    try:
        from jarvis.core import config as cfg  # noqa: PLC0415

        provider = cfg.load_config().brain.providers.get("ollama")
        return (getattr(provider, "model", "") or "qwen3.5:9b").strip()
    except Exception:  # noqa: BLE001
        return "qwen3.5:9b"


def _summarize(block: dict[str, Any], materials: str, previous: str | None, practical: bool = False,
               course_title: str = "Computer Science") -> str:
    """The local model writes the lesson from the gathered text only (no cloud)."""
    import httpx  # noqa: PLC0415

    if practical:
        prompt = (
            f"Тема урока: «{block['title']}» (модуль «{block['module']}», курс «{course_title}»).\n"
            + (f"Предыдущий урок: «{previous}». Свяжи с ним одним предложением.\n" if previous else "")
            + "Ниже материалы из интернета. Напиши по-русски практический конспект на 7-10 предложений: суть, как это "
            "работает, один пример, типичная ошибка. Затем блок «Команды и приёмы:»: по строке на команду в виде "
            "«PowerShell: команда — что делает» или «cmd: команда — что делает», с ТОЧНЫМИ командами из материалов "
            "(если их нет, напиши «нет»). Затем строка «Опасно:» с командами или действиями, которые могут навредить "
            "(или «нет»). В самом конце строка «Ключевые термины: » и 4-8 терминов. Опирайся только на материалы, "
            "команды и ключи не выдумывай.\n\nМАТЕРИАЛЫ:\n" + materials[:9000]
        )
        body = {"model": _ollama_model(), "messages": [{"role": "user", "content": prompt}], "stream": False,
                "think": False, "options": {"num_ctx": 12288, "temperature": 0.2}, "keep_alive": "30m"}
        r = httpx.post("http://localhost:11434/api/chat", json=body, timeout=300)
        r.raise_for_status()
        return str(r.json().get("message", {}).get("content", "")).strip()
    prompt = (
        f"Тема урока: «{block['title']}» (модуль «{block['module']}», курс Computer Science).\n"
        + (f"Предыдущий урок: «{previous}». Свяжи новую тему с ним одним предложением.\n" if previous else "")
        + "Ниже материалы из интернета. Напиши по-русски конспект на 7-10 предложений: суть темы, ключевые понятия "
        "с краткими определениями, как это работает, один конкретный пример и типичная ошибка. Опирайся только на "
        "материалы, ничего не выдумывай, цифры и термины бери из текста. В конце отдельной строкой напиши "
        "«Ключевые термины: » и 4-8 терминов через запятую.\n\nМАТЕРИАЛЫ:\n" + materials[:9000]
    )
    body = {
        "model": _ollama_model(),
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {"num_ctx": 12288, "temperature": 0.3},
        "keep_alive": "30m",
    }
    r = httpx.post("http://localhost:11434/api/chat", json=body, timeout=300)
    r.raise_for_status()
    return str(r.json().get("message", {}).get("content", "")).strip()


def _read_document(path: Path) -> str:
    """Plain text of a txt/md/html/pdf file."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader  # noqa: PLC0415

        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    if suffix in {".html", ".htm"}:
        parser = _ParagraphText()
        parser.feed(text)
        text = "".join(parser.parts)
    return text


_HEADING_RE = re.compile(r"^(?:#{1,4}\s+\S|(?:глава|урок|раздел|модуль|тема|lesson|chapter|part|section)\s+\S)", re.I)


def _chunk_text(text: str, target: int = 3500, minimum: int = 1200) -> list[tuple[str, str]]:
    """Split a document into study blocks of ~target chars, preferring heading boundaries."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    chunks: list[list[str]] = [[]]
    size = 0
    for ln in lines:
        heading = bool(_HEADING_RE.match(ln.strip()))
        if (size >= target) or (heading and size >= minimum):
            chunks.append([])
            size = 0
        chunks[-1].append(ln)
        size += len(ln) + 1
    out: list[tuple[str, str]] = []
    for part in chunks:
        body = "\n".join(part).strip()
        if len(body) < 80:
            if out and body:
                out[-1] = (out[-1][0], out[-1][1] + "\n" + body)
            continue
        first = next((ln.strip(" #\t") for ln in part if ln.strip()), "Фрагмент")
        out.append((first[:60].strip(" .:-") or "Фрагмент", body))
    return out


def _find_file(name: str) -> Path | None:
    """A file by absolute path, or by (part of) its name inside the inbox."""
    name = (name or "").strip().strip('"')
    if not name:
        return None
    candidate = Path(name)
    if candidate.is_file():
        return candidate
    _INBOX.mkdir(parents=True, exist_ok=True)
    files = [p for p in _INBOX.rglob("*") if p.is_file() and p.suffix.lower() in _DOC_SUFFIXES]
    low = name.lower().replace("ё", "е")
    for p in files:
        if p.name.lower() == low or p.stem.lower() == low:
            return p
    for p in files:
        if low in p.stem.lower().replace("ё", "е"):
            return p
    return None


def _file_key(path: Path) -> str:
    return "file-" + re.sub(r"[^\w]+", "-", path.stem.lower()).strip("-")


def _ingest_file(path: Path, blob: dict[str, Any]) -> str:
    """Register ``path`` as a course (blocks = chunks); returns its key. Existing courses are kept."""
    key = _file_key(path)
    if key in blob and blob[key].get("size") == path.stat().st_size:
        return key
    chunks = _chunk_text(_read_document(path))
    if not chunks:
        raise ValueError(f"В файле «{path.name}» нет читаемого текста.")
    module = path.stem[:60]
    blocks = [{"id": f"1.{i}", "module": module, "title": title} for i, (title, _body) in enumerate(chunks, start=1)]
    _TEXTS.mkdir(parents=True, exist_ok=True)
    (_TEXTS / f"{key}.json").write_text(
        json.dumps({b["id"]: body for b, (_t, body) in zip(blocks, chunks, strict=True)}, ensure_ascii=False), encoding="utf-8")
    blob[key] = {"state": "active", "started": datetime.now().strftime("%Y-%m-%d"), "blocks": blocks, "done": {},
                 "title": module, "source": str(path), "size": path.stat().st_size}
    _TITLES[key] = module
    _save(blob)
    _write_hub(key, blob[key])
    return key


def _inbox_files() -> list[Path]:
    _INBOX.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in _INBOX.rglob("*") if p.is_file() and p.suffix.lower() in _DOC_SUFFIXES)


def _pick_key(requested: str, blob: dict[str, Any]) -> str:
    """The course to study: the named one, else the one waiting for 'continue', else the latest unfinished."""
    if requested:
        return _slug(requested)
    waiting = [(c.get("awaiting") or 0, k) for k, c in blob.items() if isinstance(c, dict) and c.get("awaiting")]
    if waiting:
        return max(waiting)[1]
    open_courses = [k for k, c in blob.items() if len(c.get("done", {})) < len(c.get("blocks", [])) and c.get("state") == "active"]
    return open_courses[-1] if open_courses else _DEFAULT


def _summarize_file(block: dict[str, Any], text: str, previous: str | None, course: str) -> str:
    import httpx  # noqa: PLC0415

    prompt = (
        f"Фрагмент учебного материала «{course}», тема «{block['title']}».\n"
        + (f"Предыдущий фрагмент: «{previous}». Свяжи с ним одним предложением.\n" if previous else "")
        + "Напиши по-русски конспект на 6-10 предложений: суть, ключевые понятия с определениями, как это "
        "применяется. Опирайся только на текст, ничего не выдумывай. Затем отдельным блоком "
        "«Команды и приёмы:» перечисли построчно точные команды, шаги и приёмы из текста (если их нет, напиши «нет»). "
        "В самом конце строка «Ключевые термины: » и 4-8 терминов через запятую.\n\nТЕКСТ:\n" + text[:7000]
    )
    body = {"model": _ollama_model(), "messages": [{"role": "user", "content": prompt}], "stream": False,
            "think": False, "options": {"num_ctx": 12288, "temperature": 0.2}, "keep_alive": "30m"}
    r = httpx.post("http://localhost:11434/api/chat", json=body, timeout=300)
    r.raise_for_status()
    return str(r.json().get("message", {}).get("content", "")).strip()


async def _skill_when_finished(tool: "StudyCourseTool", key: str, course: dict[str, Any]) -> str:
    """A finished file course becomes a draft skill on its own (the owner activates it)."""
    if len(course["done"]) < len(course["blocks"]) or course.get("skill"):
        return ""
    try:
        from jarvis.plugins.tool.skill_from_knowledge import make_skill  # noqa: PLC0415

        ok, message, info = await make_skill(course.get("title", key), getattr(tool, "_ctx", None), course.get("title", ""))
    except Exception as exc:  # noqa: BLE001 - the lesson is recorded either way
        logger.warning("auto skill creation failed: %s", exc, exc_info=True)
        return ""
    if not ok:
        return "\nНавык автоматически создать не удалось: " + message
    blob = _load()
    if key in blob:
        blob[key]["skill"] = info.get("skill_name", "")
        _save(blob)
    return "\n" + message


async def _study_file(tool: "StudyCourseTool", key: str, blob: dict[str, Any], cur: dict[str, Any]) -> str:
    course = blob[key]
    idx = course["blocks"].index(cur)
    previous = course["blocks"][idx - 1]["title"] if idx > 0 else None
    try:
        texts = json.loads((_TEXTS / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "ERROR: Текст курса не найден, загрузите файл заново."
    text = texts.get(cur["id"], "")
    summary = await asyncio.to_thread(_summarize_file, cur, text, previous, course.get("title", key))
    if len(summary) < 150:
        return "ERROR: Локальная модель не составила конспект. Блок не отмечен изученным."
    terms: list[str] = []
    m = re.search(r"Ключевые термины:\s*(.+)", summary)
    if m:
        terms = [t.strip(" .") for t in m.group(1).split(",") if t.strip(" .")]
        summary = summary[: m.start()].strip()
    practices = ""
    pm = re.search(r"Команды и приёмы:\s*(.*)", summary, re.S)
    if pm:
        practices = pm.group(1).strip()
    if practices and practices.lower().strip(" .") not in {"нет", "нет."}:
        folder = Path("wiki/obsidian-vault") / "jarvis-knowledge" / "Знания"
        folder.mkdir(parents=True, exist_ok=True)
        playbook = folder / f"Приёмы {_course_title(key)}.md"
        if not playbook.exists():
            playbook.write_text(f"---\ntype: jarvis-playbook\ncourse: {_course_title(key)}\n---\n\n# Приёмы {_course_title(key)}\n\n"
                                "Команды и приёмы из загруженного курса. Jarvis сверяется с ними перед работой.\n\n", encoding="utf-8")
        link = f"[[jarvis-knowledge/Знания/{_note_name(cur)}|{cur['title']}]]"
        with playbook.open("a", encoding="utf-8") as f:
            f.write(f"## {cur['id']} {cur['title']}\n\n{practices}\n\nИз блока: {link}\n\n")
    done = tool._run({"action": "complete", "course": key, "block_id": cur["id"], "summary": summary,
                      "key_terms": terms, "source": Path(course.get("source", "")).name})
    if done.startswith("ERROR:"):
        return done
    brief = " ".join(re.split(r"(?<=[.!?])\s+", summary)[:3])
    fresh = _load()[key]
    return (f"Сэр, фрагмент {cur['id']} «{cur['title']}» курса «{course.get('title', key)}» изучен. Кратко: {brief}\n"
            f"Ключевые понятия: {', '.join(terms[:6]) or '—'}. "
            + ("Команды и приёмы записаны в справочник. " if practices and practices.lower().strip(" .") != "нет" else "")
            + f"Записано в Obsidian: «{_note_name(cur)}». Изучено {len(fresh['done'])} из {len(fresh['blocks'])}.\n"
            + ("Продолжить, Сэр?" if len(fresh["done"]) < len(fresh["blocks"]) else "Файл изучен полностью.")
            + await _skill_when_finished(tool, key, fresh))


def _write_playbook(key: str, cur: dict[str, Any], practices: str) -> None:
    """Append the exact commands of a block to the course playbook "Приёмы <курс>"."""
    folder = Path("wiki/obsidian-vault") / "jarvis-knowledge" / "Знания"
    folder.mkdir(parents=True, exist_ok=True)
    playbook = folder / f"Приёмы {_course_title(key)}.md"
    if not playbook.exists():
        playbook.write_text(f"---\ntype: jarvis-playbook\ncourse: {_course_title(key)}\n---\n\n# Приёмы {_course_title(key)}\n\n"
                            "Команды PowerShell и cmd из курса. Jarvis сверяется с ними перед работой.\n\n", encoding="utf-8")
    link = f"[[jarvis-knowledge/Знания/{_note_name(cur)}|{cur['title']}]]"
    with playbook.open("a", encoding="utf-8") as f:
        f.write(f"## {cur['id']} {cur['title']} ({cur['module']})\n\n{practices}\n\nИз блока: {link}\n\n")


async def _skill_when_module_done(tool: "StudyCourseTool", key: str, cur: dict[str, Any]) -> str:
    """A finished module of a practical course becomes a draft skill (the owner activates it)."""
    blob = _load()
    course = blob.get(key) or {}
    module = cur["module"]
    if not course.get("practical") or module in (course.get("skills") or {}):
        return ""
    if not all(b["id"] in course["done"] for b in course["blocks"] if b["module"] == module):
        return ""
    try:
        from jarvis.plugins.tool.skill_from_knowledge import make_skill  # noqa: PLC0415

        ok, message, info = await make_skill(f"{_course_title(key)}: {module}", getattr(tool, "_ctx", None),
                                             _course_title(key), module)
    except Exception as exc:  # noqa: BLE001 - the lesson is recorded either way
        logger.warning("module skill creation failed: %s", exc, exc_info=True)
        return ""
    blob = _load()
    blob[key].setdefault("skills", {})[module] = info.get("skill_name", "") if ok else "failed"
    _save(blob)
    return f"\nМодуль «{module}» пройден: " + (message if ok else "навык создать не удалось")


async def _study(tool: "StudyCourseTool", key: str, blob: dict[str, Any]) -> str:
    """Research, write, record one block; returns the report for the owner (deterministic)."""
    course = blob[key]
    cur = next((b for b in course["blocks"] if b["id"] not in course["done"]), None)
    if cur is None:
        return "Все блоки курса уже изучены, Сэр."
    if course.get("source"):
        return await _study_file(tool, key, blob, cur)
    idx = course["blocks"].index(cur)
    previous = course["blocks"][idx - 1]["title"] if idx > 0 else None
    practical = bool(course.get("practical"))
    title = _course_title(key)
    head = re.split(r"[:,]", cur["title"])[0].strip()
    queries = list(dict.fromkeys(
        [f"{head} Windows", f"{head} PowerShell команды", cur["title"], f"{head} cmd", head])) if practical else None
    materials, sources = await _gather(cur, queries, learn=practical)
    if len(materials) < 300:
        return (f"ERROR: Не нашёл материалов по теме «{cur['title']}» (нет сети или поиск недоступен). "
                "Блок не отмечен изученным.")
    summary = await asyncio.to_thread(_summarize, cur, materials, previous, practical, title)
    if len(summary) < 200:
        return "ERROR: Локальная модель не составила конспект. Блок не отмечен изученным."
    terms: list[str] = []
    m = re.search(r"Ключевые термины:\s*(.+)", summary)
    if m:
        terms = [t.strip(" .") for t in m.group(1).split(",") if t.strip(" .")]
        summary = summary[: m.start()].strip()
    practices = ""
    if practical:
        pm = re.search(r"Команды и приёмы:\s*(.*?)(?=\n\s*Опасно:|\Z)", summary, re.S)
        if pm and pm.group(1).strip().lower().strip(" .") != "нет":
            practices = pm.group(1).strip()
    done = tool._run({"action": "complete", "course": key, "block_id": cur["id"], "summary": summary,
                      "key_terms": terms, "source": ", ".join(sources[:2])})
    if done.startswith("ERROR:"):
        return done
    sentences = re.split(r"(?<=[.!?])\s+", summary)
    brief = " ".join(sentences[:3])
    if practices:
        await asyncio.to_thread(_write_playbook, key, cur, practices)
    fresh = _load()[key]
    module_note = await _skill_when_module_done(tool, key, cur) if practical else ""
    return (f"Сэр, блок {cur['id']} «{cur['title']}» изучен. Кратко: {brief}\n"
            f"Ключевые понятия: {', '.join(terms[:6]) or '—'}. Источники: {', '.join(urllib.parse.urlparse(s).netloc for s in sources[:2])}. "
            f"Записано в Obsidian: «{_note_name(cur)}». Изучено {len(fresh['done'])} из {len(fresh['blocks'])}.\n"
            + ("Команды записаны в справочник. " if practices else "")
            + ("Продолжить, Сэр?" if len(fresh["done"]) < len(fresh["blocks"]) else "Курс завершён.") + module_note)


def _course_runner(tool: "StudyCourseTool", key: str):
    """(step, after) for the continuous runner: one block per step, no questions in between."""

    async def step() -> tuple[bool, str, bool]:
        blob = _load()
        course = blob.get(key)
        if course is None or len(course["done"]) >= len(course["blocks"]):
            return True, "", True
        text = await _study(tool, key, blob)
        if text.startswith("ERROR:"):
            return False, text[6:].strip()[:160], False
        fresh = _load()[key]
        m = re.search(r"(?:блок|фрагмент) (\S+) «([^»]+)»", text.splitlines()[0] if text else "")
        line = f"{m.group(1)} «{m.group(2)[:50]}»" if m else (text.splitlines()[0][:80] if text else "")
        return True, line, len(fresh["done"]) >= len(fresh["blocks"])

    def after(reason: str) -> str:
        blob = _load()
        course = blob.get(key)
        if course is None:
            return ""
        done, total = len(course["done"]), len(course["blocks"])
        if done >= total:
            course["awaiting"] = None
            _save(blob)
            return f"Курс «{_course_title(key)}» изучен полностью ({done} из {total})."
        course["awaiting"] = datetime.now().timestamp()
        course["state"] = "active"
        _save(blob)
        return f"Изучено {done} из {total}. Продолжить, Сэр?"

    return step, after


class StudyCourseTool:
    name: str = "study_course"
    risk_tier: str = "monitor"
    description: str = (
        "Step-by-step self-study course (default: Computer Science, ~60 blocks) and study from the owner's own files "
        "(txt/md/pdf/html in data/courses_inbox: ingest, or study with path). action: study (starts the course if "
        "needed and studies block after block IN THE BACKGROUND without asking in between; tell the owner it runs and "
        "that 'стоп' stops it), stop (halts after the current block and returns the report, then ask 'Продолжить, Сэр?'), "
        "start (course), "
        "status, next (returns the next block to study: study ONLY that block via search_web), complete "
        "(block_id, summary: your own 4-8 sentence explanation, key_terms, source: url; saves a linked Obsidian "
        "note), pause, resume, list. After complete you MUST report the result to the owner and ask whether to "
        "continue; call next again only after the owner agrees."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["study", "stop", "ingest", "start", "status", "next", "complete", "pause", "resume", "list"]},
            "course": {"type": "string", "description": "Course name; default Computer Science."},
            "path": {"type": "string", "description": "study/ingest: a file in data/courses_inbox (name) or a full path."},
            "block_id": {"type": "string"},
            "summary": {"type": "string"},
            "key_terms": {"type": "array", "items": {"type": "string"}},
            "source": {"type": "string"},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:  # noqa: ARG002
        return "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:  # noqa: ARG002
        try:
            action = str(args.get("action") or "")
            self._ctx = ctx
            if action == "ingest":
                return ToolResult(success=True, output=await asyncio.to_thread(self._ingest, str(args.get("path") or "")))
            if action == "study":
                blob = _load()
                path_arg = str(args.get("path") or "").strip()
                if path_arg:
                    found = _find_file(path_arg)
                    if found is None:
                        names = ", ".join(p.name for p in _inbox_files()[:12]) or "папка пуста"
                        return ToolResult(success=False, output=None,
                                          error=f"Файл «{path_arg}» не найден. В папке data/courses_inbox: {names}.")
                    key = await asyncio.to_thread(_ingest_file, found, blob)
                    blob = _load()
                else:
                    key = _pick_key(str(args.get("course") or ""), blob)
                if key not in blob or blob[key].get("state") == "paused":
                    await asyncio.to_thread(self._run, {"action": "start" if key not in blob else "resume", "course": key})
                    blob = _load()
                from jarvis.plugins.tool import study_runner  # noqa: PLC0415

                course = blob[key]
                cur = next((b for b in course["blocks"] if b["id"] not in course["done"]), None)
                if cur is None:
                    return ToolResult(success=True, output=f"Курс «{_course_title(key)}» уже изучен полностью, Сэр.")
                step, after = _course_runner(self, key)
                msg = await study_runner.start(f"курс «{_course_title(key)}»", step, after)
                return ToolResult(success=True, output=f"Сэр, {msg} Начинаю с блока {cur['id']} «{cur['title']}»; "
                                  f"осталось блоков: {len(course['blocks']) - len(course['done'])}.")
            if action == "stop":
                from jarvis.plugins.tool import study_runner  # noqa: PLC0415

                return ToolResult(success=True, output=await study_runner.stop())
            text = await asyncio.to_thread(self._run, args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("study_course failed", exc_info=True)
            return ToolResult(success=False, output=None, error=f"study_course: {exc}")
        if text.startswith("ERROR:"):
            return ToolResult(success=False, output=None, error=text[6:].strip())
        return ToolResult(success=True, output=text)

    def _ingest(self, path: str) -> str:
        blob = _load()
        targets = [_find_file(path)] if path.strip() else _inbox_files()
        targets = [t for t in targets if t is not None]
        if not targets:
            return ("Файлов для изучения нет. Положите текстовые файлы (txt, md, pdf, html) в папку data/courses_inbox "
                    "и скажите «изучи файл <имя>».")
        lines = []
        for t in targets:
            key = _ingest_file(t, blob)
            blob = _load()
            c = blob[key]
            lines.append(f"- {c['title']}: {len(c['blocks'])} блоков, изучено {len(c['done'])}")
        return "Загружено в обучение:\n" + "\n".join(lines) + "\nСкажите «изучи файл <имя>» или «продолжи курс»."

    def _run(self, args: dict[str, Any]) -> str:  # noqa: C901 - flat dispatch
        action = str(args.get("action") or "")
        key = _slug(str(args.get("course") or _DEFAULT))
        blob = _load()
        if action == "list":
            lines = [f"- {_course_title(k)}: {len(c['done'])}/{len(c['blocks'])}" for k, c in blob.items()]
            return "Курсы:\n" + ("\n".join(lines) if lines else "пока ни одного. Доступен: Computer Science.")
        if action == "start":
            if key not in SYLLABI:
                return f"ERROR: Нет готовой программы «{key}». Есть: {', '.join(SYLLABI)}."
            if key in blob and blob[key]["done"]:
                blob[key]["state"] = "active"
                _save(blob)
                return "Курс уже начат, продолжаем. " + _status(blob[key], key)
            blob[key] = {"state": "active", "started": datetime.now().strftime("%Y-%m-%d"),
                         "blocks": _flat(SYLLABI[key]), "done": {}, "practical": key in PRACTICAL}
            _save(blob)
            _write_hub(key, blob[key])
            first = self._run({"action": "next", "course": key})
            return (f"Курс «{_course_title(key)}» начат: {len(blob[key]['blocks'])} блоков в "
                    f"{len(SYLLABI[key])} модулях. Заметка-оглавление создана в Obsidian. "
                    "Не останавливайся на этом: сразу изучай первый блок.\n" + first)
        course = blob.get(key)
        if course is None:
            return f"ERROR: Курс «{key}» не начат. Вызови start."
        if action == "status":
            from jarvis.plugins.tool import study_runner  # noqa: PLC0415

            return (study_runner.status_text() + " " if study_runner.is_running() else "") + _status(course, key)
        if action == "pause":
            course["state"] = "paused"
            _save(blob)
            return "Курс поставлен на паузу. " + _status(course, key)
        if action == "resume":
            course["state"] = "active"
            _save(blob)
            return "Курс продолжен. " + _status(course, key)
        if action == "next":
            cur = next((b for b in course["blocks"] if b["id"] not in course["done"]), None)
            if cur is None:
                course["state"] = "finished"
                _save(blob)
                return "Все блоки курса изучены. Расскажи Сэру итог и предложи повторить слабые темы."
            course["state"] = "active"
            course.pop("awaiting", None)
            _save(blob)
            idx = course["blocks"].index(cur)
            prev = course["blocks"][idx - 1] if idx > 0 else None
            return (
                f"СЛЕДУЮЩИЙ БЛОК {cur['id']}: «{cur['title']}» (модуль «{cur['module']}», "
                f"{len(course['done'])} из {len(course['blocks'])} изучено).\n"
                "Что сделать сейчас, и только это:\n"
                f"1. Вызови search_web 2-3 раза по теме блока (например: «{cur['title']} объяснение», "
                f"«{cur['title']} computer science tutorial», «{cur['title']} примеры»). Читай найденное, не пересказывай по памяти.\n"
                "2. Составь собственное связное объяснение: суть, ключевые понятия, как работает, пример, "
                "типичные ошибки и как это связано с уже изученным"
                + (f" (предыдущий блок: «{prev['title']}»)" if prev else "") + ".\n"
                f"3. Вызови study_course complete с block_id=\"{cur['id']}\", summary (4-8 предложений), key_terms и source.\n"
                "4. Ответь Сэру: что изучил (2-4 предложения), что записал в Obsidian, и спроси «Продолжить, Сэр?». "
                "НЕ начинай следующий блок без ответа."
            )
        if action == "complete":
            block_id = str(args.get("block_id") or "").strip()
            summary = str(args.get("summary") or "").strip()
            block = next((b for b in course["blocks"] if b["id"] == block_id), None)
            if block is None:
                return f"ERROR: Нет блока «{block_id}». Спроси next."
            if len(summary) < 80:
                return "ERROR: Слишком короткое резюме: напиши собственное объяснение блока (4-8 предложений)."
            from jarvis.plugins.tool.memory_notes import _learn  # noqa: PLC0415

            idx = course["blocks"].index(block)
            related = [_hub_name(key)]
            if idx > 0:
                related.append(_note_name(course["blocks"][idx - 1]))
            terms = [str(t) for t in (args.get("key_terms") or []) if str(t).strip()]
            text = summary + (("\n\nКлючевые понятия: " + ", ".join(terms)) if terms else "")
            text += f"\n\nМодуль: {block['module']}."
            _learn(_note_name(block), text, str(args.get("source") or ""), related,
                   {"course": _course_title(key), "module": block["module"], "block": block["id"]})
            course["done"][block_id] = datetime.now().strftime("%Y-%m-%d %H:%M")
            nxt = next((b for b in course["blocks"] if b["id"] not in course["done"]), None)
            course["state"] = "active" if nxt else "finished"
            course["awaiting"] = datetime.now().timestamp() if nxt else None
            _save(blob)
            _write_hub(key, course)
            tail = (f"Следующий блок: {nxt['id']} «{nxt['title']}». " if nxt else "Курс завершён. ")
            return (f"Блок {block_id} записан в Obsidian. Изучено {len(course['done'])} из {len(course['blocks'])}. {tail}"
                    "Сообщи Сэру результат и спроси, продолжать ли. Следующий блок не начинай без его согласия.")
        return f"ERROR: unknown action {action!r}"
