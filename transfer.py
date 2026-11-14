"""Detailed download progress: GB done, speed, time left, and "still going".

A download can slow to a crawl without stopping. To make that visible, a
small background thread looks at the bytes arriving on disk every second
(huggingface_hub writes into hidden ".incomplete" files first, and those
are counted too). From that it works out:

- how much of each file and of the whole item is done,
- a smoothed speed over the last several seconds,
- roughly how long is left,
- and how long it has been since any new data arrived.

The page turns "no new data for a while" into "Slow connection, still
going…" so a slow download does not look frozen. Only a real error is
shown as a stop.
"""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
from collections import deque
from pathlib import Path

SAMPLE_SECONDS = 1.0        # how often the disk is checked
SPEED_WINDOW_SECONDS = 10.0  # speed is averaged over about this long
STALL_AFTER_SECONDS = 25.0   # no new bytes for this long -> "slow, still going"
SMOOTHING = 0.35             # 0..1, higher follows changes faster
METER_FIELD = "_meter"
MONITOR_FIELD = "_monitored"

_LOCK = threading.Lock()


def _clock() -> float:
    return time.monotonic()


class SpeedMeter:
    """Turns (time, bytes so far) samples into a steady speed."""

    def __init__(self, window: float = SPEED_WINDOW_SECONDS):
        self.window = float(window)
        self.samples: deque = deque()
        self.last_bytes: int | None = None
        self.last_change: float | None = None
        self.started: float | None = None
        self.smoothed: float = 0.0
        self._lock = threading.Lock()

    def reset(self, now: float | None = None) -> None:
        now = _clock() if now is None else now
        self.samples.clear()
        self.last_bytes = None
        self.last_change = now
        self.started = now
        self.smoothed = 0.0

    def observe(self, done: int, now: float | None = None) -> None:
        with self._lock:
            self._observe(done, now)

    def _observe(self, done: int, now: float | None) -> None:
        now = _clock() if now is None else now
        done = max(0, int(done or 0))
        if self.started is None:
            self.reset(now)
        if self.last_bytes is not None and done < self.last_bytes:
            # A new file or a restarted download: start the averages over.
            self.reset(now)
        if self.last_bytes is None or done > self.last_bytes:
            if self.last_bytes is not None:
                self.last_change = now
            self.last_bytes = done
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        if len(self.samples) >= 2:
            t0, b0 = self.samples[0]
            t1, b1 = self.samples[-1]
            span = t1 - t0
            if span >= 0.5:
                raw = max(0.0, (b1 - b0) / span)
                self.smoothed = raw if self.smoothed == 0.0 else (
                    SMOOTHING * raw + (1 - SMOOTHING) * self.smoothed
                )

    def speed(self) -> float:
        return float(self.smoothed or 0.0)

    def quiet_seconds(self, now: float | None = None) -> float:
        now = _clock() if now is None else now
        ref = self.last_change if self.last_change is not None else self.started
        return max(0.0, now - ref) if ref is not None else 0.0


def meter_for(sink: dict) -> SpeedMeter:
    meter = sink.get(METER_FIELD)
    if not isinstance(meter, SpeedMeter):
        meter = SpeedMeter()
        meter.reset()
        sink[METER_FIELD] = meter
    return meter


def reset_item(sink: dict | None, *, total_bytes: int = 0, name: str = "") -> None:
    """Call when a new item starts so speed and quiet time start fresh."""
    if sink is None:
        return
    with _LOCK:
        sink["downloaded_bytes"] = 0
        sink["total_bytes"] = int(total_bytes or 0)
        sink["percent"] = 0
        sink["speed_bps"] = 0.0
        sink["eta_seconds"] = None
        sink["active_files"] = []
        sink["files_done"] = None
        sink["files_total"] = None
        sink["item_name"] = name
        meter_for(sink).reset()


def live_fields(sink: dict | None, now: float | None = None) -> dict:
    """Speed, time left and quiet time for the page (computed at read time)."""
    if not sink:
        return {}
    now = _clock() if now is None else now
    meter = meter_for(sink)
    done = int(sink.get("downloaded_bytes") or 0)
    meter.observe(done, now)
    speed = meter.speed()
    total = int(sink.get("total_bytes") or 0)
    eta = None
    if total and speed > 0 and total > done:
        eta = int((total - done) / speed)
    quiet = meter.quiet_seconds(now)
    return {
        "speed_bps": round(speed, 1),
        "eta_seconds": eta,
        "quiet_seconds": int(quiet),
        "stalled": quiet >= STALL_AFTER_SECONDS,
        "elapsed_seconds": int(max(0.0, now - (meter.started or now))),
    }


