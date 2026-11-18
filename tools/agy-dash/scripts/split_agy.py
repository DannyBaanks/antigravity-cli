#!/usr/bin/env python3
"""Deterministic split of agy_dashboard.py into the agy_dash package (Phase 1).

Moves top-level statements verbatim into modules; the only text edits are:
  * cross-module references to top-level names become ``<module>.<name>``
    (late binding: one patch on the owner module reaches every caller);
  * ``Path(__file__).resolve()`` becomes ``paths.SELF_PATH`` (the shim script,
    which is what tabs/hooks must re-exec), and SCRIPT_DIR is anchored one
    level up so it still points at tools/agy-dash/.
Prints every collision/ambiguity it finds; refuses to write if any.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2])  # tools/agy-dash dir of the worktree
text = SRC.read_text()
lines = text.splitlines(keepends=True)
tree = ast.parse(text)

MODULES = ["paths", "procs", "sessions", "tmuxctl", "accounts", "compact", "mirror", "tui", "cli"]

BY_NAME = {
    "procs": """ensure_zig_core enable_ksm_memory_merge ProcMemInfo is_ancestor_pid proc_start_ticks
        _proc_is_agy terminate_verified zig_scan""",
    "sessions": """AgySession refresh_session transcript_path_for _TRANSCRIPT_BUSY_CACHE
        is_transcript_turn_active transcript_sig zig_tail_transcript print_instant_zig_preview
        load_hibernated_registry save_hibernated_registry parse_workspace_uri load_custom_titles
        save_custom_title _all_summaries_dbs load_sessions hibernate_session _dup_kill_ok
        deduplicate_session_procs close_stale_dash_duplicates""",
    "tmuxctl": """_get_tmux_dash_server_pids _get_window_map_for_session _get_dash_tty_map
        _get_dash_window_map _get_bg_window_map sh_quote tmux_dquote write_tmux_config tmux_cmd
        ensure_dash_window_zero _short_tab_title _find_dash_window_idx _select_dash_window
        _find_tmux_window_by_conv _replace_tmux_pane_with_wake_stub open_session_in_dashboard_tab
        launch_account_login_tab _SPINNER_FRAMES _TOOL_LABELS session_live_activity
        format_led_tab_title _is_led_generated_name sync_led_tmux_tabs _ensure_bg_session
        close_tab_safely""",
    "accounts": """_SHARED_AGY_DATA_ITEMS _SHARED_GEMINI_ITEMS _SHARED_HOME_ITEMS _EMAIL_RE _ANSI_RE
        validate_slot_name _ensure_symlink ensure_isolated_home extract_email_from_cli_log
        load_accounts_registry save_accounts_registry model_to_quota_family _bucket_family
        resolve_model_arg parse_usage_output build_agy_env refresh_account_quota compute_quota_alert
        choose_baton_target _profile_cred_domain baton_preflight exec_agy_optimized
        baton_pass_session _print_accounts_table _active_pids_for_slot inspect_account_removal
        diverged_shared_items rescue_diverged_shared purge_isolated_slot_dir remove_account_slot""",
    "compact": """_NEMOTRON_CANDIDATE_MODELS mask_api_key load_nvapi_config save_nvapi_config
        estimate_session_tokens should_offer_compaction build_compaction_digest
        _discover_nvapi_nemotron_model compact_conversation_with_nvapi open_compacted_session_in_tab
        parse_handoff_metadata get_vault_handoff list_compacted_vault open_vault_entry_in_tab
        delete_vault_handoff _context_window_for_model measure_session_context
        evaluate_context_heartbeat respawn_compacted_session_in_place reload_session_tab
        detect_caller_conversation_id _write_compact_status""",
    "mirror": "latest_active_task_log run_live_mirror_curses",
    "cli": "KEYS_SHEET _keys_block",
    "tui": "CommandButton build_command_buttons hit_test_button _ram_bar _prompt_curses_text run_curses_tui",
}
OWNER_HINT = {n: m for m, s in BY_NAME.items() for n in s.split()}

docstring_node = tree.body[0]
assert isinstance(docstring_node, ast.Expr)
import_nodes = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
first_def = next(n for n in tree.body if not isinstance(n, (ast.Expr, ast.Import, ast.ImportFrom)))
main_guard = tree.body[-1]
assert isinstance(main_guard, ast.If)


def node_name(n: ast.stmt) -> str:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return n.name
    if isinstance(n, ast.Assign):
        assert len(n.targets) == 1 and isinstance(n.targets[0], ast.Name), ast.dump(n)
        return n.targets[0].id
    if isinstance(n, ast.AnnAssign):
        return n.target.id
    raise SystemExit(f"unexpected top-level stmt at line {n.lineno}: {type(n).__name__}")


body = [n for n in tree.body if n is not docstring_node and n not in import_nodes and n is not main_guard]
items = []  # (name, module, start_line_idx, end_line_idx, node)
prev_end = first_def.lineno - 1  # 0-based index where previous chunk ended
# comments right after imports belong to first def
prev_end = max(n.end_lineno for n in import_nodes)
for n in body:
    name = node_name(n)
    if name in OWNER_HINT:
        mod = OWNER_HINT[name]
    elif n.lineno <= 107:
        mod = "paths"
    elif name.startswith("cmd_") or name in ("build_parser", "main"):
        mod = "cli"
    else:
        raise SystemExit(f"no owner for {name} (line {n.lineno})")
    start = prev_end  # include blank/comment lines before this node
    end = n.end_lineno
    items.append((name, mod, start, end, n))
    prev_end = end

OWNER = {name: mod for name, mod, *_ in items}
assert len(OWNER) == len(items), "duplicate top-level names"
unused = set(OWNER_HINT) - set(OWNER)
if unused:
    raise SystemExit(f"hint names not found: {unused}")
IMPORTED = set()
for n in import_nodes:
    for a in n.names:
        IMPORTED.add((a.asname or a.name).split(".")[0])
clash = IMPORTED & set(OWNER)
if clash:
    raise SystemExit(f"top-level names shadow imports: {clash}")

# ---- local-binding collection (over-approximate: missed rewrite => ruff F821, loud) ----
def bound_names(fn: ast.AST) -> set[str]:
    out: set[str] = set()
    for x in ast.walk(fn):
        if isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)):
            out.add(x.id)
        elif isinstance(x, ast.arg):
            out.add(x.arg)
        elif isinstance(x, ast.ExceptHandler) and x.name:
            out.add(x.name)
        elif isinstance(x, (ast.Import, ast.ImportFrom)):
            for a in x.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and x is not fn:
            out.add(x.name)
        elif isinstance(x, (ast.MatchAs, ast.MatchStar)) and x.name:
            out.add(x.name)
        elif isinstance(x, (ast.Global, ast.Nonlocal)):
            out.update(x.names)
    return out


edits: dict[str, list[tuple[int, int, int, str]]] = {}  # name -> [(line, col, endcol, replacement)]
collisions: list[str] = []
import_time_xrefs: list[str] = []
needs: dict[str, set[str]] = {m: set() for m in MODULES}
FILE_CALL = "Path(__file__).resolve()"


def visit(node: ast.AST, scopes: list[set[str]], my_mod: str, item: str, in_func: bool) -> None:
    # defaults/decorators/annotations of a def are evaluated in the enclosing scope
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = node.args
        for d in a.defaults + [d for d in a.kw_defaults if d is not None]:
            visit(d, scopes, my_mod, item, in_func)
        if not isinstance(node, ast.Lambda):
            for d in node.decorator_list:
                visit(d, scopes, my_mod, item, in_func)
            for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
                if arg is not None and arg.annotation is not None:
                    visit(arg.annotation, scopes, my_mod, item, in_func)
            if node.returns is not None:
                visit(node.returns, scopes, my_mod, item, in_func)
        inner = scopes + [bound_names(node)]
        body = node.body if isinstance(node.body, list) else [node.body]
        for s in body:
            visit(s, inner, my_mod, item, True)
        return
    if isinstance(node, ast.ClassDef):
        for d in node.decorator_list + node.bases:
            visit(d, scopes, my_mod, item, in_func)
        for s in node.body:
            visit(s, scopes, my_mod, item, in_func)  # class body: names resolve to globals here
        return
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        nm = node.id
        if nm in OWNER and OWNER[nm] != my_mod:
            if any(nm in sc for sc in scopes):
                collisions.append(f"{item}: local '{nm}' shadows top-level of {OWNER[nm]} (line {node.lineno})")
            else:
                edits.setdefault(item, []).append((node.lineno, node.col_offset, node.end_col_offset, f"_{OWNER[nm]}.{nm}"))
                needs[my_mod].add(OWNER[nm])
                if not in_func:
                    import_time_xrefs.append(f"{item} ({my_mod}) uses {OWNER[nm]}.{nm} at import time, line {node.lineno}")
        elif nm in OWNER and any(nm in sc for sc in scopes) and OWNER[nm] == my_mod:
            pass
        return
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and not scopes and node.id in OWNER and OWNER[node.id] != my_mod:
        collisions.append(f"{item}: assigns top-level name {node.id} owned elsewhere")
    for ch in ast.iter_child_nodes(node):
        visit(ch, scopes, my_mod, item, in_func)


def nm_is_cross(node: ast.Name) -> bool:
    return node.id in OWNER


for name, mod, start, end, n in items:
    visit(n, [], mod, name, False)
    for x in ast.walk(n):
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            bad = {f"_{m}" for m in MODULES} & bound_names(x)
            if bad:
                collisions.append(f"{name}: local binds module alias {bad}")

if collisions:
    print("COLLISIONS:\n  " + "\n  ".join(collisions))
    raise SystemExit(2)

# ---- emit ----
header_imports = "".join(lines[import_nodes[0].lineno - 1 : import_nodes[-1].end_lineno])
docstring_src = "".join(lines[docstring_node.lineno - 1 : docstring_node.end_lineno])

chunks: dict[str, list[str]] = {m: [] for m in MODULES}
file_subs = 0
for name, mod, start, end, n in items:
    seg = lines[start:end]
    # apply edits line by line, right-to-left
    for (ln, c0, c1, rep) in sorted(edits.get(name, []), key=lambda e: (e[0], -e[1])):
        pass
    by_line: dict[int, list[tuple[int, int, str]]] = {}
    for (ln, c0, c1, rep) in edits.get(name, []):
        by_line.setdefault(ln, []).append((c0, c1, rep))
    out = []
    for i, l in enumerate(seg):
        ln = start + i + 1
        if ln in by_line:
            b = l.encode()
            for c0, c1, rep in sorted(by_line[ln], reverse=True):
                b = b[:c0] + rep.encode() + b[c1:]
            l = b.decode()
        if FILE_CALL in l:
            if mod == "paths":
                l = l.replace("Path(__file__).resolve().parent", "Path(__file__).resolve().parent.parent")
            else:
                l = l.replace(FILE_CALL, "_paths.SELF_PATH")
                needs[mod].add("paths")
            file_subs += 1
        out.append(l)
    chunks[mod].append("".join(out))

chunks["paths"].append('\nSELF_PATH = SCRIPT_DIR / "agy_dashboard.py"  # the shim; tabs/hooks re-exec this file\n')

pkg = OUT / "agy_dash"
pkg.mkdir(exist_ok=True)
for m in MODULES:
    sib = sorted(needs[m] - {m})
    sib_line = "".join(f"from . import {x} as _{x}\n" for x in sib)
    doc = f'"""agy_dash.{m} — split verbatim from agy_dashboard.py @ f255869 (Phase 1, no behaviour change)."""\n'
    src = doc + header_imports + "\n" + sib_line + "".join(chunks[m]).rstrip() + "\n"
    (pkg / f"{m}.py").write_text(src)

OWNER["SELF_PATH"] = "paths"
owner_json = json.dumps(OWNER, indent=0, sort_keys=True)
(pkg / "__init__.py").write_text(
    '"""agy_dash — agy-dash split into modules. ``paths`` is imported first: other modules read\n'
    'its constants at import time (default args, dataclass fields)."""\n'
    "from . import paths  # noqa: F401  (must be first)\n"
    "from . import procs, sessions, tmuxctl, accounts, compact, mirror, tui, cli  # noqa: F401,E401\n\n"
    f"OWNER: dict[str, str] = {owner_json}\n"
)

