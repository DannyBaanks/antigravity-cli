#!/usr/bin/env python3
"""isymem / agent_memory.py — Shared Multi-Harness Memory System (Claude-compatible).

One memory bank for every TUI on this machine (Claude Code, AGY, Codex, Grok,
OpenCode, Qwen, Crush, Hermes, Kimi, Cursor, Copilot, Pi, FX, OpenClaw,
CommandCode, Kilo, ISyCode). Storage is Claude Code's own project memory dir,
so Claude reads it natively; `docs/agent/memory` is a symlink to it.

Usage:
  isymem list [--limit N] [--compact] [--type T]
  isymem recall "<what you are about to do>" [--limit N] [--show]
  isymem search <literal text>
  isymem show <slug> [--no-links]
  isymem save <slug> --name "<title>" --desc "<1-line summary>" \
      [--type project|feedback|user|reference] \
      [--why "<why>"] [--how "<how to apply>"] [--body "<facts>"] [--replace]
  isymem archive <slug>          (alias: remove — never deletes, see history)
  isymem history [<slug>]
  isymem doctor [--stale-days N]
  isymem sync                    (refresh the index copy in inline TUI configs)
  isymem setup [--dry-run] [--undo]

Environment (tests / other machines):
  ISYMEM_DIR         memory dir override (skips repo symlink management)
  ISYMEM_STATE_DIR   history/archive/lock dir (default ~/.local/state/isymem)
  ISYMEM_AUTHOR      author recorded in metadata.author (else auto-detected)
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

ROOT = Path(os.environ.get("ISYMEM_ROOT") or Path.cwd()).resolve()
HOME = Path.home()
_CLAUDE_PROJECT_KEY = re.sub(r"[^A-Za-z0-9]", "-", str(ROOT))
CLAUDE_MEM_DIR = HOME / ".claude" / "projects" / _CLAUDE_PROJECT_KEY / "memory"
REPO_MEM_LINK = ROOT / "docs" / "agent" / "memory"
CONFIG_DIR = HOME / ".config" / "isyco-memory"
CANONICAL_RULE_PATH = CONFIG_DIR / "agents.md"
STATE_DIR = Path(
    os.environ.get("ISYMEM_STATE_DIR")
    or Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local" / "state") / "isymem"
)

MARKER_BEGIN = "<!-- isymem:setup begin -->"
MARKER_END = "<!-- isymem:setup end -->"
INDEX_HEADING = "## Shared memory index (auto-synced by `isymem`; do not edit here)"

VALID_TYPES = ("project", "feedback", "user", "reference")
# Claude Code truncates the auto-loaded MEMORY.md after this many lines.
INDEX_LINE_BUDGET = 200
INDEX_ENTRY_SOFT_MAX = 300  # chars per index line before doctor complains

RULE_BODY = """\
# Shared Multi-Harness Memory (`isymem` / `docs/agent/memory/`)

All TUIs on this machine (Claude Code, AGY, Codex, Grok, OpenCode, Qwen, etc.) share one memory bank at `docs/agent/memory/` (symlinked to `~/.claude/projects/<project>/memory/`). It is a graph you discover progressively; read only the level you need:

| Level | Command | Gives |
|---|---|---|
| L0 | index (`MEMORY.md`, below) | general rules + one line per project hub |
| L1 | `isymem <project>` · `isymem projects` | project card: its memories, open fails/todos, neighbour projects |
| L2 | `isymem <project> --fails --todo` (`--rules --state --ideas --done --open --all`); no project = all | one line per fact |
| L3 | `isymem show <slug>` · `isymem graph <slug-or-project>` | the full memory · its [[links]] |

- **Before a task:** read the card of the project you touch (`isymem <project>`) and its `--open`/`--rules` lines. No obvious project: `isymem recall "<what you are about to do>"`. Open a full memory only if a line points there.
- **Anchors (one fact per line):** `- @todo#a1b2 #project YYYY-MM-DD: text [[slug]]`. Kinds: fail, todo, rule, state, idea (closed: done, fixed).
- **Write as you go:** found a failure, a pending step, a project rule, or a new state: `isymem add <project> --fail|--todo|--rule|--state|--idea "<one line>" [--link slug]`. Finished/fixed: `isymem close <id> --note "<commit/evidence>"`.
- **Longer memories** (`isymem save <slug> --name ... --desc "<1 line, <150 chars>" --type feedback|user|project|reference [--project P] --body/--why/--how ...`). With `--project` they live under that project's hub, not in the index. Save without being asked when the user corrects you (feedback), states a preference (user), a verified state/trap/handoff appears (project), or a pointer to an external resource (reference). Saving an existing slug APPENDS; `--replace` rewrites (snapshotted, `isymem history`).
- **Dated, not live:** verify a file, flag, count or SHA on disk before relying on it; fix or `isymem archive` (never deletes) what turned out wrong.
- **Never** store secrets/tokens (save refuses them), ephemeral output, or what the repo already records. Never hand-edit the `isymem:` auto blocks. `isymem doctor` checks the bank."""

# (label, path, inline). inline=True copies the rule + current index into the
# file (for harnesses that do not resolve @imports, such as Codex). inline=False
# writes an @import line (Claude Code; it already auto-loads MEMORY.md itself).
# `require_dir`: only install when the harness's home dir exists.
SETUP_TARGETS: list[tuple[str, Path, bool]] = [
    ("agy/gemini", HOME / ".gemini" / "GEMINI.md", True),
    ("codex", HOME / ".codex" / "AGENTS.md", True),
    ("qwen", HOME / ".qwen" / "QWEN.md", True),
    ("opencode", HOME / ".config" / "opencode" / "AGENTS.md", True),
    ("claude", HOME / ".claude" / "CLAUDE.md", False),
]

# Text that must never land in shared memory (AGENTS.md §0b, bridge token leaks).
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("bridge token", re.compile(r"\b(?:LEASE|IDENTITY)_TOKEN\s*=\s*(?!<)[A-Za-z0-9_\-]{8,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenAI/Anthropic key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("NVIDIA API key", re.compile(r"\bnvapi-[A-Za-z0-9_\-]{30,}")),
    (
        "secret assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|auth[_-]?token|access[_-]?token)"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=.]{20,}"
        ),
    ),
]

KNOWN_HARNESSES = (
    "claude", "codex", "opencode", "grok", "qwen", "gemini", "agy", "crush", "kimi",
    "hermes", "cursor-agent", "copilot", "kilo", "openclaw", "commandcode", "isycode",
    "openisy", "fx", "pi",
)

STOPWORDS = set(
    """
    the and for with that this from into onto are was were been have has had not but you your
    our its it's what when where which who how why all any can could should would will just
    then than them they their there here also only over under about after before again more
    most some such very use using used make made does did doing done get got set new old
    los las del una uno unos unas que con por para como pero sin sobre entre cuando donde
    esto esta este estos estas ese esa eso aqui ahi alla porque tambien solo muy mas menos
    hay ser estar fue son era hace hacer tiene tener todo toda todos todas cada otro otra
    quiero puedes ayudar ayuda favor porfavor bien mal algo nada mis mi tus tu sus su
    """.split()
)


# ---------------------------------------------------------------- paths/locks

def resolve_mem_dir() -> Path:
    override = os.environ.get("ISYMEM_DIR")
    if override:
        p = Path(override).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p.resolve()
    if REPO_MEM_LINK.is_dir():
        return REPO_MEM_LINK.resolve()
    if CLAUDE_MEM_DIR.is_dir():
        return CLAUDE_MEM_DIR
    CLAUDE_MEM_DIR.mkdir(parents=True, exist_ok=True)
    return CLAUDE_MEM_DIR


def ensure_links() -> Path:
    mem_dir = resolve_mem_dir()
    if os.environ.get("ISYMEM_DIR"):
        return mem_dir
    if not REPO_MEM_LINK.exists() and not REPO_MEM_LINK.is_symlink():
        REPO_MEM_LINK.parent.mkdir(parents=True, exist_ok=True)
        REPO_MEM_LINK.symlink_to(mem_dir)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg_link = CONFIG_DIR / "memory"
    if not cfg_link.exists() and not cfg_link.is_symlink():
        cfg_link.symlink_to(mem_dir)
    return mem_dir


def display_dir(mem_dir: Path) -> Path:
    if not os.environ.get("ISYMEM_DIR") and REPO_MEM_LINK.is_dir():
        return REPO_MEM_LINK
    return mem_dir


@contextlib.contextmanager
def bank_lock():
    """Serialize writers from all TUIs (read-modify-write of MEMORY.md)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%S%fZ")