def short_hash(filename: str) -> str:
    """Same naming huggingface_hub uses for a file's .incomplete copy."""
    return base64.urlsafe_b64encode(hashlib.sha1(filename.encode()).digest()).decode()


class TransferMonitor:
    """Watch a folder grow and publish detailed progress into `sink`.

    `file_sizes` maps repo file paths to their sizes when they are known;
    it is used for per-file totals and for naming .incomplete files.
    """

    def __init__(
        self,
        sink: dict | None,
        folder: Path,
        *,
        file_sizes: dict[str, int] | None = None,
        total_bytes: int | None = None,
        interval: float = SAMPLE_SECONDS,
    ):
        self.sink = sink
        self.folder = Path(folder)
        self.file_sizes = {str(k): int(v or 0) for k, v in (file_sizes or {}).items()}
        self.total_bytes = int(total_bytes or 0) or sum(self.file_sizes.values())
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._owner = False
        self._by_hash = {}
        for path in self.file_sizes:
            base = path.replace("\\", "/").rsplit("/", 1)[-1]
            self._by_hash[short_hash(base + ".metadata")] = path
        self._baseline, self._baseline_files = self._measure()

    # -- measuring -------------------------------------------------------
    def _measure(self) -> tuple[int, dict[str, int]]:
        """Total bytes under the folder and each file's size (relative path)."""
        total = 0
        sizes: dict[str, int] = {}
        try:
            for dirpath, dirnames, filenames in os.walk(self.folder):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    total += size
                    rel = os.path.relpath(full, self.folder).replace("\\", "/")
                    sizes[rel] = size
        except OSError:
            pass
        return total, sizes

    def _active(self, sizes: dict[str, int]) -> list[dict]:
        rows = []
        for rel, size in sizes.items():
            if not rel.endswith(".incomplete"):
                continue
            base = rel.rsplit("/", 1)[-1]
            short = base.split(".", 1)[0]
            name = self._by_hash.get(short)
            if name is None and len(self.file_sizes) == 1:
                name = next(iter(self.file_sizes))
            total = self.file_sizes.get(name or "", 0)
            rows.append({
                "name": (name or "a file").replace("\\", "/").rsplit("/", 1)[-1],
                "done_bytes": int(size),
                "total_bytes": int(total),
                "percent": round(min(100.0, 100.0 * size / total), 1) if total else None,
            })
        rows.sort(key=lambda r: -r["done_bytes"])
        return rows[:6]

    def _files_done(self, sizes: dict[str, int]) -> int | None:
        if not self.file_sizes:
            return None
        done = 0
        for path, want in self.file_sizes.items():
            have = sizes.get(path.replace("\\", "/"))
            if have is not None and (not want or have >= want):
                done += 1
        return done

    def sample(self, now: float | None = None) -> None:
        if self.sink is None:
            return
        current, sizes = self._measure()
        done = max(0, current - self._baseline)
        with _LOCK:
            sink = self.sink
            reported = int(sink.get("downloaded_bytes") or 0)
            done = max(done, reported)  # never run backwards on screen
            if self.total_bytes:
                sink["total_bytes"] = self.total_bytes
                done_for_pct = min(done, self.total_bytes)
                sink["percent"] = round(min(99.9, 100.0 * done_for_pct / self.total_bytes), 1)
            sink["downloaded_bytes"] = done
            sink["active_files"] = self._active(sizes)
            files_done = self._files_done(sizes)
            if files_done is not None:
                sink["files_done"] = files_done
                sink["files_total"] = len(self.file_sizes)
            live = live_fields(sink, now)
            sink["speed_bps"] = live.get("speed_bps")
            sink["eta_seconds"] = live.get("eta_seconds")

    # -- thread ------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - progress must never break a download
                pass

    def __enter__(self):
        if self.sink is None:
            return self
        if self.sink.get(MONITOR_FIELD):
            # An outer monitor (whole item) is already watching.
            return self
        self._owner = True
        with _LOCK:
            self.sink[MONITOR_FIELD] = True
            if self.total_bytes:
                self.sink["total_bytes"] = self.total_bytes
            meter_for(self.sink)
        self._thread = threading.Thread(target=self._run, name="mrf-transfer", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._owner:
            return False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            self.sample()
        except Exception:  # noqa: BLE001
            pass
        with _LOCK:
            self.sink.pop(MONITOR_FIELD, None)
            if exc_type is None:
                self.sink["active_files"] = []
                if self.file_sizes:
                    self.sink["files_done"] = len(self.file_sizes)
                    self.sink["files_total"] = len(self.file_sizes)
        return False


def format_bytes(n: float | None) -> str:
    """Plain size text, used in log lines."""
    if not n:
        return "0 MB"
    n = float(n)
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    return f"{n / 1024 ** 2:.0f} MB"