shim = (
    "#!/usr/bin/env python3\n"
    + docstring_src
    + '''from __future__ import annotations

# Shim: the code lives in the agy_dash package next to this file. This module forwards every
# attribute read/write/delete to the owning submodule, so `ad.BRAIN_DIR = x` and
# `mock.patch.object(ad, "refresh_session")` keep reaching all callers. Tabs, hooks,
# ~/.local/bin/agy-dash and agy-limited still exec this file (paths.SELF_PATH).
import importlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash  # noqa: E402

_MODS = {m: importlib.import_module(f"agy_dash.{m}") for m in set(agy_dash.OWNER.values())}
_OWNER = {name: _MODS[m] for name, m in agy_dash.OWNER.items()}


class _ForwardingModule(types.ModuleType):
    def __getattr__(self, name: str):
        mod = _OWNER.get(name)
        if mod is None:
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
        return getattr(mod, name)

    def __setattr__(self, name: str, value) -> None:
        mod = _OWNER.get(name)
        if mod is not None:
            setattr(mod, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        mod = _OWNER.get(name)
        if mod is not None and name not in self.__dict__:
            delattr(mod, name)
        else:
            super().__delattr__(name)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(_OWNER))


sys.modules[__name__].__class__ = _ForwardingModule

if __name__ == "__main__":
    raise SystemExit(_OWNER["main"].main())
'''
)
(OUT / "agy_dashboard.py").write_text(shim)

print(f"items={len(items)} edits={sum(len(v) for v in edits.values())} file_subs={file_subs}")
for m in MODULES:
    print(f"  {m:9s} {sum(1 for _, mm, *_ in items if mm == m):3d} items  needs={sorted(needs[m] - {m})}")
print("IMPORT-TIME CROSS REFS:" if import_time_xrefs else "import-time cross refs: none")
for x in import_time_xrefs:
    print("  " + x)
