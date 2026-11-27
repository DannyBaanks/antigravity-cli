"""Tests for safe conversation deletion (backend + TUI button/hotkey/confirmation) in agy-dash."""
from __future__ import annotations

import curses
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash.procs as procs  # noqa: E402
import agy_dash.sessions as sessions  # noqa: E402
import agy_dash.tmuxctl as tmuxctl  # noqa: E402
import agy_dash.tui as tui  # noqa: E402
from test_tui import FakeWin  # noqa: E402


def _make_session(
    cid: str = "88fe707f-1111-2222-3333-444455556666",
    title: str = "Sesión de prueba",
    *,
    live: bool = False,
    running: bool = False,
    unknown: bool = False,
    not_fully_idle: bool = False,
    active_children: list[str] | None = None,
    hibernated: bool = False,
    transcript_busy: bool = False,
    dash_win_idx: str | None = None,
) -> sessions.AgySession:
    proc = (
        procs.ProcMemInfo(
            pid=43210,
            ppid=1,
            tty="pts/3",
            rss_kb=100_000,
            pss_kb=80_000,
            in_dash=True,
            active_children=list(active_children or []),
        )
        if live
        else None
    )
    db_status = (
        sessions.DB_STATUS_UNKNOWN
        if unknown
        else ("CASCADE_RUN_STATUS_RUNNING" if running else "CASCADE_RUN_STATUS_IDLE")
    )
    return sessions.AgySession(
        conversation_id=cid,
        title=title,
        preview="PONG",
        step_count=2,
        last_modified="2026-11-26 02:04:00",
        workspace="/tmp/swarm-bench-test",
        db_status=db_status,
        not_fully_idle=not_fully_idle,
        proc=proc,
        hibernated=hibernated,
        dash_win_idx=dash_win_idx,
        transcript_busy=transcript_busy,
    )


