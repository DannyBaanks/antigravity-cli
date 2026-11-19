"""agy_dash.procs — split verbatim from agy_dashboard.py @ f255869 (Phase 1, no behaviour change)."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths as _paths
from . import tmuxctl as _tmuxctl


_ZIG_BUILD_FAILED_AT = 0.0  # negative cache: a failed `zig build` is not retried on every ~0.5 s refresh
_ZIG_BUILD_RETRY_SEC = 600.0

# Last process-scan outcome. A failed scan must never look like "no agy running": callers that would open
# a second writer or kill something check `scan_failed()` first.
SCAN_STATE: dict[str, object] = {"failed": False, "reason": ""}


def scan_failed() -> bool:
    return bool(SCAN_STATE.get("failed"))


def _mark_scan(failed: bool, reason: str = "") -> None:
    SCAN_STATE["failed"] = failed
    SCAN_STATE["reason"] = reason


def ensure_zig_core() -> bool:
    """Return True if `agy-fast-core` exists, compiling via `zig build` if missing."""
    global _ZIG_BUILD_FAILED_AT
    if _paths.ZIG_CORE_BIN.is_file() and os.access(_paths.ZIG_CORE_BIN, os.X_OK):
        return True
    if not shutil.which("zig") or not (_paths.ZIG_CORE_DIR / "build.zig").is_file():
        return False
    if _ZIG_BUILD_FAILED_AT and time.time() - _ZIG_BUILD_FAILED_AT < _ZIG_BUILD_RETRY_SEC:
        return False
    try:
        cp = subprocess.run(
            ["zig", "build", "-Drelease"],
            cwd=str(_paths.ZIG_CORE_DIR),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=90,
        )
        built = _paths.ZIG_CORE_DIR / "zig-out" / "bin" / "agy-fast-core"
        if cp.returncode == 0 and built.is_file():
            tmp = _paths.ZIG_CORE_BIN.with_suffix(".tmp")
            shutil.copy2(built, tmp)
            os.chmod(tmp, 0o755)
            os.replace(tmp, _paths.ZIG_CORE_BIN)
            return True
    except Exception:
        pass
    _ZIG_BUILD_FAILED_AT = time.time()
    return False


def enable_ksm_memory_merge() -> bool:
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        return libc.prctl(_paths.PR_SET_MEMORY_MERGE, 1, 0, 0, 0) == 0
    except Exception:
        return False


@dataclass
class ProcMemInfo:
    pid: int
    ppid: int = 0
    tty: str = "?"
    rss_kb: int = 0
    pss_kb: int = 0
    pss_anon_kb: int = 0
    shared_clean_kb: int = 0
    ksm_kb: int = 0
    swap_kb: int = 0
    active_children: list[str] = field(default_factory=list)
    in_dash: bool = False
    start_ticks: int = 0  # /proc/<pid>/stat starttime at scan time: a recycled PID never matches


def is_ancestor_pid(target_pid: int) -> bool:
    cur = os.getpid()
    seen: set[int] = set()
    while cur > 1 and cur not in seen:
        if cur == target_pid:
            return True
        seen.add(cur)
        try:
            stat_text = Path(f"/proc/{cur}/stat").read_text()
            after_comm = stat_text.rsplit(")", 1)[1].split()
            cur = int(after_comm[1])
        except Exception:
            break
    return False


def proc_start_ticks(pid: int) -> int:
    """Kernel start time of `pid` (field 22 of /proc/<pid>/stat); 0 if it does not exist or already exited
    (a zombie keeps its /proc entry until reaped, but it is dead)."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return 0 if fields[0] in ("Z", "X") else int(fields[19])
    except (OSError, ValueError, IndexError):
        return 0


def _proc_is_agy(pid: int) -> bool:
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    # Exact basename: `agy-dash`, `agy-fast-core`, `agy-limited`, `agy_dashboard.py` are NOT agy.
    names = {"agy", Path(_paths.AGY_BIN).name}
    return any(Path(a.decode("utf-8", "replace")).name in names for a in argv[:2] if a)


def terminate_verified(proc: "ProcMemInfo", *, timeout_s: float = 8.0) -> tuple[bool, str]:
    """SIGTERM `proc` only if it is still the same `agy` process the scan saw, then wait until it is gone.

    Never kills an ancestor of this process, a recycled PID (start time changed) or a non-agy binary,
    and never escalates to SIGKILL: an agy that ignores SIGTERM is reported, not destroyed."""
    pid = proc.pid
    if pid <= 1 or is_ancestor_pid(pid):
        return False, f"PID {pid} es esta misma sesión"
    cur = proc_start_ticks(pid)
    if cur == 0:
        return True, f"PID {pid} ya no existía"
    if not proc.start_ticks:
        return False, f"PID {pid}: hora de arranque desconocida en el escaneo; no se toca"
    if cur != proc.start_ticks:
        return False, f"PID {pid} fue reutilizado por otro proceso; no se toca"
    if not _proc_is_agy(pid):
        return False, f"PID {pid} ya no es agy; no se toca"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return False, f"PID {pid}: {e}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc_start_ticks(pid) != cur:
            return True, f"PID {pid} cerrado"
        time.sleep(0.1)
    return False, f"PID {pid} no terminó en {timeout_s:.0f}s (sigue vivo)"


def zig_scan() -> tuple[int, dict[str, int], dict[str, list[ProcMemInfo]]]:
    """Call `agy-fast-core scan` (Zig) and parse its JSON in Python."""
    dash_server_pids = _tmuxctl._get_tmux_dash_server_pids()
    reason = "agy-fast-core no disponible (ni compilable)"
    if ensure_zig_core():
        try:
            cp = subprocess.run(
                [str(_paths.ZIG_CORE_BIN), "scan"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if cp.returncode == 0 and cp.stdout.strip():
                data = json.loads(cp.stdout)
                scan_us = int(data.get("scan_us", 0))
                ksm = data.get("ksm", {"run": 1, "pages_shared": 0, "pages_sharing": 0, "pages_scanned": 0})
                by_conv: dict[str, list[ProcMemInfo]] = {}
                for p in data.get("procs", []):
                    cid = p.get("conv_id") or ""
                    if not cid:
                        continue
                    kids = [
                        c for c in p.get("children", [])
                        if not c.startswith("agy-fast-core(")
                    ]
                    ppid = int(p.get("ppid", 0))
                    pinfo = ProcMemInfo(
                        pid=int(p["pid"]),
                        ppid=ppid,
                        tty=str(p.get("tty", "?")),
                        rss_kb=int(p.get("rss_kb", 0)),
                        pss_kb=int(p.get("pss_kb", 0)),
                        pss_anon_kb=int(p.get("pss_anon_kb", 0)),
                        shared_clean_kb=int(p.get("shared_clean_kb", 0)),
                        ksm_kb=int(p.get("ksm_kb", 0)),
                        swap_kb=int(p.get("swap_kb", 0)),
                        active_children=kids,
                        in_dash=(ppid in dash_server_pids),
                        start_ticks=proc_start_ticks(int(p["pid"])),
                    )
                    by_conv.setdefault(cid, []).append(pinfo)
                _mark_scan(False)
                return scan_us, ksm, by_conv
            reason = f"agy-fast-core scan rc={cp.returncode}"
        except subprocess.TimeoutExpired:
            reason = "agy-fast-core scan tardó >1.5s"
        except Exception as exc:
            reason = f"agy-fast-core scan: {type(exc).__name__}"
    _mark_scan(True, reason)
    return 0, {"run": 1, "pages_shared": 0, "pages_sharing": 0, "pages_scanned": 0}, {}
