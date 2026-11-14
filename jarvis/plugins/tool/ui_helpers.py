"""ui_helpers: small Windows UI-driving primitives shared by the app-control tools.

Fork addition (2026-11-14). Used by ``spotify_control``, ``happ_vpn`` and
``steam_control`` where an app offers no API. Everything here is best-effort and
never raises into a tool: callers get ``None`` / empty results and report
honestly. Building blocks:

* window lookup by process, focus + restore of the previous window;
* screen capture of a rectangle (``mss``);
* OCR through the Windows built-in engine (``Windows.Media.Ocr``, via Windows
  PowerShell 5.1, no download), with optional upscaling for small UI text;
* UI Automation (``System.Windows.Automation``) element listing / invoke;
* synthetic mouse clicks, wheel and key chords.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from jarvis.platform import window_state

logger = logging.getLogger(__name__)

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
user32 = ctypes.windll.user32  # type: ignore[attr-defined]

try:  # physical-pixel coordinates for SetCursorPos / mss / UIA agree
    user32.SetProcessDPIAware()
except Exception:  # noqa: BLE001
    pass


# ----------------------------------------------------------------------
# PowerShell
# ----------------------------------------------------------------------
def run_ps(script: str, timeout: int = 60) -> str:
    """Run ``script`` in Windows PowerShell 5.1 and return UTF-8 stdout."""
    prelude = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8\n$ErrorActionPreference='Stop'\n"
               "$ProgressPreference='SilentlyContinue'\n")
    encoded = base64.b64encode((prelude + script).encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(  # noqa: S603 — fixed argv; the script is built in this package
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True, timeout=timeout, check=False, creationflags=_NOWIN,
    )
    out = (proc.stdout or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")
        if "CLIXML" in err:  # PowerShell wraps errors in CLIXML when stderr is redirected
            import re

            parts = re.findall(r'<S S="Error">(.*?)</S>', err, flags=re.S)
            err = " ".join(parts).replace("_x000D__x000A_", " ")
        raise RuntimeError((err.strip() or out.strip())[:400])
    return out


# ----------------------------------------------------------------------
# Windows and focus
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Win:
    handle: int
    title: str
    pid: int
    minimized: bool


def find_window(process: str, title_like: str = "") -> Win | None:
    """First visible titled window of ``process`` (e.g. ``"Happ"``), or None."""
    import psutil

    wanted = process.lower() + ".exe"
    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == wanted}
    like = title_like.lower()
    for w in window_state.list_windows():
        if w.pid in pids and w.handle and (w.title or "").strip() and like in (w.title or "").lower():
            return Win(int(w.handle), w.title, int(w.pid), bool(w.minimized))
    return None


def foreground() -> int:
    return int(user32.GetForegroundWindow() or 0)


def window_rect(handle: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.GetWindowRect(int(handle), ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def focus(win: Win, settle: float = 0.8) -> bool:
    """Restore (if minimized) and bring ``win`` to the foreground; True when it is active."""
    if win.minimized or user32.IsIconic(win.handle):
        user32.ShowWindow(win.handle, 9)
    window_state._force_foreground_windows(win.handle)  # noqa: SLF001 — hardened foreground raise
    time.sleep(settle)
    return foreground() == win.handle


def restore_focus(handle: int) -> None:
    if handle and handle != foreground():
        window_state._force_foreground_windows(int(handle))  # noqa: SLF001


# ----------------------------------------------------------------------
# Screen
# ----------------------------------------------------------------------
def capture(left: int, top: int, width: int, height: int):  # -> PIL.Image.Image
    import mss
    from PIL import Image

    with mss.MSS() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
        return Image.frombytes("RGB", shot.size, shot.rgb)


def green_pixels(img, box: tuple[int, int, int, int]) -> int:
    """Count Spotify-green pixels (#1ED760-ish) inside ``box`` of ``img``."""
    crop = img.crop(box).convert("RGB")
    count = 0
    for r, g, b in crop.getdata():
        if g > 150 and r < 90 and b < 130 and g - r > 80:
            count += 1
    return count


# ----------------------------------------------------------------------
# OCR (Windows.Media.Ocr through PowerShell)
# ----------------------------------------------------------------------
_OCR_PS = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.RandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language('__LANG__')))
if (-not $engine) { throw 'no OCR engine for __LANG__' }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('__PATH__')) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) { foreach ($word in $line.Words) {
    $r = $word.BoundingRect
    Write-Output ("{0}`t{1}`t{2}`t{3}`t{4}" -f [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height, $word.Text) } }
"""


@dataclass(frozen=True)
class Word:
    x: int
    y: int
    w: int
    h: int
    text: str

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


def ocr(img, lang: str = "ru", scale: int = 1, invert: bool = False) -> list[Word]:
    """OCR a PIL image. Coordinates are returned in the ORIGINAL image's pixels."""
    from PIL import ImageOps

    work = img.convert("L")
    if scale > 1:
        from PIL import Image

        work = work.resize((work.width * scale, work.height * scale), Image.LANCZOS)
        work = ImageOps.autocontrast(work)
    if invert:
        work = ImageOps.invert(work)
    fd, path = tempfile.mkstemp(suffix=".png", prefix="jarvis_ocr_")
    os.close(fd)
    try:
        work.save(path)
        out = run_ps(_OCR_PS.replace("__LANG__", lang).replace("__PATH__", path.replace("'", "''")), timeout=60)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    words: list[Word] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 5 and all(p.lstrip("-").isdigit() for p in parts[:4]):
            x, y, w, h = (int(p) // scale for p in parts[:4])
            words.append(Word(x, y, max(1, w), max(1, h), parts[4]))
    return words


# ----------------------------------------------------------------------
# UI Automation
# ----------------------------------------------------------------------
_UIA_LIST_PS = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$pids = @(Get-Process -Name '__PROC__' -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$win = $null
foreach ($w in $AE::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
    try { if ($pids -contains $w.Current.ProcessId -and $w.Current.Name -like '*__TITLE__*') { $win = $w; break } } catch {}
}
if (-not $win) { Write-Output '[]'; exit 0 }
$rows = @()
foreach ($el in $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
    $c = $el.Current
    $r = $c.BoundingRectangle
    if ($r.IsEmpty) { continue }
    $rows += [pscustomobject]@{
        type = ($c.ControlType.ProgrammaticName -replace 'ControlType\.', '')
        name = $c.Name
        id = ($c.AutomationId -replace '^.*ApplicationWindowContentControl\.?', '')
        left = [int]$r.Left; top = [int]$r.Top; width = [int]$r.Width; height = [int]$r.Height
    }
}
if ($rows.Count -eq 0) { Write-Output '[]' } else { ConvertTo-Json -InputObject @($rows) -Compress }
"""

_UIA_INVOKE_PS = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$pids = @(Get-Process -Name '__PROC__' -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$win = $null
foreach ($w in $AE::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
    try { if ($pids -contains $w.Current.ProcessId -and $w.Current.Name -like '*__TITLE__*') { $win = $w; break } } catch {}
}
if (-not $win) { Write-Output 'NOWINDOW'; exit 0 }
foreach ($el in $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
    $r = $el.Current.BoundingRectangle
    if ([int]$r.Left -eq __LEFT__ -and [int]$r.Top -eq __TOP__) {
        $pattern = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $pattern.Invoke()
        Write-Output 'INVOKED'
        exit 0
    }
}
Write-Output 'NOTFOUND'
"""

_UIA_STATE_PS = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$pids = @(Get-Process -Name '__PROC__' -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
foreach ($w in $AE::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
    try {
        if ($pids -contains $w.Current.ProcessId -and $w.Current.Name -like '*__TITLE__*') {
            $wp = $w.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern)
            $wp.SetWindowVisualState([System.Windows.Automation.WindowVisualState]::__STATE__)
            Write-Output 'OK'; exit 0
        }
    } catch {}
}
Write-Output 'NOWINDOW'
"""


def uia_elements(process: str, title_like: str = "") -> list[dict[str, Any]]:
    """Visible UIA elements of the process' main window (type, name, id, rect)."""
    script = _UIA_LIST_PS.replace("__PROC__", process).replace("__TITLE__", title_like.replace("'", "''"))
    text = run_ps(script, timeout=90).strip()
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def uia_invoke(process: str, left: int, top: int, title_like: str = "") -> bool:
    """Invoke the element whose rect starts at (left, top); True when it was invoked."""
    script = (_UIA_INVOKE_PS.replace("__PROC__", process).replace("__TITLE__", title_like.replace("'", "''"))
              .replace("__LEFT__", str(int(left))).replace("__TOP__", str(int(top))))
    return run_ps(script, timeout=60).strip().endswith("INVOKED")


def uia_window_state(process: str, state: str, title_like: str = "") -> bool:
    """state: Maximized | Normal | Minimized (WindowPattern)."""
    script = _UIA_STATE_PS.replace("__PROC__", process).replace("__TITLE__", title_like.replace("'", "''")).replace("__STATE__", state)
    return run_ps(script, timeout=30).strip().endswith("OK")


# ----------------------------------------------------------------------
# Input
# ----------------------------------------------------------------------
def click(x: int, y: int, restore_cursor: bool = True) -> None:
    prev = wt.POINT()
    user32.GetCursorPos(ctypes.byref(prev))
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
    time.sleep(0.05)
    if restore_cursor:
        user32.SetCursorPos(prev.x, prev.y)


def right_click(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(0x0008, 0, 0, 0, 0)  # right down
    user32.mouse_event(0x0010, 0, 0, 0, 0)  # right up


def chord(*vks: int) -> None:
    """Press virtual keys together (modifiers first) and release in reverse."""
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.02)


VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28,
      "b": 0x42, "r": 0x52, "s": 0x53, "esc": 0x1B, "enter": 0x0D, "tab": 0x09, "space": 0x20}
