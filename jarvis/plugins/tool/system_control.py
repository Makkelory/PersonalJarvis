"""system_control: master volume and a real computer-status report.

Fork addition (2026-11-14), next to ``system_apps``:

* ``set_volume`` — read or change the Windows master volume / mute through the
  Core Audio ``IAudioEndpointVolume`` interface (inline C# via Windows
  PowerShell 5.1, so no extra package is installed).
* ``system_status`` — CPU, RAM, disks, uptime, heaviest processes and the
  NVIDIA GPU (via ``nvidia-smi`` when present). Only measured values are
  reported; CPU temperature has no portable source on Windows and is said so
  instead of being guessed.

Windows only; elsewhere both tools answer honestly that they are unavailable.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import subprocess
import time
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.platform import detect_platform

logger = logging.getLogger(__name__)

_AUDIO_CS = r"""
using System;
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
  int f1(); int f2(); int f3(); int f4();
  int SetMasterVolumeLevelScalar(float fLevel, Guid pguidEventContext);
  int f6();
  int GetMasterVolumeLevelScalar(out float pfLevel);
  int f8(); int f9(); int f10(); int f11();
  int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, Guid pguidEventContext);
  int GetMute(out bool pbMute);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { int Activate(ref Guid id, int clsCtx, int activationParams, out IAudioEndpointVolume aev); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int f(); int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject { }
public class Audio {
  static IAudioEndpointVolume Vol() {
    var enumerator = new MMDeviceEnumeratorComObject() as IMMDeviceEnumerator;
    IMMDevice dev = null;
    Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(0, 1, out dev));
    IAudioEndpointVolume epv = null;
    var epvid = typeof(IAudioEndpointVolume).GUID;
    Marshal.ThrowExceptionForHR(dev.Activate(ref epvid, 23, 0, out epv));
    return epv;
  }
  public static float Volume {
    get { float v = -1; Marshal.ThrowExceptionForHR(Vol().GetMasterVolumeLevelScalar(out v)); return v; }
    set { Marshal.ThrowExceptionForHR(Vol().SetMasterVolumeLevelScalar(value, Guid.Empty)); }
  }
  public static bool Mute {
    get { bool m = false; Marshal.ThrowExceptionForHR(Vol().GetMute(out m)); return m; }
    set { Marshal.ThrowExceptionForHR(Vol().SetMute(value, Guid.Empty)); }
  }
}
"""


def _run_audio(script_body: str) -> tuple[int | None, bool | None]:
    """Run ``script_body`` (PowerShell using [Audio]) and return (percent, muted)."""
    script = (
        "$ErrorActionPreference='Stop'\n"
        f"Add-Type -TypeDefinition @'\n{_AUDIO_CS}\n'@\n"
        f"{script_body}\n"
        "$v=[int][math]::Round([Audio]::Volume*100); $m=[Audio]::Mute\n"
        "Write-Output \"VOL=$v MUTE=$m\"\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(  # noqa: S603 — fixed argv, script is built in this module
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=30, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = (proc.stdout or "").strip().splitlines()
    for line in reversed(out):
        if line.startswith("VOL="):
            parts = dict(p.split("=") for p in line.split())
            return int(parts["VOL"]), parts["MUTE"].lower() == "true"
    raise RuntimeError((proc.stderr or proc.stdout or "no output").strip()[:300])


class SetVolumeTool:
    name: str = "set_volume"
    risk_tier: str = "monitor"
    description: str = (
        "Reads or changes the Windows master volume and mute. action: get, set "
        "(needs level 0-100), up, down (optional step, default 10), mute, "
        "unmute, toggle_mute. Always call it to change or report the volume."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "set", "up", "down", "mute", "unmute", "toggle_mute"],
                "default": "get",
            },
            "level": {"type": "integer", "minimum": 0, "maximum": 100,
                      "description": "Target volume percent for action=set"},
            "step": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
        },
    }

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="set_volume supports Windows only")
        action = str(args.get("action") or "get").lower()
        try:
            step = max(1, min(100, int(args.get("step") or 10)))
            level = args.get("level")
            if action == "set":
                if level is None:
                    return ToolResult(success=False, output=None, error="level (0-100) is required for set")
                body = f"[Audio]::Volume = {max(0, min(100, int(level)))}/100.0; [Audio]::Mute = $false"
            elif action == "up":
                body = f"[Audio]::Volume = [math]::Min(1.0, [Audio]::Volume + {step}/100.0); [Audio]::Mute = $false"
            elif action == "down":
                body = f"[Audio]::Volume = [math]::Max(0.0, [Audio]::Volume - {step}/100.0)"
            elif action == "mute":
                body = "[Audio]::Mute = $true"
            elif action == "unmute":
                body = "[Audio]::Mute = $false"
            elif action == "toggle_mute":
                body = "[Audio]::Mute = -not [Audio]::Mute"
            elif action == "get":
                body = ""
            else:
                return ToolResult(success=False, output=None, error=f"unknown action {action!r}")
            logger.info("[SET_VOLUME] action=%s level=%s step=%s trace=%s", action, level, step, ctx.trace_id)
            percent, muted = await asyncio.to_thread(_run_audio, body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SET_VOLUME] failed", exc_info=True)
            return ToolResult(success=False, output=None, error=f"Громкость не изменена: {type(exc).__name__}: {exc}")
        state = "звук выключен" if muted else "звук включён"
        return ToolResult(success=True, output=f"Громкость {percent}%, {state}.")


def _fmt_uptime(seconds: float) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return (f"{d} д " if d else "") + f"{h} ч {m} мин"


def _gpu_line() -> str:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return "Видеокарта: nvidia-smi не найден, данные недоступны."
    try:
        proc = subprocess.run(  # noqa: S603
            [exe, "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        name, temp, util, used, total, power = [s.strip() for s in proc.stdout.strip().splitlines()[0].split(",")]
        return (f"Видеокарта {name}: {util}% загрузка, {temp} °C, память {int(float(used))}/"
                f"{int(float(total))} МБ, {float(power):.0f} Вт.")
    except Exception:  # noqa: BLE001
        return "Видеокарта: не удалось прочитать данные nvidia-smi."


def _status_report() -> str:
    import psutil

    procs = list(psutil.process_iter(["name"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except (psutil.Error, OSError):
            pass
    cpu_total = psutil.cpu_percent(interval=0.7)
    cores = psutil.cpu_count(logical=True) or 1
    rows: list[tuple[float, float, str]] = []
    for p in procs:
        try:
            if (p.info["name"] or "") == "System Idle Process":
                continue
            rows.append((p.cpu_percent(None) / cores, p.memory_info().rss / 1048576, p.info["name"] or "?"))
        except (psutil.Error, OSError):
            continue
    vm = psutil.virtual_memory()
    lines = [
        f"Процессор: {cpu_total:.0f}% загрузка ({cores} потоков).",
        f"Память: занято {vm.used / 2**30:.1f} из {vm.total / 2**30:.1f} ГБ ({vm.percent:.0f}%).",
        _gpu_line(),
    ]
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append(f"{part.mountpoint.rstrip(chr(92))} свободно {u.free / 2**30:.0f} из {u.total / 2**30:.0f} ГБ")
        except OSError:
            continue
    if disks:
        lines.append("Диски: " + "; ".join(disks) + ".")
    lines.append(f"Работает без перезагрузки: {_fmt_uptime(time.time() - psutil.boot_time())}.")
    top_cpu = sorted(rows, key=lambda r: -r[0])[:3]
    top_mem = sorted(rows, key=lambda r: -r[1])[:3]
    lines.append("Больше всего процессора: " + ", ".join(f"{n} {c:.0f}%" for c, _m, n in top_cpu) + ".")
    lines.append("Больше всего памяти: " + ", ".join(f"{n} {m:.0f} МБ" for _c, m, n in top_mem) + ".")
    lines.append("Температура процессора: нет доступного датчика, не угадываю.")
    return "\n".join(lines)


class SystemStatusTool:
    name: str = "system_status"
    risk_tier: str = "safe"
    description: str = (
        "Reports the REAL current state of this computer: CPU load, RAM, disk "
        "free space, GPU load/temperature/memory, uptime and the heaviest "
        "processes. Call it before answering any question about the PC's state."
    )
    schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="system_status supports Windows only")
        logger.info("[SYSTEM_STATUS] trace=%s", ctx.trace_id)
        try:
            return ToolResult(success=True, output=await asyncio.to_thread(_status_report))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SYSTEM_STATUS] failed", exc_info=True)
            return ToolResult(success=False, output=None, error=f"{type(exc).__name__}: {exc}")


