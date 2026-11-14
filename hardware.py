"""Read this computer's memory and graphics card, then suggest a size limit.

Everything here is read-only and stays on this computer. Nothing is sent
anywhere. Each check has a short timeout and quietly gives up if a tool is
missing, so detection never stops the app from starting.

Sources, best first:
- NVIDIA: nvidia-smi (Windows, Linux) when it is installed with the driver.
- Windows: the display adapter's HardwareInformation.qwMemorySize registry
  value (the 64-bit one; the older 32-bit values and Win32_VideoController
  AdapterRAM stop at 4 GB). PowerShell's Get-CimInstance is a last resort.
- macOS: sysctl (hw.memsize, Apple Silicon flag, chip name), and
  system_profiler for the graphics card on Intel Macs.
- Linux: /proc/meminfo, nvidia-smi, and the amdgpu sysfs files (rocm-smi
  if it is installed).
"""
from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

GIB = 1024 ** 3
MIB = 1024 ** 2
COMMAND_TIMEOUT = 6.0
SLOW_COMMAND_TIMEOUT = 12.0

# Ratios that match the built-in presets (16 GB card -> 13 GB, 16 GB Mac -> 10 GB).
GPU_RATIO = 0.8       # leave room on the graphics card for context and the screen
APPLE_RATIO = 0.625   # macOS and your apps share the same memory
CPU_RATIO = 0.4       # no usable graphics card: stay well inside system memory
MIN_CAP_GB = 1.0
MIN_DEDICATED_GB = 3.0

COMMON_SIZES_GB = (2, 3, 4, 6, 8, 10, 11, 12, 16, 18, 20, 24, 32, 36, 40, 48,
                   64, 80, 96, 128, 192, 256, 384, 512)

_VIRTUAL_RE = re.compile(
    r"microsoft basic|basic display|basic render|remote display|remotefx|hyper-v|"
    r"virtual|vmware|virtualbox|vbox|parallels|qxl|cirrus|bochs|parsec|citrix|"
    r"spacedesk|displaylink|indirect display|\bidd\b|\brdp\b|mirror|meta quest|oculus|"
    r"llvmpipe|software",
    re.I,
)
_INTEGRATED_AMD_RE = re.compile(
    r"radeon\(tm\) graphics|radeon graphics|vega \d+ graphics|radeon\(tm\) [0-9]{3}m|"
    r"\b(610|660|680|740|760|780|860|880|890)m\b",
    re.I,
)


# --- small helpers that tests replace ---------------------------------------

def _run(args: list[str], timeout: float = COMMAND_TIMEOUT) -> str:
    """Run a command and return its output, or "" if it is missing or slow."""
    exe = args[0]
    if not (os.path.isabs(exe) and os.path.exists(exe)) and shutil.which(exe) is None:
        return ""
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console flash
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if proc.returncode != 0:
        return ""
    try:
        return proc.stdout.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _system() -> str:
    return platform.system()


# --- number formatting --------------------------------------------------------

def nominal_gb(value_gb: float | None) -> float | None:
    """Snap a measured size to the size printed on the box (31.7 -> 32)."""
    if value_gb is None:
        return None
    try:
        value = float(value_gb)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    for size in COMMON_SIZES_GB:
        if abs(value - size) <= max(0.35, size * 0.06):
            return float(size)
    return round(value, 1)


def gb_label(value_gb: float | None) -> str:
    if value_gb is None:
        return "unknown"
    if abs(value_gb - round(value_gb)) < 0.05:
        return f"{int(round(value_gb))} GB"
    return f"{value_gb:.1f} GB"


def _round_half(value: float) -> float:
    return math.floor(value * 2 + 0.5) / 2


def suggest_cap(kind: str, memory_gb: float | None) -> float | None:
    """Suggested largest model file (GB) for this kind of computer."""
    if not memory_gb or memory_gb <= 0:
        return None
    ratio = {"gpu": GPU_RATIO, "apple": APPLE_RATIO}.get(kind, CPU_RATIO)
    return max(MIN_CAP_GB, _round_half(memory_gb * ratio))


