"""agy_dash.paths — split verbatim from agy_dashboard.py @ f255869 (Phase 1, no behaviour change)."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import os
import re
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@contextlib.contextmanager
def file_lock(target: Path):
    """Exclusive advisory lock beside `target` (TUI, Alt+H hotkey, hooks and the quota thread share files)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(str(target) + ".lock", "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def atomic_write_text(target: Path, text: str, *, mode: int | None = None) -> None:
    """Write via a unique temp file in the same dir + os.replace: two writers never share one `.tmp`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _resolve_real_home() -> Path:
    raw = os.environ.get("AGY_REAL_HOME") or str(Path.home())
    marker = "/.gemini/antigravity-cli/accounts/"
    if marker in raw:
        raw = raw.split(marker, 1)[0]
    return Path(raw)


HOME = _resolve_real_home()
SCRIPT_DIR = Path(__file__).resolve().parent.parent
ZIG_CORE_DIR = SCRIPT_DIR / "zig_core"
ZIG_CORE_BIN = SCRIPT_DIR / "agy-fast-core"

AGY_DATA_DIR = HOME / ".gemini" / "antigravity-cli"
SUMMARIES_DB = AGY_DATA_DIR / "conversation_summaries.db"
BRAIN_DIR = AGY_DATA_DIR / "brain"
CONVERSATIONS_DIR = AGY_DATA_DIR / "conversations"
PRESENCE_DIR = AGY_DATA_DIR / "presence"
HIBERNATED_FILE = AGY_DATA_DIR / "dashboard_hibernated.json"
CUSTOM_TITLES_FILE = AGY_DATA_DIR / "custom_titles.json"
NVAPI_CONFIG_FILE = AGY_DATA_DIR / "nvapi_config.json"
COMPACTED_DIR = AGY_DATA_DIR / "compacted"
HEARTBEATS_DIR = AGY_DATA_DIR / "heartbeats"
SANDBOX_DIR = Path(os.environ.get("AGY_DASH_SANDBOX_DIR") or (AGY_DATA_DIR / "sandbox"))
ACCOUNTS_DIR = AGY_DATA_DIR / "accounts"
ACCOUNTS_REGISTRY = ACCOUNTS_DIR / "registry.json"
TMUX_CONF = AGY_DATA_DIR / "agy-dash.tmux.conf"
TMUX_SOCKET = "agy-dash"
TMUX_SESSION = "agy"
TMUX_BG_SESSION = "agy-bg"

AGY_BIN = os.environ.get("AGY_BIN", str(HOME / ".local" / "bin" / "agy"))
DEFAULT_GOGC = os.environ.get("AGY_GOGC", "35")
DEFAULT_GOMEMLIMIT = os.environ.get("AGY_GOMEMLIMIT", "180MiB")
DEFAULT_IDLE_HIBERNATE_SEC = int(os.environ.get("AGY_IDLE_HIBERNATE_SEC", "0"))
QUOTA_WARN_PCT = float(os.environ.get("AGY_QUOTA_WARN_PCT", "15"))
QUOTA_CRIT_PCT = float(os.environ.get("AGY_QUOTA_CRIT_PCT", "5"))
QUOTA_POLL_INTERVAL_SEC = float(os.environ.get("AGY_QUOTA_POLL_SEC", "120"))
DEFAULT_NVAPI_BASE_URL = os.environ.get("AGY_NVAPI_BASE_URL", "https://integrate.api.nvidia.com/v1")
DEFAULT_NVAPI_MODEL = os.environ.get("AGY_NVAPI_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
LED_TICK_SEC = float(os.environ.get("AGY_LED_TICK_SEC", "0.9"))  # tab spinner cadence (each tick = tmux redraw)
HEARTBEAT_INTERVAL_SEC = float(os.environ.get("AGY_HB_INTERVAL_SEC", "300"))
HEARTBEAT_WARN_TOKENS = int(os.environ.get("AGY_HB_WARN_TOKENS", "100000"))
HEARTBEAT_RECOMMEND_TOKENS = int(os.environ.get("AGY_HB_RECOMMEND_TOKENS", "150000"))
HEARTBEAT_URGENT_TOKENS = int(os.environ.get("AGY_HB_URGENT_TOKENS", "200000"))
COMPACT_MIN_STEPS = int(os.environ.get("AGY_COMPACT_MIN_STEPS", "40"))
COMPACT_MIN_BYTES = int(os.environ.get("AGY_COMPACT_MIN_BYTES", "49152"))

MODEL_DISPLAY_TO_SLUG: dict[str, str] = {
    "Gemini 4 Argon (Medium)": "gemini-4-argon-medium",
    "Gemini 4 Argon (High)": "gemini-4-argon-high",
    "Gemini 4 Argon (Low)": "gemini-4-argon-low",
    "Gemini 4 Hydrogen (High)": "gemini-4-hydrogen-high",
    "Gemini 4 Hydrogen (XHigh)": "gemini-4-hydrogen-xhigh",
    "Gemini 4 Hydrogen (Medium)": "gemini-4-hydrogen-medium",
    "Gemini 4 Hydrogen (Low)": "gemini-4-hydrogen-low",
    "Orchestrator (Argon)": "gemini-pro-orchestrator-g1-pro",
    "Gemini 3.8 Flash (High)": "gemini-3.8-flash-high",
    "Gemini 3.8 Flash (Medium)": "gemini-3.8-flash-medium",
    "Gemini 3.8 Flash (Low)": "gemini-3.8-flash-low",
    "Claude Opus 5.5 (Low)": "claude-opus-5-5-low",
    "Claude Opus 5.5 (Medium)": "claude-opus-5-5-medium",
    "Claude Opus 5.5 (High)": "claude-opus-5-5-high",
    "Claude Sonnet 5.5 (Low)": "claude-sonnet-5-5-low",
    "Claude Sonnet 5.5 (Medium)": "claude-sonnet-5-5-medium",
    "Claude Sonnet 5.5 (High)": "claude-sonnet-5-5-high",
    "GPT-OSS 120B (Medium)": "gpt-oss-120b-medium",
}

PR_SET_MEMORY_MERGE = 67

DEFAULT_TIMEZONE = os.environ.get("AGY_TZ") or "America/Mexico_City"
_CDMX_TZ_NAME = "America/Mexico_City"
_CDMX_FALLBACK_TZ = dt.timezone(dt.timedelta(hours=-6), "CDMX")
_ISO_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def _configured_tz_name() -> str:
    return (os.environ.get("AGY_TZ") or DEFAULT_TIMEZONE).strip() or _CDMX_TZ_NAME


def display_timezone() -> dt.tzinfo:
    """Return the configured display timezone (default: CDMX / America/Mexico_City)."""
    tz_name = _configured_tz_name()
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return _CDMX_FALLBACK_TZ


def display_tz_label(moment: dt.datetime | None = None) -> str:
    """Short human-friendly label for the display timezone ('CDMX' for America/Mexico_City)."""
    tz_name = _configured_tz_name()
    if tz_name.lower() in (_CDMX_TZ_NAME.lower(), "cdmx"):
        return "CDMX"
    tz = display_timezone()
    ref = moment.astimezone(tz) if moment is not None else dt.datetime.now(tz)
    return ref.tzname() or tz_name


def parse_iso_utc(raw: str | None) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp (including Go nanosecond SQLite strings) into UTC datetime."""
    s = str(raw or "").strip()
    if not s or s.startswith("0001-"):
        return None
    s = _ISO_FRAC_RE.sub(r"\1", s.replace(" ", "T", 1))
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def format_display_clock(
    ts: float | None = None,
    fmt: str = "%H:%M:%S",
    *,
    with_tz: bool = False,
) -> str:
    """Format an epoch timestamp (or now) in the configured display timezone."""
    tz = display_timezone()
    moment = dt.datetime.now(tz) if ts is None else dt.datetime.fromtimestamp(ts, tz=tz)
    out = moment.strftime(fmt)
    return f"{out} {display_tz_label(moment)}" if with_tz else out


def format_display_iso(
    raw: str | None,
    *,
    fmt: str = "%Y-%m-%d %H:%M:%S",
    with_tz: bool = True,
    empty: str = "",
) -> str:
    """Convert an ISO UTC timestamp string into the configured display timezone (default CDMX)."""
    s = str(raw or "").strip()
    if not s:
        return empty
    parsed = parse_iso_utc(s)
    if parsed is None:
        return s
    moment = parsed.astimezone(display_timezone())
    out = moment.strftime(fmt)
    return f"{out} {display_tz_label(moment)}" if with_tz else out


SELF_PATH = SCRIPT_DIR / "agy_dashboard.py"  # the shim; tabs/hooks re-exec this file