def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def snapshot(path: Path) -> Path | None:
    """Copy the current version of a bank file to the history dir before changing it."""
    if not path.is_file():
        return None
    dest_dir = STATE_DIR / "history" / path.stem
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stamp()}.md"
    shutil.copy2(path, dest)
    return dest


def detect_author() -> str:
    env = os.environ.get("ISYMEM_AUTHOR", "").strip()
    if env:
        return env
    if os.environ.get("CLAUDECODE") == "1":
        return "claude_code"
    pid = os.getppid()
    for _ in range(8):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        names = [Path(a.decode(errors="replace")).name.lower() for a in raw[:2] if a]
        for name in names:
            base = re.sub(r"(\.js|\.mjs|\.py|\.exe)$", "", name)
            if base in KNOWN_HARNESSES:
                return base
        try:
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            break
        if pid <= 1:
            break
    return "unknown"


# ----------------------------------------------------------- parse / render

def normalize_slug(slug: str) -> str:
    slug = slug.strip()
    if slug.endswith(".md"):
        slug = slug[:-3]
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", slug).strip("-.").lower()
    if not slug or slug in ("memory", "index"):
        raise ValueError(f"Invalid memory slug: {slug!r}")
    return slug


INDEX_RX = re.compile(r"^\s*-\s*\[([^\]]+)\]\(([^)]+)\)\s*(?:—|--|-)\s*(.+)$")

# ---- graph model ---------------------------------------------------------
# Node  = a memory file. Every memory belongs to one project (metadata.project,
#         default "general"). Each project has a hub file `hub-<project>.md`.
# Edges = [[links]] in bodies + hub -> member (auto list in the hub).
# Anchor = one greppable fact per line, so agents read lines, not files:
#   - @todo#a1b2 #isycode 2026-11-17: text [[source-slug]]
GENERAL = "general"
HUB_PREFIX = "hub-"
OPEN_KINDS = ("todo", "fail")
ANCHOR_KINDS = ("fail", "todo", "rule", "state", "idea", "done", "fixed")
CLOSE_TO = {"todo": "done", "fail": "fixed"}
ANCHOR_RX = re.compile(
    r"^\s*-\s*@(?P<kind>[a-z]+)#(?P<id>[0-9a-f]{4,8})\s+#(?P<project>[a-z0-9_.-]+)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2}):?\s*(?P<text>.*)$"
)
LEDGER_HEADING = "## Ledger"
MEMBERS_BEGIN = "<!-- isymem:members begin (auto: do not edit) -->"
MEMBERS_END = "<!-- isymem:members end -->"
RULES_HEADING = "## Reglas y preferencias (siempre aplican)"
PROJECTS_HEADING = ("## Proyectos — tarjeta: `isymem <proyecto>` · fallos/pendientes: "
                    "`isymem <proyecto> --open` · todos: `isymem --open`")


def parse_memory_index(mem_dir: Path) -> list[tuple[str, str, str, str]]:
    """Return list of (title, filename, description, raw_line) from MEMORY.md."""
    index_file = mem_dir / "MEMORY.md"
    if not index_file.is_file():
        return []
    entries: list[tuple[str, str, str, str]] = []
    for line in index_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = INDEX_RX.match(line)
        if m:
            entries.append((m.group(1).strip(), m.group(2).strip(), m.group(3).strip(), line))
        else:
            entries.append(("", "", line.strip(), line))
    return entries