def _seed_data_dir(root: Path, cids: list[str]) -> Path:
    for sub in ("conversations", "brain", "annotations", "implicit", "presence", "heartbeats", "compacted", "accounts"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    db = root / "conversation_summaries.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT "",
            preview TEXT NOT NULL DEFAULT "",
            step_count INTEGER NOT NULL DEFAULT 0,
            last_modified_time TEXT NOT NULL DEFAULT "",
            workspace_uris TEXT NOT NULL DEFAULT "[]",
            status TEXT NOT NULL DEFAULT "",
            nesting_depth INTEGER NOT NULL DEFAULT 0,
            not_fully_idle NUMERIC NOT NULL DEFAULT 0,
            killed NUMERIC NOT NULL DEFAULT 0
        )"""
    )
    conn.execute("CREATE TABLE conversations_fts (conversation_id TEXT, title TEXT)")
    conn.execute("CREATE TABLE conversation_parents (conversation_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL)")
    hib = {}
    titles = {}
    conv_accts = {}
    for cid in cids:
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, '', 2, '2026-11-26 02:00:00', '[]', 'CASCADE_RUN_STATUS_IDLE', 0, 0, 0)",
            (cid, f"Title {cid[:8]}"),
        )
        conn.execute("INSERT INTO conversations_fts VALUES (?, ?)", (cid, f"Title {cid[:8]}"))
        conn.execute("INSERT INTO conversation_parents VALUES (?, 'parent-0')", (cid,))
        (root / "conversations" / f"{cid}.db").write_bytes(b"x" * 2048)
        (root / "conversations" / f"{cid}.db-wal").write_bytes(b"w" * 512)
        bdir = root / "brain" / cid / ".system_generated" / "logs"
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "transcript.jsonl").write_text('{"type":"USER_INPUT","content":"hola"}\n', encoding="utf-8")
        (root / "annotations" / f"{cid}.pbtxt").write_text("archived: false\n", encoding="utf-8")
        (root / "implicit" / f"{cid}.pb").write_bytes(b"imp")
        (root / "presence" / f"{cid}.lock").write_bytes(b"")
        (root / "heartbeats" / f"{cid[:16]}.json").write_text("{}", encoding="utf-8")
        (root / "heartbeats" / f"{cid}.autocompact").write_text("1", encoding="utf-8")
        (root / "compacted" / f"{cid[:8]}_handoff.md").write_text("# Vault handoff\n", encoding="utf-8")
        hib[cid] = {"conversation_id": cid, "title": f"Title {cid[:8]}"}
        titles[cid] = f"Custom {cid[:8]}"
        conv_accts[cid] = "pro3"
    conn.commit()
    conn.close()
    (root / "dashboard_hibernated.json").write_text(json.dumps(hib), encoding="utf-8")
    (root / "custom_titles.json").write_text(json.dumps(titles), encoding="utf-8")
    (root / "accounts" / "registry.json").write_text(json.dumps({"active_slot": "primary", "accounts": {}, "conv_accounts": conv_accts}), encoding="utf-8")
    return db


class DeleteSessionBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_scan_state = dict(procs.SCAN_STATE)
        procs.SCAN_STATE.update(failed=False, reason="")

    def tearDown(self) -> None:
        procs.SCAN_STATE.clear()
        procs.SCAN_STATE.update(self._orig_scan_state)

    def test_requires_confirmation_and_deletes_only_target_preserving_vault(self) -> None:
        cid_del = "88fe707f-1111-2222-3333-444455556666"
        cid_keep = "027ddc65-aaaa-bbbb-cccc-ddddeeeeffff"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = _seed_data_dir(root, [cid_del, cid_keep])
            sess = _make_session(cid_del, "Chat A Borrar", hibernated=True)

            ok0, msg0 = sessions.delete_session(sess, confirmed=False, fresh=False, data_dir=root)
            self.assertFalse(ok0)
            self.assertIn("Confirmación requerida", msg0)
            self.assertTrue((root / "conversations" / f"{cid_del}.db").exists())

            info = sessions.inspect_session_deletion(sess, fresh=False, data_dir=root)
            self.assertEqual(info["blocked_reason"], "")
            self.assertTrue(info["has_brain"])
            self.assertGreater(info["size_bytes"], 2048)

            closed: list[str] = []
            ok, msg = sessions.delete_session(
                sess,
                confirmed=True,
                fresh=False,
                on_close_tabs=lambda s: closed.append(s.conversation_id),
                data_dir=root,
            )
            self.assertTrue(ok, msg)
            self.assertEqual(closed, [cid_del])
            self.assertIn("Eliminada", msg)

            # Target artifacts removed
            self.assertFalse((root / "conversations" / f"{cid_del}.db").exists())
            self.assertFalse((root / "conversations" / f"{cid_del}.db-wal").exists())
            self.assertFalse((root / "brain" / cid_del).exists())
            self.assertFalse((root / "annotations" / f"{cid_del}.pbtxt").exists())
            self.assertFalse((root / "implicit" / f"{cid_del}.pb").exists())
            self.assertFalse((root / "presence" / f"{cid_del}.lock").exists())
            self.assertFalse((root / "heartbeats" / f"{cid_del[:16]}.json").exists())
            self.assertFalse((root / "heartbeats" / f"{cid_del}.autocompact").exists())

            # Vault handoff is preserved
            self.assertTrue((root / "compacted" / f"{cid_del[:8]}_handoff.md").exists())

            # Sibling conversation is untouched
            self.assertTrue((root / "conversations" / f"{cid_keep}.db").exists())
            self.assertTrue((root / "brain" / cid_keep).exists())

            # SQLite rows & dash metadata cleaned only for target
            conn = sqlite3.connect(str(db))
            self.assertEqual(
                [r[0] for r in conn.execute("SELECT conversation_id FROM conversation_summaries").fetchall()],
                [cid_keep],
            )
            self.assertEqual(
                [r[0] for r in conn.execute("SELECT conversation_id FROM conversations_fts").fetchall()],
                [cid_keep],
            )
            self.assertEqual(
                [r[0] for r in conn.execute("SELECT conversation_id FROM conversation_parents").fetchall()],
                [cid_keep],
            )
            conn.close()

            self.assertNotIn(cid_del, json.loads((root / "dashboard_hibernated.json").read_text()))
            self.assertIn(cid_keep, json.loads((root / "dashboard_hibernated.json").read_text()))
            self.assertNotIn(cid_del, json.loads((root / "custom_titles.json").read_text()))
            self.assertNotIn(cid_del, json.loads((root / "accounts" / "registry.json").read_text())["conv_accounts"])

    def test_blocks_unsafe_states_and_invalid_ids(self) -> None:
        cases = [
            ("running", _make_session(live=True, running=True), "turno activo"),
            ("transcript_busy", _make_session(live=True, transcript_busy=True), "turno activo"),
            ("bg_task", _make_session(live=True, not_fully_idle=True), "segundo plano"),
            ("active_children", _make_session(live=True, active_children=["bash"]), "segundo plano"),
            ("unknown", _make_session(live=True, unknown=True), "desconocido"),
            ("traversal", _make_session(cid="../escape"), "inválido"),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, sess, expected in cases:
                with self.subTest(case=name):
                    ok, msg = sessions.delete_session(sess, confirmed=True, fresh=False, data_dir=root)
                    self.assertFalse(ok)
                    self.assertIn(expected, msg)

            # Ancestor PID blocked
            ancestor_sess = _make_session(live=True)
            with mock.patch.object(procs, "is_ancestor_pid", return_value=True):
                ok, msg = sessions.delete_session(ancestor_sess, confirmed=True, fresh=False, data_dir=root)
            self.assertFalse(ok)
            self.assertIn("actual", msg)

            # Failed process scan blocked
            procs.SCAN_STATE.update(failed=True, reason="zig error")
            ok, msg = sessions.delete_session(_make_session(), confirmed=True, fresh=False, data_dir=root)
            self.assertFalse(ok)
            self.assertIn("Escaneo de procesos falló", msg)

    def test_idle_live_session_aborts_if_terminate_fails(self) -> None:
        cid = "99aa1122-1111-2222-3333-444455556666"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_data_dir(root, [cid])
            sess = _make_session(cid, "Idle Live", live=True)
            with mock.patch.object(procs, "is_ancestor_pid", return_value=False), \
                 mock.patch.object(procs, "terminate_verified", return_value=(False, "sigterm ignored")):
                ok, msg = sessions.delete_session(sess, confirmed=True, fresh=False, data_dir=root)
            self.assertFalse(ok)
            self.assertIn("no se pudo cerrar su proceso", msg)
            self.assertTrue((root / "conversations" / f"{cid}.db").exists())

    def test_close_session_tabs_never_kills_dash_window(self) -> None:
        with mock.patch.object(tmuxctl, "_get_dash_window_map", return_value={"cid-dash": ("0", "⚡DASH"), "cid-ok": ("2", "●chat")}), \
             mock.patch.object(tmuxctl, "_get_bg_window_map", return_value={"cid-ok": ("1", "●bg")}), \
             mock.patch.object(tmuxctl, "tmux_cmd") as tcmd:
            tmuxctl.close_session_tabs_for_deletion("cid-dash")
            tcmd.assert_not_called()
            tmuxctl.close_session_tabs_for_deletion("cid-ok")
            self.assertEqual(tcmd.call_count, 2)


class DeleteButtonTuiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_scan_state = dict(procs.SCAN_STATE)
        procs.SCAN_STATE.update(failed=False, reason="")
        for p in (
            mock.patch.object(tui.curses, "color_pair", side_effect=lambda n: n << 8),
            mock.patch.object(tui.curses, "flushinp"),
            mock.patch.object(tui.Dashboard, "_prepare_frame", lambda self, now: None),
            mock.patch.object(tui._compact, "list_compacted_vault", return_value=[]),
            mock.patch.object(tui._sessions, "transcript_sig", return_value=(0, 0)),
            mock.patch.object(tui._sessions, "zig_tail_transcript", return_value=[]),
            mock.patch.object(tui._compact, "load_nvapi_config", return_value={}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self) -> None:
        procs.SCAN_STATE.clear()
        procs.SCAN_STATE.update(self._orig_scan_state)

    def test_uppercase_D_opens_delete_popup_instead_of_detaching(self) -> None:
        win = FakeWin(30, 130)
        d = tui.Dashboard(win)
        s1 = _make_session("88fe707f-1111-2222-3333-444455556666", "Sesión 88fe707f")
        d.sessions = [s1]
        with mock.patch.object(tui._tmuxctl, "tmux_cmd") as tcmd:
            d.handle_key(ord("D"))
        tcmd.assert_not_called()
        self.assertIsNotNone(d.pending_delete_sess)
        self.assertEqual(d._view_name(), "delete_confirm")

        # Detach now lives on uppercase Q (outside delete_confirm)
        d.handle_key(27)  # Esc cancels popup
        with mock.patch.object(tui._tmuxctl, "tmux_cmd") as tcmd:
            d.handle_key(ord("Q"))
        tcmd.assert_called_once_with("detach-client")

    def test_delete_popup_arrows_enter_and_mouse_buttons(self) -> None:
        win = FakeWin(30, 130)
        d = tui.Dashboard(win)
        s1 = _make_session("88fe707f-1111-2222-3333-444455556666", "Sesión 88fe707f")
        s2 = _make_session("027ddc65-1111-2222-3333-444455556666", "Sesión 027ddc65")
        d.sessions = [s1, s2]
        with mock.patch.object(d, "cached_context", return_value={"level": "OK", "pct": 2.0, "used_tokens": 500}):
            d.draw(now=1000.0)
            del_btn = next(b for b in d.cmd_buttons if b.action_id == "delete")
            bx = d.panel_x + del_btn.col_offset + 1
            by = d.panel_btn_y + del_btn.row_offset

            # Click Eliminar button -> opens centered popup with focus on Cancelar (0)
            with mock.patch.object(tui.curses, "getmouse", return_value=(0, bx, by, 0, curses.BUTTON1_CLICKED)), \
                 mock.patch.object(tui._sessions, "delete_session") as rm:
                d.handle_mouse()
            rm.assert_not_called()
            self.assertIsNotNone(d.pending_delete_sess)
            self.assertEqual(d.pending_delete_sess["focus"], 0)

            d.draw(now=1001.0)
            rendered = "\n".join(t for _, _, t in win.writes)
            self.assertIn("Confirmar eliminación", rendered)
            self.assertIn("Cancelar", rendered)
            self.assertEqual(len(d.popup_hits), 2)

            # Default Enter on focus=0 (Cancelar) cancels without deleting
            with mock.patch.object(tui._sessions, "delete_session") as rm:
                d.handle_key(10)
            rm.assert_not_called()
            self.assertIsNone(d.pending_delete_sess)

            # Re-open with 'd', press Right arrow -> focus=1 (Eliminar), Left -> focus=0, Right -> focus=1, Enter -> deletes
            d.handle_key(ord("d"))
            d.handle_key(curses.KEY_RIGHT)
            self.assertEqual(d.pending_delete_sess["focus"], 1)
            d.handle_key(curses.KEY_LEFT)
            self.assertEqual(d.pending_delete_sess["focus"], 0)
            d.handle_key(curses.KEY_RIGHT)
            self.assertEqual(d.pending_delete_sess["focus"], 1)
            with mock.patch.object(tui._sessions, "delete_session", return_value=(True, "🗑️ Eliminada 'Sesión 88fe707f'.")) as rm:
                d.handle_key(10)
            rm.assert_called_once()
            self.assertTrue(rm.call_args.kwargs["confirmed"])
            self.assertEqual([s.conversation_id for s in d.sessions], [s2.conversation_id])
            self.assertIsNone(d.pending_delete_sess)
            self.assertTrue(d.force_scan)

            # Mouse click on Cancelar and Eliminar inside popup
            d.handle_key(ord("d"))
            d.draw(now=1002.0)
            cy, cx0, _, _ = next(h for h in d.popup_hits if h[3] == "cancel")
            with mock.patch.object(tui.curses, "getmouse", return_value=(0, cx0 + 1, cy, 0, curses.BUTTON1_CLICKED)), \
                 mock.patch.object(tui._sessions, "delete_session") as rm:
                d.handle_mouse()
            rm.assert_not_called()
            self.assertIsNone(d.pending_delete_sess)

            d.handle_key(ord("d"))
            d.draw(now=1003.0)
            dy, dx0, _, _ = next(h for h in d.popup_hits if h[3] == "confirm")
            with mock.patch.object(tui.curses, "getmouse", return_value=(0, dx0 + 1, dy, 0, curses.BUTTON1_CLICKED)), \
                 mock.patch.object(tui._sessions, "delete_session", return_value=(True, "🗑️ Eliminada 'Sesión 027ddc65'.")) as rm:
                d.handle_mouse()
            rm.assert_called_once()
            self.assertEqual(d.sessions, [])
            self.assertIsNone(d.pending_delete_sess)

    def test_ctrl_d_multi_select_mouse_and_batch_delete(self) -> None:
        win = FakeWin(30, 130)
        d = tui.Dashboard(win)
        s_run = _make_session("11111111-1111-2222-3333-444455556666", "Activa", live=True, running=True)
        s_a = _make_session("22222222-1111-2222-3333-444455556666", "Swarm A")
        s_b = _make_session("33333333-1111-2222-3333-444455556666", "Swarm B")
        d.sessions = [s_run, s_a, s_b]
        with mock.patch.object(d, "cached_context", return_value={"level": "OK", "pct": 2.0, "used_tokens": 500}):
            # Ctrl+D enters multi_select mode
            d.handle_key(4)
            self.assertEqual(d._view_name(), "multi_select")
            self.assertEqual(d.pending_multi_select, set())

            # Running session cannot be marked
            d.cursor = 0
            d.handle_key(32)  # Space
            self.assertEqual(d.pending_multi_select, set())
            self.assertIn("No se marca", d.status_banner)

            # Space marks s_a, draw shows checkboxes, mouse click marks s_b
            d.cursor = 1
            d.handle_key(32)
            self.assertEqual(d.pending_multi_select, {s_a.conversation_id})
            d.draw(now=1000.0)
            rendered = "\n".join(t for _, _, t in win.writes)
            self.assertIn("[✓]", rendered)
            self.assertIn("[·]", rendered)

            row_sb = next(y for y, idx in d.row_y_to_session_idx.items() if idx == 2)
            with mock.patch.object(tui.curses, "getmouse", return_value=(0, 5, row_sb, 0, curses.BUTTON1_CLICKED)):
                d.handle_mouse()
            self.assertEqual(d.pending_multi_select, {s_a.conversation_id, s_b.conversation_id})

            # 'a' toggles all eligible off, then back on (never includes s_run)
            d.handle_key(ord("a"))
            self.assertEqual(d.pending_multi_select, set())
            d.handle_key(ord("a"))
            self.assertEqual(d.pending_multi_select, {s_a.conversation_id, s_b.conversation_id})

            # Enter opens confirmation popup for both
            d.handle_key(10)
            self.assertEqual(d._view_name(), "delete_confirm")
            self.assertEqual(len(d.pending_delete_sess["items"]), 2)
            d.draw(now=1001.0)
            popup_txt = "\n".join(t for _, _, t in win.writes)
            self.assertIn("(2 chats)", popup_txt)

            # Right arrow + Enter confirms batch deletion
            d.handle_key(curses.KEY_RIGHT)
            with mock.patch.object(tui._sessions, "delete_session", return_value=(True, "ok")) as rm:
                d.handle_key(10)
            self.assertEqual(rm.call_count, 2)
            self.assertEqual([s.conversation_id for s in d.sessions], [s_run.conversation_id])
            self.assertIn("Eliminadas 2 de 2", d.status_banner)

    def test_supr_key_also_triggers_delete_and_blocks_while_compacting(self) -> None:
        win = FakeWin(30, 130)
        d = tui.Dashboard(win)
        s1 = _make_session("88fe707f-1111-2222-3333-444455556666", "Compactando")
        d.sessions = [s1]
        d.compact_jobs[s1.conversation_id] = {"status": "running", "title": s1.title, "started": 0.0}
        d.handle_key(curses.KEY_DC)
        self.assertIsNone(d.pending_delete_sess)
        self.assertIn("compactando", d.status_banner)

        d.compact_jobs.clear()
        d.handle_key(curses.KEY_DC)
        self.assertIsNotNone(d.pending_delete_sess)


if __name__ == "__main__":
    unittest.main()