# --- vendor guesses -----------------------------------------------------------

def vendor_of(name: str) -> str:
    low = (name or "").lower()
    if any(w in low for w in ("nvidia", "geforce", "quadro", "tesla", "rtx", "gtx")):
        return "nvidia"
    if any(w in low for w in ("amd", "radeon", "ati ", "firepro", "instinct")):
        return "amd"
    if any(w in low for w in ("intel", "iris", "uhd", "arc ")) or low.startswith("arc"):
        return "intel"
    if "apple" in low:
        return "apple"
    return "other"


def is_virtual_adapter(name: str) -> bool:
    return bool(_VIRTUAL_RE.search(name or ""))


def looks_dedicated(name: str, vram_gb: float | None) -> bool:
    vendor = vendor_of(name)
    if vram_gb is None or vram_gb < MIN_DEDICATED_GB:
        return False
    if vendor == "intel":
        return "arc" in (name or "").lower()
    if vendor == "amd" and _INTEGRATED_AMD_RE.search(name or ""):
        return False
    return vendor in {"nvidia", "amd", "other"}


def _gpu(name: str, vram_gb: float | None, source: str, *, at_least: bool = False) -> dict:
    name = " ".join(str(name or "").split()) or "Graphics card"
    vram = nominal_gb(vram_gb) if vram_gb else None
    return {
        "name": name,
        "vram_gb": vram,
        "vram_at_least": bool(at_least),
        "vendor": vendor_of(name),
        "dedicated": looks_dedicated(name, vram),
        "source": source,
    }


# --- NVIDIA (any OS) ----------------------------------------------------------

def parse_nvidia_smi(text: str) -> list[dict]:
    gpus = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0]:
            continue
        try:
            mib = float(re.sub(r"[^0-9.]", "", parts[1]) or 0)
        except ValueError:
            mib = 0
        gpus.append(_gpu(parts[0], (mib * MIB / GIB) if mib else None, "nvidia-smi"))
    return gpus


def detect_nvidia() -> list[dict]:
    candidates = ["nvidia-smi"]
    if sys.platform == "win32":
        candidates.append(r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe")
        candidates.append(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe"))
    for exe in candidates:
        out = _run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        if out.strip():
            return parse_nvidia_smi(out)
    return []


# --- Windows ------------------------------------------------------------------

def _reg_number(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, (bytes, bytearray)):
        if not value:
            return None
        return int.from_bytes(bytes(value[:8]), "little") or None
    try:
        return int(str(value).strip()) or None
    except ValueError:
        return None


def _windows_ram_gb() -> float | None:
    try:
        import ctypes
        from ctypes import wintypes  # noqa: F401
    except ImportError:
        return None
    try:
        kb = ctypes.c_ulonglong(0)
        if ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kb)) and kb.value:
            return kb.value * 1024 / GIB
    except Exception:  # noqa: BLE001
        pass
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys / GIB
    except Exception:  # noqa: BLE001
        pass
    return None