def split_frontmatter(text: str) -> tuple[list[str] | None, str]:
    """Return (frontmatter lines without the --- fences, body)."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        if text.endswith("\n---"):
            end = len(text) - 4
        else:
            return None, text
    fm = text[4:end].split("\n")
    body = text[end + 5:]
    return fm, body


def fm_get(fm: list[str], key: str) -> str | None:
    """Read `key` (top level) or `metadata.key` (two-space indented)."""
    rx = re.compile(rf"^(?:  )?{re.escape(key)}:\s*(.*)$")
    for line in fm:
        m = rx.match(line)
        if m:
            val = m.group(1).strip()
            if len(val) >= 2 and val[0] == val[-1] == '"':
                try:
                    return json.loads(val)
                except json.JSONDecodeError:
                    return val[1:-1]
            if len(val) >= 2 and val[0] == val[-1] == "'":
                return val[1:-1].replace("''", "'")
            return val
    return None


def fm_set(fm: list[str], key: str, value: str, *, nested: bool) -> list[str]:
    """Set a top-level or metadata.* key, keeping every other line verbatim."""
    prefix = "  " if nested else ""
    rx = re.compile(rf"^{prefix}{re.escape(key)}:")
    out = list(fm)
    for i, line in enumerate(out):
        if rx.match(line):
            out[i] = f"{prefix}{key}: {value}"
            return out
    if not nested:
        out.insert(0 if key == "name" else min(1, len(out)), f"{key}: {value}")
        return out
    if "metadata:" not in out:
        out.append("metadata:")
    idx = out.index("metadata:") + 1
    while idx < len(out) and out[idx].startswith("  "):
        idx += 1
    out.insert(idx, f"  {key}: {value}")
    return out


def yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def iso_now() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def find_secrets(text: str) -> list[str]:
    hits = []
    for label, rx in SECRET_PATTERNS:
        m = rx.search(text)
        if m:
            hits.append(f"{label} (…{m.group(0)[:6]}…)")
    return hits


class Memory:
    def __init__(self, path: Path, title: str = ""):
        self.path = path
        self.slug = path.stem
        self.text = path.read_text(encoding="utf-8", errors="replace")
        self.fm, self.body = split_frontmatter(self.text)
        self.title = title

    def get(self, key: str) -> str | None:
        return fm_get(self.fm, key) if self.fm is not None else None

    @property
    def mtype(self) -> str:
        return self.get("type") or "?"

    @property
    def description(self) -> str:
        return self.get("description") or ""

    def modified(self) -> dt.datetime:
        raw = self.get("modified")
        if raw:
            with contextlib.suppress(ValueError):
                return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.datetime.fromtimestamp(self.path.stat().st_mtime, dt.timezone.utc)

    def age_days(self) -> int:
        return max(0, (utc_now() - self.modified()).days)

    def links(self) -> list[str]:
        body = re.sub(r"<!--.*?-->", "", self.body, flags=re.DOTALL)  # format hints are not edges
        return sorted(set(re.findall(r"\[\[([^\]|#]+)", body)))

    @property
    def is_hub(self) -> bool:
        return self.slug.startswith(HUB_PREFIX)

    @property
    def project(self) -> str:
        if self.is_hub:
            return self.slug[len(HUB_PREFIX):]
        return (self.get("project") or GENERAL).lower()


def load_bank(mem_dir: Path) -> list[Memory]:
    titles = {fn: t for t, fn, _, _ in parse_memory_index(mem_dir) if fn}
    return [Memory(p, titles.get(p.name, "")) for p in sorted(mem_dir.glob("*.md")) if p.name != "MEMORY.md"]


def resolve_slug(mem_dir: Path, raw: str) -> Path | None:
    slug = raw.strip()
    target = mem_dir / (slug if slug.endswith(".md") else slug + ".md")
    if target.is_file():
        return target
    matches = [m for m in sorted(mem_dir.glob(f"*{slug.removesuffix('.md')}*.md")) if m.name != "MEMORY.md"]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Ambiguous slug '{raw}'. Candidates:", file=sys.stderr)
        for m in matches:
            print(f"  - {m.stem}", file=sys.stderr)
    else:
        print(f"Memory '{raw}' not found in {mem_dir} (try `isymem recall \"{raw}\"`)", file=sys.stderr)
    return None


# ------------------------------------------------------------------ commands

def cmd_list(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    index_file = mem_dir / "MEMORY.md"
    if not index_file.is_file():
        print("(MEMORY.md no existe aun)")
        return 0
    lines = [l for l in index_file.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    mtype = getattr(args, "type", None)
    if mtype:
        types = {m.path.name: m.mtype for m in load_bank(mem_dir)}
        lines = [l for l in lines if (mm := INDEX_RX.match(l)) and types.get(mm.group(2).strip()) == mtype]
    total = len(lines)
    limit = getattr(args, "limit", 0) or 0
    shown = lines[:limit] if limit > 0 else lines
    compact = getattr(args, "compact", False)
    if not compact:
        label = f", type={mtype}" if mtype else ""
        print(f"SHARED MEMORY ({display_dir(mem_dir)}/MEMORY.md — {total} entradas{label}):")
        print("  Antes de actuar: `isymem recall \"<tarea>\"` | Detalle: `isymem show <slug>` | Guardar: `isymem save <slug> ...`")
    for line in shown:
        print(line if compact else f"  {line}")
    if limit > 0 and total > limit:
        print(f"  (+{total - limit} entradas mas: `isymem list`)")
    return 0


def link_targets(bank: list[Memory]) -> dict[str, str]:
    """[[link]] text -> slug. Links may use the filename slug or the frontmatter name."""
    out = {m.slug: m.slug for m in bank}
    for m in bank:
        name = m.get("name")
        if name:
            out.setdefault(name, m.slug)
    return out


def _print_links(mem_dir: Path, mem: Memory) -> None:
    bank = load_bank(mem_dir)
    targets = link_targets(bank)
    out = []
    for link in mem.links():
        slug = targets.get(link)
        out.append(link if slug == link else f"{link} -> {slug}" if slug else f"{link} (no existe aun)")
    own = {mem.slug, mem.get("name") or mem.slug}
    backlinks = [o.slug for o in bank if o.slug != mem.slug and own & set(o.links())]
    if out or backlinks:
        print("\n# --- isymem ---")
        if out:
            print("# Enlaza a  : " + ", ".join(out))
        if backlinks:
            print("# Citada por: " + ", ".join(backlinks))


def cmd_show(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    slug = args.slug.strip()
    if slug.lower() in ("memory", "memory.md", "index"):
        target = mem_dir / "MEMORY.md"
    else:
        target = resolve_slug(mem_dir, slug)
        if target is None:
            return 1
    print(f"# File: {display_dir(mem_dir) / target.name}")
    if target.name != "MEMORY.md":
        mem = Memory(target)
        print(f"# type={mem.mtype} age={mem.age_days()}d author={mem.get('author') or '?'} "
              "(memoria fechada: verifica en disco antes de confiar en rutas/conteos/SHAs)")
    print(target.read_text(encoding="utf-8", errors="replace").rstrip())
    if target.name != "MEMORY.md" and not args.no_links:
        _print_links(mem_dir, Memory(target))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    q = args.query.strip()
    rx = re.compile(re.escape(q), re.IGNORECASE)
    hits = 0
    for md in sorted(mem_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        matched_lines = [(i, l.strip()) for i, l in enumerate(text.splitlines(), 1) if rx.search(l)]
        if matched_lines or rx.search(md.stem):
            hits += 1
            label = "MEMORY.md (index)" if md.name == "MEMORY.md" else f"{md.stem} ({md.name})"
            print(f"[{label}]")
            for ln, content in matched_lines[:6]:
                print(f"  L{ln}: {content[:140]}")
            if len(matched_lines) > 6:
                print(f"  ... (+{len(matched_lines) - 6} more matches)")
    if hits == 0:
        print(f"No matches for '{q}' in {display_dir(mem_dir)} (fuzzy: `isymem recall \"{q}\"`)")
        return 1
    return 0


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


# Danny writes tasks in Spanish; most memories are in English. Query-side gloss only.
ES_EN = {
    "matar": "kill", "mata": "kill", "proceso": "process", "procesos": "process",
    "memoria": "memory", "memorias": "memory", "borrar": "delete", "eliminar": "delete",
    "rama": "branch", "ramas": "branch", "subir": "push", "empujar": "push", "fusionar": "merge",
    "prueba": "test", "pruebas": "test", "error": "bug", "fallo": "failure", "colgado": "hang",
    "lento": "slow", "congelado": "freeze", "congela": "freeze", "corrida": "run", "correr": "run",
    "ejecutar": "run", "modelo": "model", "modelos": "model", "pesos": "weights", "motor": "engine",
    "token": "token", "contrasena": "secret", "clave": "key", "busqueda": "search", "buscar": "search",
    "repositorio": "repo", "publicar": "publish", "congelar": "freeze", "agente": "agent",
    "agentes": "agent", "puente": "bridge", "arrancar": "launch", "lanzador": "launcher",
    "juego": "game", "sonido": "audio", "pantalla": "screen", "telefono": "iphone",
    "inicio": "login", "sesion": "session", "suspender": "suspend", "bloqueo": "lock",
    "camara": "camera", "carga": "load", "rendimiento": "performance", "velocidad": "speed",
    "regla": "rule", "reglas": "rule", "evidencia": "evidence", "experimento": "experiment",
}


def _keys(text: str, *, gloss: bool = False) -> list[str]:
    out = []
    for tok in re.findall(r"[a-z0-9][a-z0-9_+]*", _fold(text).replace("-", " ").replace("/", " ")):
        if len(tok) < 3 or tok in STOPWORDS or tok.isdigit() and len(tok) < 4:
            continue
        out.append(tok[:6])  # crude stem: memory/memories, agente/agentes
        if gloss and tok in ES_EN:
            out.append(ES_EN[tok][:6])
    return out


def rank(mem_dir: Path, query: str) -> list[tuple[float, Memory]]:
    bank = load_bank(mem_dir)
    qkeys = set(_keys(query, gloss=True))
    if not qkeys or not bank:
        return []
    docs = []
    for m in bank:
        head = set(_keys(f"{m.slug} {m.title} {m.description}"))
        body: dict[str, int] = {}
        for k in _keys(m.body):
            body[k] = body.get(k, 0) + 1
        docs.append((m, head, body))
    n = len(docs)
    df = {k: sum(1 for _, h, b in docs if k in h or k in b) for k in qkeys}
    scored = []
    for m, head, body in docs:
        score = 0.0
        matched = 0
        for k in qkeys:
            if not df[k]:
                continue
            idf = math.log((n + 1) / (df[k] + 0.5))
            hit = 0.0
            if k in head:
                hit += 3.0
            if k in body:
                hit += min(body[k], 4) * 0.5
            if hit:
                matched += 1
                score += idf * hit
        if score > 0:
            # Prefer memories that cover more of the query, not one word many times.
            score *= 1 + matched / len(qkeys)
            scored.append((round(score, 2), m))
    scored.sort(key=lambda x: (-x[0], x[1].slug))
    # Drop the long tail of one-generic-word matches.
    floor = scored[0][0] * 0.2 if scored else 0
    return [s for s in scored if s[0] >= floor]


def cmd_recall(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    query = " ".join(args.query)
    hits = rank(mem_dir, query)[: args.limit]
    if not hits:
        print(f"RECALL: ninguna memoria aplica a {query!r} (si aprendes algo durable, `isymem save`).")
        return 1
    print(f"RECALL {query!r} — lee las que apliquen ANTES de actuar (`isymem show <slug>`):")
    for score, m in hits:
        print(f"  {score:6.2f}  {m.slug:<36} [{m.mtype}, {m.project}, {m.age_days()}d]  {m.description[:150]}")
    if args.show:
        for _, m in hits:
            print(f"\n==================== {m.slug} ====================")
            print(m.text.rstrip())
    return 0


def _existing_index_title(mem_dir: Path, filename: str) -> str | None:
    for title, fn, _, _ in parse_memory_index(mem_dir):
        if fn == filename:
            return title
    return None


def _write_index_entry(mem_dir: Path, filename: str, bullet: str | None) -> bool:
    """Replace (or append, or with bullet=None drop) the index line for filename."""
    index_file = mem_dir / "MEMORY.md"
    lines = index_file.read_text(encoding="utf-8", errors="replace").splitlines() if index_file.is_file() else []
    link_pat = re.compile(rf"^\s*-\s*\[[^\]]+\]\({re.escape(filename)}\)")
    out, found = [], False
    for line in lines:
        if link_pat.match(line):
            if not found and bullet is not None:
                out.append(bullet)
            found = True
        else:
            out.append(line)
    if not found and bullet is not None:
        # Hubs go at the end (projects section); plain memories before it.
        pos = next((i for i, l in enumerate(out) if l.startswith("## Proyectos")), None)
        if pos is None or filename.startswith(HUB_PREFIX):
            out.append(bullet)
        else:
            while pos > 0 and not out[pos - 1].strip():
                pos -= 1
            out.insert(pos, bullet)
    new_text = "\n".join(out) + "\n"
    if index_file.is_file() and index_file.read_text(encoding="utf-8", errors="replace") == new_text:
        return found
    snapshot(index_file)
    atomic_write(index_file, new_text)
    return found


def cmd_save(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    try:
        slug = normalize_slug(args.slug)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    filename = f"{slug}.md"
    target = mem_dir / filename
    author = args.author or detect_author()
    desc = " ".join((args.desc or "").split())
    name = " ".join((args.name or "").split())

    new_parts = [p.strip() for p in (args.body, f"**Why:** {args.why.strip()}" if args.why else "",
                                     f"**How to apply:** {args.how.strip()}" if args.how else "") if p and p.strip()]

    leaked = find_secrets("\n".join([desc, name, *new_parts]))
    if leaked:
        print("REFUSED: the text looks like it contains a secret: " + "; ".join(leaked), file=sys.stderr)
        print("Shared memory is read by every TUI. Describe where the secret lives instead of its value.", file=sys.stderr)
        return 2
    if args.type and args.type not in VALID_TYPES:
        print(f"ERROR: --type must be one of {', '.join(VALID_TYPES)}", file=sys.stderr)
        return 2

    with bank_lock():
        exists = target.is_file()
        if not exists and (not desc or not name):
            print("ERROR: a new memory needs --name and --desc.", file=sys.stderr)
            return 2

        if exists:
            mem = Memory(target)
            fm = mem.fm if mem.fm is not None else [f"name: {slug}", "metadata:"]
            body = mem.body if mem.fm is not None else mem.text
            if desc:
                fm = fm_set(fm, "description", yaml_str(desc), nested=False)
            if args.type:
                fm = fm_set(fm, "type", args.type, nested=True)
            if args.project:
                fm = fm_set(fm, "project", args.project, nested=True)
            if desc or args.type or new_parts:  # re-tagging the project alone keeps the age signal
                fm = fm_set(fm, "modified", iso_now(), nested=True)
                fm = fm_set(fm, "author", author, nested=True)
            if args.replace:
                if not new_parts:
                    print("ERROR: --replace needs --body/--why/--how.", file=sys.stderr)
                    return 2
                body = "\n" + "\n\n".join(new_parts) + "\n"
            elif new_parts:
                day = dt.datetime.now().astimezone().strftime("%Y-%m-%d")  # Danny's local date
                body = body.rstrip() + f"\n\n_Update {day} ({author}):_ " + "\n\n".join(new_parts) + "\n"
            desc = desc or mem.description
            snapshot(target)
            content = "---\n" + "\n".join(fm) + "\n---\n" + body
        else:
            parts = new_parts or [desc]
            content = (
                "---\n"
                f"name: {slug}\n"
                f"description: {yaml_str(desc)}\n"
                "metadata:\n"
                "  node_type: memory\n"
                f"  type: {args.type or 'project'}\n"
                + (f"  project: {args.project}\n" if args.project else "")
                + f"  author: {author}\n"
                f"  modified: {iso_now()}\n"
                "---\n\n" + "\n\n".join(parts) + "\n"
            )
        atomic_write(target, content)

        title = name or _existing_index_title(mem_dir, filename) or slug
        bullet = f"- [{title}]({filename}) — {desc}"
        saved = Memory(target)
        hub = mem_dir / f"{HUB_PREFIX}{saved.project}.md"
        if saved.project != GENERAL and not saved.is_hub and hub.is_file():
            # Reached through its project hub: keep it out of the always-loaded index.
            _write_index_entry(mem_dir, filename, None)
            bullet = f"(via {hub.name}; card: isymem {saved.project})"
        else:
            _write_index_entry(mem_dir, filename, bullet)
        refresh_hub_members(mem_dir, saved.project)
        synced = sync_inline_targets(mem_dir)

    print(f"MEMORY {'UPDATED' if exists else 'CREATED'}: {display_dir(mem_dir) / filename}"
          + (" (body replaced)" if exists and args.replace else " (update appended)" if exists and new_parts else ""))
    print(f"INDEX  ENTRY  : {bullet}")
    if synced:
        print(f"SYNCED        : {', '.join(synced)}")
    if len(desc) > 200:
        print(f"WARN: description is {len(desc)} chars; the index is loaded every session — aim for <150.")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    """Move a memory out of the bank (never deletes: AGENTS.md §0c)."""
    mem_dir = ensure_links()
    try:
        slug = normalize_slug(args.slug)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    filename = f"{slug}.md"
    target = mem_dir / filename
    with bank_lock():
        dest = None
        project = Memory(target).project if target.is_file() else GENERAL
        if target.is_file():
            arch = STATE_DIR / "archive"
            arch.mkdir(parents=True, exist_ok=True)
            dest = arch / f"{slug}.{stamp()}.md"
            shutil.move(str(target), dest)
        had_index = _write_index_entry(mem_dir, filename, None)
        if dest is None and not had_index:
            print(f"Memory '{slug}' not found.", file=sys.stderr)
            return 1
        refresh_hub_members(mem_dir, project)
        synced = sync_inline_targets(mem_dir)
    print(f"ARCHIVED slug={slug} file={'-> ' + str(dest) if dest else 'absent'} index={'removed' if had_index else 'absent'}")
    if dest:
        print(f"Restore: cp '{dest}' '{target}' && isymem save {slug} --name ... --desc ...")
    if synced:
        print(f"SYNCED: {', '.join(synced)}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    hist = STATE_DIR / "history"
    arch = STATE_DIR / "archive"
    if not args.slug:
        names = sorted(p.name for p in hist.iterdir()) if hist.is_dir() else []
        print(f"History dir: {hist} ({len(names)} files with snapshots)")
        for n in names:
            print(f"  {n}: {len(list((hist / n).glob('*.md')))} versions")
        archived = sorted(arch.glob("*.md")) if arch.is_dir() else []
        print(f"Archive dir: {arch} ({len(archived)} archived)")
        for p in archived:
            print(f"  {p.name}")
        return 0
    slug = args.slug.removesuffix(".md")
    versions = sorted((hist / slug).glob("*.md")) if (hist / slug).is_dir() else []
    archived = sorted(arch.glob(f"{slug}.*.md")) if arch.is_dir() else []
    if not versions and not archived:
        print(f"No history for '{slug}'.")
        return 1
    for p in versions:
        print(f"  snapshot  {p}")
    for p in archived:
        print(f"  archived  {p}")
    print("Diff: diff -u <snapshot> docs/agent/memory/<slug>.md   Restore: cp <snapshot> docs/agent/memory/<slug>.md")
    return 0


# ------------------------------------------- projects / anchors / graph

def project_map(bank: list[Memory]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in bank:
        p = out.setdefault(m.project, {"hub": None, "members": []})
        if m.is_hub:
            p["hub"] = m
        else:
            p["members"].append(m)
    return out


def anchor_pattern(kinds: tuple[str, ...] | None, project: str | None) -> str:
    """The same regex an agent can paste into ziggrep by hand."""
    k = "|".join(kinds) if kinds else "[a-z]+"
    proj = re.escape(project) if project else "[a-z0-9_.-]+"
    return rf"^\s*-\s*@({k})#[0-9a-f]{{4,8}}\s+#{proj}\s"


def grep_anchors(paths: list[Path], kinds: tuple[str, ...] | None = None,
                 project: str | None = None) -> list[tuple[Path, int, re.Match]]:
    """Find anchor lines with ziggrep (one line per fact, no whole-file reads)."""
    if not paths:
        return []
    pattern = anchor_pattern(kinds, project)
    rows: list[tuple[Path, int, re.Match]] = []
    exe = shutil.which("ziggrep")
    if exe:
        try:
            p = subprocess.run([exe, "-n", "-H", "-e", pattern, *map(str, paths)],
                               capture_output=True, text=True, timeout=20)
            if p.returncode in (0, 1):
                for line in p.stdout.splitlines():
                    fn, ln, text = line.split(":", 2)
                    m = ANCHOR_RX.match(text)
                    if m:
                        rows.append((Path(fn), int(ln), m))
                return rows
        except (OSError, subprocess.SubprocessError, ValueError):
            rows = []
    rx = re.compile(pattern)  # fallback when ziggrep is missing
    for path in paths:
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = ANCHOR_RX.match(line) if rx.match(line) else None
            if m:
                rows.append((path, i, m))
    return rows


def _open_counts(rows) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for _, _, m in rows:
        key = (m["project"], m["kind"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def neighbors(bank: list[Memory], project: str) -> dict[str, int]:
    """Other projects connected to `project` through [[links]] (either direction)."""
    targets = link_targets(bank)
    by_slug = {m.slug: m for m in bank}
    out: dict[str, int] = {}
    for m in bank:
        if m.is_hub:
            continue
        for link in m.links():
            t = by_slug.get(targets.get(link, ""))
            if t is None or t.is_hub:
                continue
            a, b = m.project, t.project
            if a != b and project in (a, b):
                other = b if a == project else a
                out[other] = out.get(other, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def render_members(members: list[Memory]) -> str:
    lines = [f"- [[{m.slug}]] [{m.mtype}] — {m.description[:140]}"
             for m in sorted(members, key=lambda m: m.modified(), reverse=True)]
    return f"{MEMBERS_BEGIN}\n" + ("\n".join(lines) if lines else "(sin memorias aún)") + f"\n{MEMBERS_END}"


def refresh_hub_members(mem_dir: Path, project: str) -> bool:
    """Rewrite the auto member list inside hub-<project>.md. Caller holds the lock."""
    hub_path = mem_dir / f"{HUB_PREFIX}{project}.md"
    if project == GENERAL or not hub_path.is_file():
        return False
    members = [m for m in load_bank(mem_dir) if m.project == project and not m.is_hub]
    block = render_members(members)
    text = hub_path.read_text(encoding="utf-8", errors="replace")
    rx = re.compile(rf"{re.escape(MEMBERS_BEGIN)}.*?{re.escape(MEMBERS_END)}", re.DOTALL)
    new = rx.sub(lambda _m: block, text) if rx.search(text) else text.rstrip() + f"\n\n## Memorias\n\n{block}\n"
    if new == text:
        return False
    snapshot(hub_path)
    atomic_write(hub_path, new)
    return True


def ensure_hub(mem_dir: Path, project: str, *, name: str = "", desc: str = "", author: str = "") -> Path:
    """Create hub-<project>.md (+ index line) if missing. Caller holds the lock."""
    hub_path = mem_dir / f"{HUB_PREFIX}{project}.md"
    if hub_path.is_file():
        return hub_path
    desc = desc or f"Proyecto {project}: memorias, fallos y pendientes"
    content = (
        "---\n"
        f"name: {HUB_PREFIX}{project}\n"
        f"description: {yaml_str(desc)}\n"
        "metadata:\n"
        "  node_type: hub\n"
        "  type: project\n"
        f"  project: {project}\n"
        f"  author: {author or detect_author()}\n"
        f"  modified: {iso_now()}\n"
        "---\n\n"
        f"{desc}. Tarjeta: `isymem {project}`; abiertos: `isymem {project} --open`.\n\n"
        f"{LEDGER_HEADING}\n\n"
        "<!-- una línea por hecho: - @fail|todo|rule|state|idea#id #proyecto AAAA-MM-DD: texto [[slug]] "
        f"(usa `isymem add {project} --todo \"...\"`) -->\n\n"
        f"## Memorias\n\n{render_members([])}\n"
    )
    atomic_write(hub_path, content)
    _write_index_entry(mem_dir, hub_path.name, f"- [{name or project}]({hub_path.name}) — {desc}")
    refresh_hub_members(mem_dir, project)
    return hub_path


def _new_anchor_id(bank: list[Memory], seed: str) -> str:
    used = set()
    for m in bank:
        used.update(re.findall(r"@[a-z]+#([0-9a-f]{4,8})\b", m.text))
    n = 0
    while True:
        aid = hashlib.sha1(f"{seed}{time.time_ns()}{n}".encode()).hexdigest()[:4]
        if aid not in used:
            return aid
        n += 1


def _insert_in_ledger(text: str, line: str) -> str:
    lines = text.split("\n")
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == LEDGER_HEADING)
    except StopIteration:
        # No ledger yet: put it before the member list (or at the end).
        pos = next((i for i, l in enumerate(lines) if l.startswith("## Memorias")), len(lines))
        lines[pos:pos] = [LEDGER_HEADING, "", line, ""]
        return "\n".join(lines)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    last = max((i for i in range(start + 1, end) if lines[i].strip()), default=None)
    if last is None:
        lines[start + 1:start + 1] = ["", line]
    else:
        lines.insert(last + 1, line)
    return "\n".join(lines)


def cmd_project(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    bank = load_bank(mem_dir)
    pm = project_map(bank)
    name = (args.name or "").lower() or None
    kinds: list[str] = []
    for k in args.kinds or []:
        kinds.extend({"open": OPEN_KINDS, "all": ANCHOR_KINDS, "done": ("done", "fixed")}.get(k, (k,)))
    kinds_t = tuple(dict.fromkeys(kinds)) or None
    if name and name not in pm:
        print(f"Proyecto '{name}' no existe. Proyectos: {', '.join(sorted(pm))}", file=sys.stderr)
        print(f"Crear: isymem hub {name} --name \"<Título>\" --desc \"<1 línea>\"", file=sys.stderr)
        return 1
    paths = [m.path for m in bank]

    if kinds_t:
        rows = grep_anchors(paths, kinds_t, name)
        if not rows:
            print(f"(sin anclas {'/'.join(kinds_t)} {('en ' + name) if name else ''})")
            return 1
        cur = None
        for path, ln, m in sorted(rows, key=lambda r: (r[2]["project"], r[2]["kind"], r[2]["date"])):
            if m["project"] != cur:
                cur = m["project"]
                print(f"[{cur}]")
            print(f"  @{m['kind']}#{m['id']} {m['date']}: {m['text'][:220]}  ({path.name}:{ln})")
        print("cerrar: isymem close <id> [--note \"...\"] · nueva: isymem add <proyecto> --todo|--fail \"...\"")
        return 0

    counts = _open_counts(grep_anchors(paths, OPEN_KINDS))
    if not name:
        print("PROYECTOS (tarjeta: `isymem <proyecto>` · abiertos: `isymem <proyecto> --open`):")
        for proj in sorted(pm, key=lambda p: (p == GENERAL, p)):
            hub = pm[proj]["hub"]
            hook = hub.description if hub else ("reglas generales (en el índice)" if proj == GENERAL
                                               else "(sin hub: isymem hub " + proj + " ...)")
            t, f = counts.get((proj, "todo"), 0), counts.get((proj, "fail"), 0)
            print(f"  {proj:<12} {len(pm[proj]['members']):>2} mem  todo {t:<2} fail {f:<2} {hook[:110]}")
        return 0

    p = pm[name]
    hub = p["hub"]
    t, f = counts.get((name, "todo"), 0), counts.get((name, "fail"), 0)
    print(f"PROYECTO {name} — {hub.description if hub else '(sin hub)'}")
    if hub:
        print(f"  hub: {display_dir(mem_dir) / hub.path.name}")
    print(f"  abiertos: {t} todo · {f} fail  → isymem {name} --open   (reglas: --rules · estado: --state)")
    print("  memorias (lee solo la que aplique: isymem show <slug>):")
    for m in sorted(p["members"], key=lambda m: m.modified(), reverse=True):
        print(f"    {m.slug:<36} [{m.mtype}, {m.age_days()}d] {m.description[:110]}")
    nb = neighbors(bank, name)
    if nb:
        print("  vecinos (grafo por [[links]]): " + ", ".join(f"{k}({v})" for k, v in nb.items()))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    project = args.project.lower()
    kind, text = next(((k, getattr(args, k)) for k in ("fail", "todo", "rule", "state", "idea") if getattr(args, k)))
    text = " ".join(text.split())
    leaked = find_secrets(text)
    if leaked:
        print("REFUSED: looks like a secret: " + "; ".join(leaked), file=sys.stderr)
        return 2
    if project == GENERAL:
        print("ERROR: las anclas van en un proyecto (isymem projects); una regla general es `isymem save`.", file=sys.stderr)
        return 2
    with bank_lock():
        created = not (mem_dir / f"{HUB_PREFIX}{project}.md").is_file()
        hub_path = ensure_hub(mem_dir, project, author=args.author)
        bank = load_bank(mem_dir)
        aid = _new_anchor_id(bank, project + text)
        day = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
        line = f"- @{kind}#{aid} #{project} {day}: {text}" + (f" [[{args.link}]]" if args.link else "")
        hub_text = hub_path.read_text(encoding="utf-8", errors="replace")
        fm, body = split_frontmatter(hub_text)
        if fm is not None:
            fm = fm_set(fm, "modified", iso_now(), nested=True)
            hub_text = "---\n" + "\n".join(fm) + "\n---\n" + body
        snapshot(hub_path)
        atomic_write(hub_path, _insert_in_ledger(hub_text, line))
        synced = sync_inline_targets(mem_dir) if created else []
    print(f"ADDED {line}")
    print(f"  en {display_dir(mem_dir) / hub_path.name}" + (" (hub nuevo)" if created else ""))
    if synced:
        print(f"SYNCED: {', '.join(synced)}")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    aid = args.id.lstrip("#").lower()
    with bank_lock():
        for m in load_bank(mem_dir):
            lines = m.text.split("\n")
            for i, line in enumerate(lines):
                am = ANCHOR_RX.match(line)
                if not am or am["id"] != aid:
                    continue
                if am["kind"] not in CLOSE_TO:
                    print(f"@{am['kind']}#{aid} no se cierra (solo todo→done, fail→fixed).", file=sys.stderr)
                    return 1
                day = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
                closed = CLOSE_TO[am["kind"]]
                lines[i] = (line.replace(f"@{am['kind']}#{aid}", f"@{closed}#{aid}", 1)
                            + f" ✓{day}" + (f" {' '.join(args.note.split())}" if args.note else ""))
                if args.note and find_secrets(args.note):
                    print("REFUSED: the note looks like a secret.", file=sys.stderr)
                    return 2
                snapshot(m.path)
                atomic_write(m.path, "\n".join(lines))
                print(f"CLOSED {lines[i].strip()}  ({m.path.name}:{i + 1})")
                return 0
    print(f"No hay ancla #{aid}. Busca: isymem --open", file=sys.stderr)
    return 1


def cmd_hub(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    project = args.project.lower()
    if project == GENERAL:
        print("ERROR: 'general' no tiene hub: sus memorias van directo en el índice.", file=sys.stderr)
        return 2
    desc = " ".join(args.desc.split())
    if find_secrets(desc + args.name):
        print("REFUSED: looks like a secret.", file=sys.stderr)
        return 2
    with bank_lock():
        hub_path = mem_dir / f"{HUB_PREFIX}{project}.md"
        if not hub_path.is_file():
            ensure_hub(mem_dir, project, name=args.name, desc=desc, author=args.author)
            status = "CREATED"
        else:
            hub = Memory(hub_path)
            fm = hub.fm or []
            if desc:
                fm = fm_set(fm, "description", yaml_str(desc), nested=False)
            fm = fm_set(fm, "modified", iso_now(), nested=True)
            snapshot(hub_path)
            atomic_write(hub_path, "---\n" + "\n".join(fm) + "\n---\n" + hub.body)
            title = args.name or _existing_index_title(mem_dir, hub_path.name) or project
            _write_index_entry(mem_dir, hub_path.name, f"- [{title}]({hub_path.name}) — {desc or hub.description}")
            refresh_hub_members(mem_dir, project)
            status = "UPDATED"
        synced = sync_inline_targets(mem_dir)
    print(f"HUB {status}: {display_dir(mem_dir) / hub_path.name}")
    if synced:
        print(f"SYNCED: {', '.join(synced)}")
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    bank = load_bank(mem_dir)
    targets = link_targets(bank)
    by_slug = {m.slug: m for m in bank}
    node = args.node.lower().removesuffix(".md")
    pm = project_map(bank)
    if node in pm and node not in by_slug:
        members = pm[node]["members"]
        print(f"GRAFO proyecto {node}: {len(members)} memorias")
        for m in members:
            outs = [targets[l] for l in m.links() if l in targets and targets[l] != m.slug]
            ext = [f"{s} ({by_slug[s].project})" for s in outs if by_slug[s].project != node and not by_slug[s].is_hub]
            ins = [o.slug for o in bank if not o.is_hub and o.project != node
                   and {m.slug, m.get("name") or m.slug} & set(o.links())]
            if ext or ins:
                print(f"  {m.slug}: " + "; ".join(filter(None, [
                    ("→ " + ", ".join(ext)) if ext else "", ("← " + ", ".join(ins)) if ins else ""])))
        nb = neighbors(bank, node)
        print("  vecinos: " + (", ".join(f"{k}({v})" for k, v in nb.items()) or "ninguno"))
        return 0
    target = by_slug.get(targets.get(node, node))
    if target is None:
        print(f"'{node}' no es memoria ni proyecto.", file=sys.stderr)
        return 1
    print(f"GRAFO {target.slug} [{target.mtype}] proyecto={target.project}")
    for link in target.links():
        s = targets.get(link)
        print(f"  → {link}" + (f" ({by_slug[s].project})" if s else " (no existe aún)"))
    own = {target.slug, target.get("name") or target.slug}
    for o in bank:
        if o.slug != target.slug and own & set(o.links()):
            print(f"  ← {o.slug} ({o.project})")
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    """Index = general rules + one line per project hub; project memories via their hub."""
    mem_dir = ensure_links()
    with bank_lock():
        bank = load_bank(mem_dir)
        pm = project_map(bank)
        if not args.dry_run:
            for proj in pm:
                refresh_hub_members(mem_dir, proj)
            bank = load_bank(mem_dir)
            pm = project_map(bank)
        index_file = mem_dir / "MEMORY.md"
        before = index_file.read_text(encoding="utf-8", errors="replace") if index_file.is_file() else ""
        raw = {fn: line for _, fn, _, line in parse_memory_index(mem_dir) if fn}
        order = list(raw)
        by_file = {m.path.name: m for m in bank}
        hubbed = {p for p, v in pm.items() if v["hub"] is not None}

        def line_for(m: Memory) -> str:
            return raw.get(m.path.name) or f"- [{m.title or m.slug}]({m.path.name}) — {m.description}"

        rules, hubs, kept_unhubbed = [], [], []
        ranked = sorted(by_file.values(), key=lambda m: order.index(m.path.name) if m.path.name in order else 10**6)
        for m in ranked:
            if m.is_hub:
                hubs.append(line_for(m))
            elif m.project == GENERAL:
                rules.append(line_for(m))
            elif m.project not in hubbed:
                kept_unhubbed.append(line_for(m))
        out = [RULES_HEADING, *rules, *kept_unhubbed, "", PROJECTS_HEADING, *hubs]
        new = "\n".join(out) + "\n"
        moved = sum(1 for m in bank if not m.is_hub and m.project in hubbed)
        print(f"index: {len(before.splitlines())} → {len(out)} lines, {len(before.encode())} → {len(new.encode())} bytes; "
              f"{len(rules)} general, {len(hubs)} hubs, {moved} memories reached via hubs"
              + (f", {len(kept_unhubbed)} project memories without hub (kept)" if kept_unhubbed else ""))
        if args.dry_run:
            print("(dry-run: nothing written)")
            return 0
        if new != before:
            snapshot(index_file)
            atomic_write(index_file, new)
        synced = sync_inline_targets(mem_dir)
    if synced:
        print(f"SYNCED: {', '.join(synced)}")
    return 0


# ------------------------------------------------------------- setup / sync

def render_inline_block(mem_dir: Path) -> str:
    index_file = mem_dir / "MEMORY.md"
    index = index_file.read_text(encoding="utf-8", errors="replace").strip() if index_file.is_file() else "(empty)"
    return f"{RULE_BODY}\n\n{INDEX_HEADING}\n\n{index}"


def _block_rx() -> re.Pattern[str]:
    return re.compile(rf"{re.escape(MARKER_BEGIN)}.*?{re.escape(MARKER_END)}\n?", re.DOTALL)


def _apply_marker_block(path: Path, block_content: str, *, dry_run: bool, undo: bool,
                        only_if_present: bool = False) -> str:
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    pat = _block_rx()
    if undo:
        if not pat.search(existing):
            return "unchanged (not present)"
        new_text = pat.sub("", existing).rstrip() + "\n"
        if not dry_run:
            atomic_write(path, new_text)
        return "removed"
    full_block = f"{MARKER_BEGIN}\n{block_content.rstrip()}\n{MARKER_END}\n"
    if pat.search(existing):
        new_text = pat.sub(lambda _m: full_block, existing)
        status = "updated" if new_text != existing else "already up-to-date"
    elif only_if_present:
        return "not installed"
    else:
        sep = ""
        if existing and not existing.endswith("\n"):
            sep = "\n\n"
        elif existing and not existing.endswith("\n\n"):
            sep = "\n"
        new_text = existing + sep + full_block
        status = "installed"
    if not dry_run and new_text != existing:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, new_text)
    return status


def sync_inline_targets(mem_dir: Path) -> list[str]:
    """Refresh the index copy inside inline TUI configs that already have the block."""
    if os.environ.get("ISYMEM_DIR") and not os.environ.get("ISYMEM_SYNC_TARGETS"):
        return []
    block = render_inline_block(mem_dir)
    done = []
    for label, path, inline in SETUP_TARGETS:
        if inline and path.is_file():
            with contextlib.suppress(OSError):
                if _apply_marker_block(path, block, dry_run=False, undo=False, only_if_present=True) == "updated":
                    done.append(label)
    return done


def cmd_sync(_args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    with bank_lock():
        block = render_inline_block(mem_dir)
        for label, path, inline in SETUP_TARGETS:
            if not inline:
                continue
            status = _apply_marker_block(path, block, dry_run=False, undo=False, only_if_present=True) \
                if path.is_file() else "not installed"
            print(f"  {label:<12} {path}: {status}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    if not args.dry_run and not args.undo:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write(CANONICAL_RULE_PATH, RULE_BODY + "\n")
        print(f"  canonical rule: {CANONICAL_RULE_PATH}")
    inline_block = render_inline_block(mem_dir)
    for label, target_path, inline in SETUP_TARGETS:
        if not target_path.parent.is_dir() and not args.undo:
            print(f"  {label:<12} skipped (harness not installed: {target_path.parent})")
            continue
        block = inline_block if inline else "@~/.config/isyco-memory/agents.md"
        status = _apply_marker_block(target_path, block, dry_run=args.dry_run, undo=args.undo)
        mode = "inline+index" if inline else "@import"
        print(f"  {label:<12} ({mode:<12}) {target_path}: {status}")
    return 0


# -------------------------------------------------------------------- doctor

def cmd_doctor(args: argparse.Namespace) -> int:
    mem_dir = ensure_links()
    errors: list[str] = []
    warns: list[str] = []
    infos: list[str] = []

    entries = parse_memory_index(mem_dir)
    index_file = mem_dir / "MEMORY.md"
    index_lines = len(entries)
    indexed = [fn for _, fn, _, _ in entries if fn]
    indexed_set = set(indexed)
    bank = load_bank(mem_dir)
    actual = {m.path.name for m in bank}

    print(f"Memory directory : {mem_dir}")
    if not os.environ.get("ISYMEM_DIR"):
        print(f"Repo symlink     : {REPO_MEM_LINK} -> {REPO_MEM_LINK.resolve() if REPO_MEM_LINK.exists() else 'MISSING'}")
    print(f"History/archive  : {STATE_DIR}")
    print(f"Index            : {len(indexed_set)} entries, {index_lines} lines, "
          f"{index_file.stat().st_size if index_file.is_file() else 0} bytes")
    print(f"Topic .md files  : {len(actual)}")

    for fn in sorted(f for f in indexed_set if not (mem_dir / f).is_file()):
        errors.append(f"index points to missing file: {fn}")
    pm = project_map(bank)
    via_hub = {m.path.name for p in pm.values() if p["hub"] is not None and p["hub"].path.name in indexed_set
               for m in p["members"]}
    for fn in sorted(actual - indexed_set - via_hub):
        warns.append(f"unindexed file, not reachable from any indexed hub: {fn}")
    for proj, p in sorted(pm.items()):
        if proj == GENERAL:
            continue
        if p["hub"] is None:
            infos.append(f"project '{proj}' has no hub (isymem hub {proj} --name ... --desc ...)")
            continue
        hub_text = p["hub"].text
        mm = re.search(rf"{re.escape(MEMBERS_BEGIN)}.*?{re.escape(MEMBERS_END)}", hub_text, re.DOTALL)
        if mm is None or mm.group(0) != render_members(p["members"]):
            warns.append(f"{p['hub'].path.name}: member list stale (run `isymem compact`)")
    seen_ids: dict[str, str] = {}
    for m in bank:
        for line in m.text.splitlines():
            am = ANCHOR_RX.match(line)
            if not am:
                if re.match(r"^\s*-\s*@[a-z]+#", line):
                    warns.append(f"{m.path.name}: malformed anchor: {line.strip()[:80]}")
                continue
            if am["kind"] not in ANCHOR_KINDS:
                warns.append(f"{m.path.name}: unknown anchor kind @{am['kind']}")
            if am["project"] not in pm:
                infos.append(f"{m.path.name}: anchor #{am['id']} tags unknown project #{am['project']}")
            if am["id"] in seen_ids:
                errors.append(f"duplicate anchor id #{am['id']} in {seen_ids[am['id']]} and {m.path.name}")
            seen_ids[am["id"]] = m.path.name
    for fn in sorted({f for f in indexed if indexed.count(f) > 1}):
        warns.append(f"duplicate index entries for {fn}")
    for _title, fn, _, raw in entries:
        if not fn:
            warns.append(f"index line is not a '- [Title](file.md) — hook' entry: {raw[:80]}")
        elif len(raw) > INDEX_ENTRY_SOFT_MAX:
            warns.append(f"long index line ({len(raw)} chars) for {fn}: move detail into the file")
    if index_lines > INDEX_LINE_BUDGET:
        errors.append(f"index has {index_lines} lines; Claude Code loads only the first {INDEX_LINE_BUDGET}")
    elif index_lines > INDEX_LINE_BUDGET * 0.8:
        warns.append(f"index at {index_lines}/{INDEX_LINE_BUDGET} lines: consolidate or archive")

    slugs = link_targets(bank)
    for m in bank:
        if m.fm is None:
            errors.append(f"{m.path.name}: no YAML frontmatter")
            continue
        if (m.get("name") or "") != m.slug:
            warns.append(f"{m.path.name}: frontmatter name {m.get('name')!r} != slug")
        if m.mtype not in VALID_TYPES:
            warns.append(f"{m.path.name}: metadata.type {m.mtype!r} not in {VALID_TYPES}")
        if not m.description:
            warns.append(f"{m.path.name}: empty description")
        leaked = find_secrets(m.text)
        if leaked:
            errors.append(f"{m.path.name}: possible secret: {'; '.join(leaked)}")
        dangling = [l for l in m.links() if l not in slugs]
        if dangling:
            infos.append(f"{m.slug}: [[links]] without a memory yet: {', '.join(dangling)}")
        if m.mtype == "project" and m.age_days() > args.stale_days:
            infos.append(f"{m.slug}: project state {m.age_days()}d old — verify or archive")

    if index_file.is_file():
        leaked = find_secrets(index_file.read_text(encoding="utf-8", errors="replace"))
        if leaked:
            errors.append(f"MEMORY.md: possible secret: {'; '.join(leaked)}")

    block = render_inline_block(mem_dir)
    for label, target_path, inline in SETUP_TARGETS:
        text = target_path.read_text(encoding="utf-8", errors="replace") if target_path.is_file() else ""
        if MARKER_BEGIN not in text:
            state = "MISSING" if target_path.parent.is_dir() else "n/a (harness not installed)"
        elif inline:
            m = _block_rx().search(text)
            in_sync = m is not None and m.group(0).rstrip("\n") == f"{MARKER_BEGIN}\n{block.rstrip()}\n{MARKER_END}"
            state = "INSTALLED, index in sync" if in_sync else "INSTALLED, STALE (run `isymem sync`)"
            if not in_sync:
                warns.append(f"{label}: inline index stale")
        else:
            state = "INSTALLED (@import; Claude auto-loads MEMORY.md)"
        print(f"TUI rule [{label:<10}]: {state} ({target_path})")

    for tag, items in (("ERROR", errors), ("WARN", warns), ("INFO", infos)):
        for it in items:
            print(f"{tag:<5} {it}")
    print(f"doctor: {len(errors)} error(s), {len(warns)} warning(s), {len(infos)} note(s)")
    return 1 if errors else 0


# -------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="isymem",
        description="Shared Multi-Harness Memory System (Claude/AGY/Codex/Grok/OpenCode/Qwen/ISyCode)",
    )
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="Print the shared MEMORY.md index")
    p.add_argument("--limit", type=int, default=0, help="Max entries to display (0 = all)")
    p.add_argument("--compact", action="store_true", help="Raw lines only")
    p.add_argument("--type", choices=VALID_TYPES, help="Only entries of this type")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("recall", help="Rank the memories relevant to a task description (run before acting)")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--show", action="store_true", help="Also print the full text of each hit")
    p.set_defaults(fn=cmd_recall)

    p = sub.add_parser("show", help="Read a memory topic file (<slug> or MEMORY)")
    p.add_argument("slug")
    p.add_argument("--no-links", action="store_true", help="Do not print links/backlinks")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("search", help="Literal, case-insensitive search across MEMORY.md and topic files")
    p.add_argument("query")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("save", help="Create a memory, or append an update to an existing one, and index it")
    p.add_argument("slug", help="Short kebab-case slug (e.g. agy-dashboard-ram)")
    p.add_argument("--name", default="", help="Short title for [Title](slug.md) (required when new)")
    p.add_argument("--desc", default="", help="One-line summary for MEMORY.md (required when new)")
    p.add_argument("--type", default=None, choices=VALID_TYPES, help="Category (new default: project; kept on update)")
    p.add_argument("--why", default="", help="Why this rule/state exists (incident or motivation)")
    p.add_argument("--how", default="", help="How and when agents should apply it")
    p.add_argument("--body", default="", help="Facts / context paragraph")
    p.add_argument("--replace", action="store_true", help="Rewrite the body instead of appending (old one is snapshotted)")
    p.add_argument("--project", default="", type=str.lower,
                   help="Project this memory belongs to (has hub-<project>.md); omit for general rules")
    p.add_argument("--author", default="", help="Author id (default: $ISYMEM_AUTHOR or detected harness)")
    p.set_defaults(fn=cmd_save)

    for name in ("archive", "remove"):
        p = sub.add_parser(name, help="Move a memory to the archive and drop its index line (never deletes)")
        p.add_argument("slug")
        p.set_defaults(fn=cmd_archive)

    p = sub.add_parser("history", help="List snapshots taken before overwrites, and archived memories")
    p.add_argument("slug", nargs="?")
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("doctor", help="Check index integrity, frontmatter, secrets, staleness, TUI rule setup")
    p.add_argument("--stale-days", type=int, default=30, help="Flag project memories older than this")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("sync", help="Refresh the index copy inside inline TUI configs (Codex, AGY, ...)")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("project", aliases=["projects", "p"],
                       help="Project card, or its anchor lines: isymem <project> --fails --todo (shortcut)")
    p.add_argument("name", nargs="?")
    for flags, const in ((("--fails", "--fail"), "fail"),
                         (("--todo", "--todos", "--pending", "--pendientes", "--pendings"), "todo"),
                         (("--rules",), "rule"), (("--state",), "state"), (("--ideas",), "idea"),
                         (("--done",), "done"), (("--open", "--abiertos"), "open"), (("--all",), "all")):
        p.add_argument(*flags, action="append_const", dest="kinds", const=const)
    p.set_defaults(fn=cmd_project)

    p = sub.add_parser("add", help="Append one anchor line to a project ledger (creates the hub if needed)")
    p.add_argument("project")
    g = p.add_mutually_exclusive_group(required=True)
    for k in ("fail", "todo", "rule", "state", "idea"):
        g.add_argument(f"--{k}", metavar="TEXT")
    p.add_argument("--link", default="", help="Related memory slug, stored as [[slug]]")
    p.add_argument("--author", default="")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("close", help="Close an anchor: todo→done, fail→fixed")
    p.add_argument("id")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("hub", help="Create or update a project hub (hub-<project>.md)")
    p.add_argument("project")
    p.add_argument("--name", default="")
    p.add_argument("--desc", default="")
    p.add_argument("--author", default="")
    p.set_defaults(fn=cmd_hub)

    p = sub.add_parser("graph", help="Edges of a memory or a project ([[links]] across projects)")
    p.add_argument("node")
    p.set_defaults(fn=cmd_graph)

    p = sub.add_parser("compact", help="Rebuild MEMORY.md as general rules + one line per project hub")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_compact)

    p = sub.add_parser("setup", help="Install shared memory instructions into global TUI configs")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--undo", action="store_true")
    p.set_defaults(fn=cmd_setup)

    return ap


def main() -> int:
    ap = build_parser()
    argv = sys.argv[1:]
    subcommands = {a for act in ap._subparsers._group_actions for a in act.choices}  # type: ignore[union-attr]
    if argv and argv[0] not in subcommands and argv[0] not in ("-h", "--help"):
        argv = ["project", *argv]  # isymem isycode --fails  ==  isymem project isycode --fails
    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        args.limit, args.compact, args.type = 0, False, None
        return cmd_list(args)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
