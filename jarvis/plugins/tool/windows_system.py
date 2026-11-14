"""windows_system: the built-in Windows tools as catalog programs.

Fork addition (2026-11-14). Task Manager, Registry Editor, Services, Event Viewer,
cmd, PowerShell and the other system consoles are not in the Start Menu catalog
the way installed programs are, yet they are what the Windows course is about.
This list makes them first-class catalog entries (kind ``sys``): they open by
their Russian names, get their own Obsidian notes (with the command that starts
them from cmd and PowerShell) and are linked from the knowledge written about them.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

#: (display name, launch target, spoken aliases, description, command line)
SYSTEM_TOOLS: tuple[tuple[str, str, tuple[str, ...], str, str], ...] = (
    ("Командная строка", "cmd.exe", ("командная строка", "cmd", "сиэмди", "консоль"),
     "Классическая консоль Windows: команды cmd, bat-файлы, сетевые утилиты (ipconfig, ping, netstat).", "cmd"),
    ("Windows PowerShell", "powershell.exe", ("павершелл", "пауэршелл", "повершелл", "powershell"),
     "Оболочка и язык сценариев на объектах: командлеты вида Глагол-Существительное, конвейер, модули, удалённое управление.",
     "powershell"),
    ("Терминал Windows", "wt.exe", ("терминал", "виндовс терминал", "windows terminal"),
     "Современное окно для cmd, PowerShell и WSL с вкладками и панелями.", "wt"),
    ("Диспетчер задач", "taskmgr.exe", ("диспетчер задач", "таск менеджер", "задачи"),
     "Процессы, службы, автозагрузка, нагрузка на CPU, память, диск и сеть.", "taskmgr"),
    ("Редактор реестра", "regedit.exe", ("редактор реестра", "реестр", "regedit"),
     "Просмотр и правка реестра Windows. Перед правкой делайте резервную копию ветки.", "regedit"),
    ("Службы", "services.msc", ("службы", "сервисы", "services"),
     "Список служб Windows: запуск, остановка, тип запуска.", "services.msc"),
    ("Просмотр событий", "eventvwr.msc", ("просмотр событий", "журнал событий", "event viewer"),
     "Журналы системы, приложений и безопасности: ошибки, предупреждения, аудит.", "eventvwr.msc"),
    ("Монитор ресурсов", "resmon.exe", ("монитор ресурсов", "resmon"),
     "Подробная нагрузка по процессам: CPU, память, диск, сеть, открытые порты.", "resmon"),
    ("Системный монитор", "perfmon.exe", ("системный монитор", "монитор производительности", "perfmon"),
     "Счётчики производительности, графики, наборы сборщиков данных.", "perfmon"),
    ("Конфигурация системы", "msconfig.exe", ("конфигурация системы", "msconfig"),
     "Параметры загрузки, службы, безопасный режим.", "msconfig"),
    ("Сведения о системе", "msinfo32.exe", ("сведения о системе", "msinfo32"),
     "Полная сводка по железу, драйверам и окружению.", "msinfo32"),
    ("Управление дисками", "diskmgmt.msc", ("управление дисками", "диски"),
     "Разделы, тома, буквы дисков, подключение VHD.", "diskmgmt.msc"),
    ("Диспетчер устройств", "devmgmt.msc", ("диспетчер устройств", "устройства"),
     "Устройства и драйверы: обновление, отключение, откат.", "devmgmt.msc"),
    ("Управление компьютером", "compmgmt.msc", ("управление компьютером", "compmgmt"),
     "Единая консоль: события, общие папки, пользователи, диски, службы.", "compmgmt.msc"),
    ("Планировщик заданий", "taskschd.msc", ("планировщик заданий", "планировщик", "taskschd"),
     "Запуск программ и скриптов по расписанию и по событиям.", "taskschd.msc"),
    ("Брандмауэр Защитника Windows", "wf.msc", ("брандмауэр", "файрвол", "firewall"),
     "Правила входящих и исходящих подключений.", "wf.msc"),
    ("Локальные пользователи и группы", "lusrmgr.msc", ("локальные пользователи", "пользователи и группы"),
     "Учётные записи и группы компьютера (в Windows Home недоступно).", "lusrmgr.msc"),
    ("Редактор локальной групповой политики", "gpedit.msc", ("групповая политика", "редактор групповой политики", "gpedit"),
     "Политики системы и пользователя (в Windows Home недоступно).", "gpedit.msc"),
    ("Сетевые подключения", "ncpa.cpl", ("сетевые подключения", "сетевые адаптеры", "ncpa"),
     "Сетевые адаптеры: IP, DNS, включение и отключение.", "ncpa.cpl"),
    ("Панель управления", "control.exe", ("панель управления", "control panel"),
     "Классическая панель настроек системы.", "control"),
    ("Очистка диска", "cleanmgr.exe", ("очистка диска", "cleanmgr"),
     "Удаление временных файлов и мусора с диска.", "cleanmgr"),
    ("Удалённый рабочий стол", "mstsc.exe", ("удалённый рабочий стол", "удаленный рабочий стол", "rdp", "mstsc"),
     "Подключение к другому компьютеру по RDP.", "mstsc"),
    ("Средство диагностики DirectX", "dxdiag.exe", ("dxdiag", "диагностика directx"),
     "Сведения о видеокарте, звуке и DirectX.", "dxdiag"),
    ("Проводник", "explorer.exe", ("проводник", "эксплорер", "explorer"),
     "Файловый менеджер и оболочка рабочего стола.", "explorer"),
    ("Калькулятор", "calc.exe", ("калькулятор", "calc"),
     "Обычный, инженерный и программистский режимы.", "calc"),
    ("Блокнот", "notepad.exe", ("блокнот", "notepad"), "Простой текстовый редактор.", "notepad"),
    ("Paint", "mspaint.exe", ("пейнт", "paint", "рисование"), "Простой графический редактор.", "mspaint"),
    ("Ножницы", "snippingtool.exe", ("ножницы", "snipping tool"), "Скриншоты области и окон.", "snippingtool"),
    ("Параметры Windows", "ms-settings:", ("параметры", "настройки windows", "настройки"),
     "Современные настройки: сеть, звук, обновления, приложения, конфиденциальность.", "start ms-settings:"),
)


def _resolve(target: str) -> str:
    """Absolute path of a system tool when it exists on this PC, else the bare target (shell resolves it)."""
    if target.endswith(":"):
        return target
    found = shutil.which(target)
    if found:
        return found
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for sub in ("System32", ""):
        path = os.path.join(windir, sub, target)
        if os.path.isfile(path):
            return path
    return target


def catalog_entries() -> list[dict[str, Any]]:
    """Catalog rows (kind ``sys``) for the system tools that exist on this PC."""
    rows: list[dict[str, Any]] = []
    for name, target, _aliases, desc, command in SYSTEM_TOOLS:
        path = _resolve(target)
        if not target.endswith(":") and path == target and not target.endswith((".msc", ".cpl")):
            continue  # not present on this edition of Windows
        if target.endswith((".msc", ".cpl")) and path == target:
            windir = os.environ.get("WINDIR", r"C:\Windows")
            if not os.path.isfile(os.path.join(windir, "System32", target)):
                continue
        rows.append({"n": name, "k": "sys", "p": path, "e": path if os.path.isfile(path) else "", "d": desc, "c": command})
    return rows


def spoken_aliases() -> dict[str, str]:
    """Spoken name -> catalog name, for every system tool."""
    out: dict[str, str] = {}
    for name, _target, aliases, _desc, _command in SYSTEM_TOOLS:
        out[name.lower()] = name.lower()
        for alias in aliases:
            out[alias] = name.lower()
    return out