def _windows_active_adapters() -> list[tuple[str, str]]:
    """(name, registry key) for each display adapter Windows is using."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("DeviceName", wintypes.WCHAR * 32),
            ("DeviceString", wintypes.WCHAR * 128),
            ("StateFlags", wintypes.DWORD),
            ("DeviceID", wintypes.WCHAR * 128),
            ("DeviceKey", wintypes.WCHAR * 128),
        ]

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        user32 = ctypes.windll.user32
        for i in range(32):
            dd = DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
                break
            if dd.StateFlags & 0x8:  # mirroring driver
                continue
            key = str(dd.DeviceKey or "")
            ident = key.lower() or str(dd.DeviceString or "").lower()
            if ident in seen:
                continue
            seen.add(ident)
            out.append((str(dd.DeviceString or ""), key))
    except Exception:  # noqa: BLE001
        return out
    return out


def _windows_registry_adapters() -> list[dict]:
    """Every display adapter entry under the display Class key."""
    try:
        import winreg
    except ImportError:
        return []
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    rows = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            for i in range(64):
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as key:
                        rows.append(_read_adapter_key(winreg, key))
                except OSError:
                    continue
    except OSError:
        return rows
    return [r for r in rows if r.get("name")]


def _read_adapter_key(winreg, key) -> dict:
    def value(name):
        try:
            return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    name = value("DriverDesc")
    if isinstance(name, (bytes, bytearray)):
        name = bytes(name).decode("utf-16-le", errors="ignore").strip("\x00")
    q = _reg_number(value("HardwareInformation.qwMemorySize"))
    small = _reg_number(value("HardwareInformation.MemorySize"))
    mem = q or small
    return {
        "name": str(name or "").strip(),
        "bytes": mem,
        "at_least": bool(not q and small and small >= 0xFFF00000),
    }


def _windows_video_key(device_key: str) -> dict | None:
    try:
        import winreg
    except ImportError:
        return None
    path = device_key or ""
    prefix = "\\registry\\machine\\"
    if path.lower().startswith(prefix):
        path = path[len(prefix):]
    if not path:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return _read_adapter_key(winreg, key)
    except OSError:
        return None


def parse_cim_json(text: str) -> list[dict]:
    try:
        data = json.loads(text or "null")
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    gpus = []
    for row in data or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "").strip()
        if not name or is_virtual_adapter(name):
            continue
        ram = _reg_number(row.get("AdapterRAM"))
        # AdapterRAM is a 32-bit number, so anything 4 GB or more reads as ~4 GB.
        at_least = bool(ram and ram >= 0xFFF00000)
        gpus.append(_gpu(name, (ram / GIB) if ram else None, "windows-cim", at_least=at_least))
    return gpus


def detect_windows() -> dict:
    ram = _windows_ram_gb()
    gpus: list[dict] = []
    active = _windows_active_adapters()
    registry_rows = None
    for name, key in active:
        if is_virtual_adapter(name):
            continue
        row = _windows_video_key(key)
        if not row or not row.get("bytes"):
            registry_rows = registry_rows if registry_rows is not None else _windows_registry_adapters()
            matches = [r for r in registry_rows if r["name"].lower() == name.lower() and r.get("bytes")]
            row = max(matches, key=lambda r: r["bytes"]) if matches else {"name": name, "bytes": None}
        mem = row.get("bytes")
        gpus.append(_gpu(name or row.get("name") or "", (mem / GIB) if mem else None,
                         "windows-registry", at_least=bool(row.get("at_least"))))
    if not active:
        rows = _windows_registry_adapters()
        by_name: dict[str, dict] = {}
        for r in rows:
            if is_virtual_adapter(r["name"]):
                continue
            prev = by_name.get(r["name"].lower())
            if prev is None or (r.get("bytes") or 0) > (prev.get("bytes") or 0):
                by_name[r["name"].lower()] = r
        for r in by_name.values():
            mem = r.get("bytes")
            gpus.append(_gpu(r["name"], (mem / GIB) if mem else None, "windows-registry",
                             at_least=bool(r.get("at_least"))))
    if not gpus and not active:
        out = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"],
            timeout=SLOW_COMMAND_TIMEOUT,
        )
        gpus = parse_cim_json(out)
    return {"ram_gb": ram, "gpus": gpus, "apple_silicon": False, "chip": platform.processor() or ""}


# --- macOS --------------------------------------------------------------------

def _sysctl(name: str) -> str:
    return _run(["sysctl", "-n", name]).strip()


def parse_vram_text(text: str) -> float | None:
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(GB|MB)", text or "", re.I)
    if not m:
        return None
    value = float(m.group(1))
    return value if m.group(2).upper() == "GB" else value / 1024


def parse_system_profiler(text: str) -> list[dict]:
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        return []
    gpus = []
    for row in (data.get("SPDisplaysDataType") or []) if isinstance(data, dict) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("sppci_model") or row.get("_name") or "").strip()
        if not name:
            continue
        vram = parse_vram_text(str(row.get("spdisplays_vram") or row.get("_spdisplays_vram") or ""))
        gpus.append(_gpu(name, vram, "system_profiler"))
    return gpus


def detect_macos() -> dict:
    ram = None
    raw = _sysctl("hw.memsize")
    if raw.isdigit():
        ram = int(raw) / GIB
    chip = _sysctl("machdep.cpu.brand_string")
    arm = _sysctl("hw.optional.arm64") == "1" or chip.lower().startswith("apple")
    gpus: list[dict] = []
    if arm:
        label = (chip or "Apple Silicon") + " graphics (shares memory with the computer)"
        gpus.append({"name": label, "vram_gb": None, "vram_at_least": False,
                     "vendor": "apple", "dedicated": False, "source": "sysctl"})
    else:
        gpus = parse_system_profiler(
            _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=SLOW_COMMAND_TIMEOUT)
        )
    return {"ram_gb": ram, "gpus": gpus, "apple_silicon": bool(arm), "chip": chip}


# --- Linux --------------------------------------------------------------------

def parse_meminfo(text: str) -> float | None:
    m = re.search(r"^MemTotal:\s+(\d+)\s*kB", text or "", re.M)
    return int(m.group(1)) * 1024 / GIB if m else None


def _linux_amd_sysfs() -> list[dict]:
    gpus = []
    drm = Path("/sys/class/drm")
    try:
        cards = sorted(p for p in drm.iterdir() if re.fullmatch(r"card\d+", p.name))
    except OSError:
        return gpus
    for card in cards:
        dev = card / "device"
        vendor = _read_text(str(dev / "vendor")).strip().lower()
        if vendor != "0x1002":
            continue
        raw = _read_text(str(dev / "mem_info_vram_total")).strip()
        vram = int(raw) / GIB if raw.isdigit() else None
        name = _read_text(str(dev / "product_name")).strip() or "AMD Radeon graphics"
        gpus.append(_gpu(name, vram, "amdgpu-sysfs"))
    return gpus


def parse_rocm_smi(text: str) -> list[dict]:
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        return []
    gpus = []
    for key, row in (data.items() if isinstance(data, dict) else []):
        if not isinstance(row, dict) or not str(key).lower().startswith("card"):
            continue
        name = str(row.get("Card series") or row.get("Card SKU") or row.get("Device Name") or "AMD Radeon graphics")
        total = None
        for k, v in row.items():
            if "vram total memory" in str(k).lower():
                total = _reg_number(v)
        gpus.append(_gpu(name, (total / GIB) if total else None, "rocm-smi"))
    return gpus


def detect_linux() -> dict:
    ram = parse_meminfo(_read_text("/proc/meminfo"))
    gpus = _linux_amd_sysfs()
    if not gpus:
        gpus = parse_rocm_smi(_run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"]))
    chip = ""
    cpuinfo = _read_text("/proc/cpuinfo")
    m = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.M)
    if m:
        chip = m.group(1).strip()
    return {"ram_gb": ram, "gpus": gpus, "apple_silicon": False, "chip": chip}


# --- putting it together ------------------------------------------------------

def os_label(system: str) -> str:
    return {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(system, system or "unknown")


def build_profile(raw: dict, *, system: str | None = None) -> dict:
    """Turn raw findings into plain words, a kind, and a suggested limit."""
    system = system if system is not None else _system()
    ram = nominal_gb(raw.get("ram_gb"))
    gpus = [g for g in (raw.get("gpus") or []) if not is_virtual_adapter(g.get("name", ""))]
    apple = bool(raw.get("apple_silicon"))
    dedicated = [g for g in gpus if g.get("dedicated") and g.get("vram_gb")]
    best = max(dedicated, key=lambda g: g["vram_gb"]) if dedicated else None
    notes: list[str] = []

    if best:
        kind, memory = "gpu", float(best["vram_gb"])
    elif apple and ram:
        kind, memory = "apple", ram
    else:
        kind, memory = "cpu", ram
    cap = suggest_cap(kind, memory)

    parts = []
    if best:
        label = f"{best['name']}, {gb_label(best['vram_gb'])}"
        if best.get("vram_at_least"):
            label = f"{best['name']}, at least {gb_label(best['vram_gb'])}"
        parts.append(label + " graphics memory")
    elif apple:
        parts.append(raw.get("chip") or "Apple Silicon")
    elif gpus:
        parts.append(gpus[0]["name"] + " (built-in graphics)")
    if ram:
        parts.append(f"{gb_label(ram)} unified memory" if apple else f"{gb_label(ram)} system memory")
    summary = " · ".join(parts) if parts else "Could not read this computer's memory or graphics card"

    if kind == "gpu":
        explanation = (
            f"Models up to about {gb_label(cap)} should fit on the graphics card "
            f"(about {int(GPU_RATIO * 100)}% of its {gb_label(memory)}, leaving room to run)."
        )
        if len(dedicated) > 1:
            notes.append(
                f"This computer has {len(dedicated)} graphics cards. The limit uses the largest one."
            )
        if best.get("vram_at_least"):
            notes.append("Windows only reported \"4 GB or more\" for this card, so the real amount may be higher. You can raise the limit.")
        if best.get("vendor") == "amd":
            notes.append("AMD cards work with many local AI apps (such as LM Studio and Ollama), but support varies.")
        if best.get("vendor") == "intel":
            notes.append("Intel Arc support in local AI apps is still improving, so results may vary.")
    elif kind == "apple":
        explanation = (
            f"Macs share memory between the computer and the graphics, so models up to about "
            f"{gb_label(cap)} should fit (about {int(APPLE_RATIO * 100)}% of {gb_label(memory)})."
        )
    elif memory:
        explanation = (
            f"No separate graphics card was found, so models would run on the main processor. "
            f"Models up to about {gb_label(cap)} should fit in {gb_label(memory)} of memory, "
            "but they will run more slowly."
        )
    else:
        explanation = (
            "The app could not read this computer's memory. Pick the preset closest to this "
            "computer, or type your own size limit."
        )
    ok = bool(memory)
    formats = ["gguf", "mlx"] if apple else ["gguf"]
    return {
        "ok": ok,
        "os": os_label(system),
        "ram_gb": ram,
        "gpus": gpus,
        "best_gpu": best,
        "apple_silicon": apple,
        "chip": raw.get("chip") or "",
        "kind": kind,
        "memory_for_models_gb": memory,
        "suggested_max_model_gb": cap,
        "summary": summary,
        "explanation": explanation,
        "notes": notes,
        "formats": formats,
        "detected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def detect(system: str | None = None) -> dict:
    """Detect hardware for this computer. Never raises."""
    system = system if system is not None else _system()
    started = time.monotonic()
    raw: dict = {"ram_gb": None, "gpus": [], "apple_silicon": False, "chip": ""}
    errors: list[str] = []
    try:
        if system == "Windows":
            raw = detect_windows()
        elif system == "Darwin":
            raw = detect_macos()
        else:
            raw = detect_linux()
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
    if system != "Darwin":
        try:
            nvidia = detect_nvidia()
        except Exception as exc:  # noqa: BLE001
            nvidia = []
            errors.append(str(exc))
        if nvidia:
            others = [g for g in raw.get("gpus") or [] if g.get("vendor") != "nvidia"]
            raw["gpus"] = nvidia + others
    profile = build_profile(raw, system=system)
    profile["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    if errors:
        profile["errors"] = errors[:3]
    return profile


if __name__ == "__main__":  # pragma: no cover - handy for a quick manual check
    print(json.dumps(detect(), indent=2))
