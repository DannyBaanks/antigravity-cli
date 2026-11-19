from __future__ import annotations

import curses
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash.tui as tui  # noqa: E402
from agy_dash import termtext as tt  # noqa: E402
from agy_dash import sessions as _sessions  # noqa: E402

# Todas las acciones que existían antes del rediseño del panel (execute_action).
ORIGINAL_ACTIONS = (
    "refresh", "refresh_quota", "open", "open_full", "open_vault", "mirror", "reload", "new",
    "hibernate", "hib_all", "dedup", "baton", "accounts", "compact", "rename", "vault_compact",
    "vault", "nvapi_key", "memory", "ranking",
)
# Tecla con la que se alcanza cada acción (en la vista donde aplica).
KEY_FOR = {
    "refresh": "r", "refresh_quota": "U", "open": 10, "open_full": "o", "open_vault": "b",
    "mirror": "v", "reload": "R", "new": "n", "hibernate": "h", "hib_all": "H", "dedup": "x",
    "baton": "B", "accounts": "u", "compact": "c", "rename": "t", "vault_compact": "C",
    "vault": "V", "nvapi_key": "K", "memory": "m", "ranking": "M",
}
VIEWS = ("sessions", "modal", "accounts", "pending_delete", "vault", "memory")


class FakeWin:
    """Ventana falsa: registra cada addstr y falla si escribe fuera de la pantalla o en la
    última columna (lo que en curses lanza error en la celda inferior derecha)."""

    def __init__(self, h: int, w: int) -> None:
        self.h, self.w = h, w
        self.writes: list[tuple[int, int, str]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return (self.h, self.w)

    def addstr(self, y, x, s, attr=0) -> None:
        assert 0 <= y < self.h and x >= 0 and x + tt.text_cols(s) <= self.w - 1, (y, x, s)
        self.writes.append((y, x, s))

    def erase(self) -> None:
        self.writes.clear()

    def refresh(self) -> None:
        pass

    def move(self, y, x) -> None:
        pass

    def clrtoeol(self) -> None:
        pass

    def getch(self) -> int:
        return -1


def _dash(h: int = 30, w: int = 120):
    win = FakeWin(h, w)
    return tui.Dashboard(win), win


def _render_variant(variant) -> str:
    return "  ".join(f"[{k}] {label}" for k, label in variant)


class TermtextTests(unittest.TestCase):
    def test_text_cols_counts_wide_chars_and_combining(self) -> None:
        self.assertEqual(tt.text_cols("ab"), 2)
        self.assertEqual(tt.text_cols("🔌"), 2)
        self.assertEqual(tt.text_cols("💤 x"), 4)
        self.assertEqual(tt.text_cols("é"), 1)

    def test_fit_cuts_and_pads_by_columns(self) -> None:
        self.assertEqual(tt.fit("🔌🔌🔌", 5), "🔌🔌")
        self.assertEqual(tt.fit("ab🔌", 4, pad=True), "ab🔌")
        self.assertEqual(tt.fit("ab", 4, pad=True), "ab  ")

    def test_first_fit_prefers_full_then_short(self) -> None:
        full, short = "[q] salir  [h] hibernar", "[q] salir"
        self.assertEqual(tt.first_fit([full, short], 30), full)
        self.assertEqual(tt.first_fit([full, short], 12), short)

    def test_tail_keeps_last_columns(self) -> None:
        self.assertEqual(tt.tail("abc🔌", 3), "c🔌")

    def test_clean_removes_escape_and_c1_controls(self) -> None:
        self.assertNotIn("\x1b", tt.clean("a\x1b[31mb\x1b]0;x\x07"))
        self.assertEqual(tt.clean("x\ty\nz\x9b"), "x y z")

    def test_put_never_writes_last_column_or_outside(self) -> None:
        win = FakeWin(3, 10)
        tt.put(win, 0, 0, "x" * 30, 30)
        self.assertEqual(win.writes, [(0, 0, "x" * 9)])
        tt.put(win, 2, 9, "y", 1)
        tt.put(win, 3, 0, "z", 1)
        self.assertEqual(len(win.writes), 1)


class FooterAndButtonTests(unittest.TestCase):
    def test_footer_fits_80_and_50_columns_for_every_view(self) -> None:
        dash, _ = _dash()
        for view in VIEWS:
            variants = dash.FOOTER_VARIANTS[view]
            for cols in (79, 49):  # 80 y 50 columnas (la última celda no se usa)
                with self.subTest(view=view, cols=cols):
                    text, hits = dash._footer(view, cols)
                    self.assertLessEqual(tt.text_cols(text), cols)
                    self.assertIn(text, [_render_variant(v) for v in variants])  # sin cortar
                    self.assertIn("[q]", text)
                    self.assertTrue(all(x1 < cols for _, x1, _ in hits))

    def test_footer_hits_map_to_the_key_codes(self) -> None:
        dash, _ = _dash()
        text, hits = dash._footer("accounts", 119)
        codes = {code for _, _, code in hits}
        self.assertIn(ord("u"), codes)
        self.assertIn(ord("+"), codes)
        self.assertIn(ord("q"), codes)

    def test_buttons_are_a_short_grid_and_labels_fit(self) -> None:
        btns = tui.build_command_buttons(44)
        self.assertLessEqual(len(btns), 10)
        self.assertTrue(all(b.row_offset <= 8 for b in btns))
        self.assertEqual(len({(b.row_offset, b.col_offset) for b in btns}), len(btns))
        for b in btns:
            with self.subTest(action=b.action_id):
                self.assertIn(b.action_id, ORIGINAL_ACTIONS)
                self.assertLessEqual(tt.text_cols(f" {b.label}"), b.width)
                self.assertEqual(tui.hit_test_button(btns, 0, 6, b.col_offset, 6 + b.row_offset), b.action_id)

    def test_every_original_action_keeps_a_key(self) -> None:
        for action in ORIGINAL_ACTIONS:
            with self.subTest(action=action):
                dash, _ = _dash()
                pcs = None
                if action in ("open_full", "open_vault"):
                    pcs = dash.pending_compact_sess = mock.Mock()
                if action == "dedup":  # mata procesos: pide [x] otra vez (EGM r3)
                    dash.sessions = [mock.Mock(extra_procs=[mock.Mock()])]
                with mock.patch.object(dash, "execute_action", return_value="") as ea:
                    dash.handle_key(KEY_FOR[action])
                    if action == "dedup":
                        ea.assert_not_called()
                        dash.handle_key(KEY_FOR[action])
                if pcs is not None:  # el modal actúa sobre la sesión que muestra (EGM r3)
                    ea.assert_called_once_with(action, pcs)
                else:
                    ea.assert_called_once_with(action)

    def test_question_mark_opens_help_and_any_key_closes_it(self) -> None:
        dash, _ = _dash()
        with mock.patch.object(dash, "execute_action", return_value="") as ea:
            dash.handle_key(ord("?"))
            self.assertTrue(dash.show_help)
            dash.handle_key(ord("x"))
        self.assertFalse(dash.show_help)
        ea.assert_not_called()


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        patches = [
            mock.patch.object(tui.curses, "color_pair", side_effect=lambda n: n << 8),
            mock.patch.object(tui.Dashboard, "_prepare_frame", lambda self, now: None),
            mock.patch.object(tui._compact, "list_compacted_vault", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_every_view_draws_within_80_and_50_columns(self) -> None:
        for h, w in ((24, 80), (20, 50), (40, 120)):
            for mode in ("sessions", "accounts", "vault", "memory", "help"):
                with self.subTest(size=(h, w), mode=mode):
                    dash, win = _dash(h, w)
                    dash.reg = {"accounts": {"primary": {"authenticated": True, "email": "a@b.c"}}}
                    if mode == "help":
                        dash.show_help = True
                    else:
                        dash.mode = mode
                    dash.draw(now=1000.0)  # FakeWin falla si algo toca la última columna
                    footer = [t for y, _, t in win.writes if y == h - 1]
                    self.assertTrue(footer)
                    self.assertIn("[q]", footer[0])
                    self.assertLessEqual(tt.text_cols(footer[0]), w - 1)

    def test_footer_click_sends_the_same_key(self) -> None:
        dash, _ = _dash(30, 120)
        dash.mode = "accounts"
        dash.reg = {"accounts": {}}
        dash.draw(now=1.0)
        x0, _, _ = next(h for h in dash.footer_hits if h[2] == ord("u"))
        with mock.patch.object(tui.curses, "getmouse", return_value=(0, x0, 29, 0, curses.BUTTON1_CLICKED)):
            self.assertEqual(dash.handle_mouse(), ord("u"))

    def test_session_inspector_leads_with_state_and_context(self) -> None:
        session = _sessions.AgySession(
            conversation_id="abc12345-zzzz", title="\x1b[2Jsesión", preview="", step_count=7,
            last_modified="", workspace="/tmp/ws", db_status="CASCADE_RUN_STATUS_IDLE", not_fully_idle=False,
        )
        dash, win = _dash(30, 120)
        dash.sessions = [session]
        patches = [
            mock.patch.object(tui._sessions, "transcript_sig", return_value=(0, 0)),
            mock.patch.object(tui._sessions, "zig_tail_transcript", return_value=[]),
            mock.patch.object(tui._compact, "load_nvapi_config", return_value={}),
            mock.patch.object(dash, "cached_context", return_value={"level": "OK", "pct": 12.0, "used_tokens": 9000}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        dash.draw(now=1000.0)
        texts = [t for _, _, t in win.writes]
        self.assertFalse(any("\x1b" in t for t in texts))
        insp = [t for t in texts if "ctx 12%" in t]
        self.assertTrue(insp, "el inspector muestra contexto al principio")
        row = next(y for y, _, t in win.writes if "ctx 12%" in t)
        self.assertEqual(row, 12)  # rejilla de botones en 6..10, regla en 11, inspector desde 12

    def test_compaction_modal_has_no_key_list_and_footer_has_keys(self) -> None:
        session = _sessions.AgySession(
            conversation_id="abc12345-zzzz", title="pesada", preview="", step_count=90,
            last_modified="", workspace="/tmp/ws", db_status="CASCADE_RUN_STATUS_IDLE", not_fully_idle=False,
        )
        dash, win = _dash(30, 120)
        dash.sessions = [session]
        dash.pending_compact_sess = session
        patches = [
            mock.patch.object(tui._sessions, "transcript_sig", return_value=(0, 0)),
            mock.patch.object(tui._compact, "should_offer_compaction", return_value=(True, 40000, 300)),
            mock.patch.object(tui._compact, "get_vault_handoff", return_value=None),
            mock.patch.object(tui._compact, "load_nvapi_config", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        dash.draw(now=1000.0)
        # filas de la modal: py+1..py+4 con py = 30 - 3 - preview_height(10) = 17
        modal = " ".join(t for y, _, t in win.writes if 17 < y < 22)
        self.assertIn("Reabrirla completa", modal)
        self.assertNotIn("[c]", modal)  # las teclas van en el pie, no repetidas en la modal
        footer = next(t for y, _, t in win.writes if y == 29)
        self.assertIn("[c] compactar+ir", footer)

    def test_scan_failed_shows_warning_and_blocks_open_and_hibernate(self) -> None:
        dash, win = _dash(30, 120)
        dash.scan_failed = True
        dash.draw(now=1.0)
        row3 = [t for y, _, t in win.writes if y == 3]
        self.assertTrue(any("escaneo de procesos falló" in t for t in row3))
        for action in ("open", "hibernate", "hib_all", "dedup", "open_full"):
            with self.subTest(action=action):
                self.assertIn("bloqueados", dash.execute_action(action))

    def test_scan_failed_defaults_to_false_for_plain_session_lists(self) -> None:
        dash, _ = _dash()
        self.assertFalse(dash.scan_failed)
        self.assertFalse(getattr(dash.sessions, "scan_failed", False))


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stdscr = mock.Mock()
        self.dashboard = tui.Dashboard(self.stdscr)

    def test_run_curses_tui_builds_dashboard_and_runs_it(self) -> None:
        with mock.patch.object(tui, "Dashboard") as dashboard_cls:
            tui.run_curses_tui(self.stdscr)

        dashboard_cls.assert_called_once_with(self.stdscr)
        dashboard_cls.return_value.run.assert_called_once_with()

    def test_handle_key_q_r_and_arrows(self) -> None:
        self.assertEqual(self.dashboard.mode, "sessions")
        self.assertEqual(self.dashboard.cursor, 0)

        with mock.patch.object(tui._tmuxctl, "tmux_cmd") as tmux_cmd:
            self.dashboard.handle_key(ord("q"))
            tmux_cmd.assert_called_once_with("next-window")
            tmux_cmd.reset_mock()

            self.dashboard.handle_key(ord("r"))
            tmux_cmd.assert_called_once_with("refresh-client", "-S")

        self.assertTrue(self.dashboard.force_scan)
        self.assertEqual(self.dashboard.cached_preview_sig, (-1, -1))
        self.assertEqual(self.dashboard.mode, "sessions")

        self.dashboard.force_scan = False
        self.dashboard.handle_key(curses.KEY_DOWN)
        self.assertEqual(self.dashboard.cursor, 1)
        self.dashboard.handle_key(curses.KEY_UP)
        self.assertEqual(self.dashboard.cursor, 0)
        self.dashboard.handle_key(curses.KEY_UP)
        self.assertEqual(self.dashboard.cursor, 0)


if __name__ == "__main__":
    unittest.main()
