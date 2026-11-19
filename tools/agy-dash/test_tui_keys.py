"""Contract between the key registry (KEYMAP), the footer (FOOTER_VARIANTS), the help sheet
(HELP_LINES) and the handlers, plus the single-Confirm behaviour of agy_dash/tui.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_dash.tui as tui  # noqa: E402
from test_tui import FakeWin  # noqa: E402
from test_tui_round2 import _sess  # noqa: E402

D = tui.Dashboard
ANY_KEY = "otra tecla"


def _help_codes(keys: str) -> tuple[set[int], bool]:
    """Codes named in one HELP_LINES key cell, and whether it says "any other key"."""
    codes: set[int] = set()
    any_key = False
    for part in keys.split(" / "):
        if part == ANY_KEY:
            any_key = True
            continue
        codes.update(tui.key_codes(part))
    return codes, any_key


class KeyContract(unittest.TestCase):
    def test_footer_help_and_keymap_cover_the_same_views(self) -> None:
        self.assertEqual(set(D.FOOTER_VARIANTS), set(tui.KEYMAP))
        self.assertEqual(set(D.HELP_LINES), set(tui.KEYMAP))

    def test_every_bound_action_has_a_handler(self) -> None:
        for view, table in tui.KEYMAP.items():
            for code, action in table.items():
                with self.subTest(view=view, code=code, action=action):
                    self.assertTrue(action in tui.EXEC_ACTIONS or callable(getattr(D, f"_k_{action}", None)))

    def test_every_footer_key_is_bound(self) -> None:
        for view, variants in D.FOOTER_VARIANTS.items():
            any_key = tui.CONFIRM_SPECS.get(view, (None, False, ""))[1]
            for variant in variants:
                for key, _label in variant:
                    with self.subTest(view=view, key=key):
                        codes = set(tui.key_codes(key)) - tui.GLOBAL_KEYS
                        if any_key and key == "Esc":
                            continue  # "cancelar" = cualquier tecla sin acción
                        self.assertTrue(codes <= set(tui.KEYMAP[view]), codes - set(tui.KEYMAP[view]))

    def test_every_help_key_is_bound(self) -> None:
        for view, rows in D.HELP_LINES.items():
            for keys, _desc in rows:
                codes, any_key = _help_codes(keys)
                with self.subTest(view=view, keys=keys):
                    if any_key:
                        self.assertTrue(tui.CONFIRM_SPECS[view][1], "solo una confirmación cancela con otra tecla")
                    self.assertTrue(codes <= set(tui.KEYMAP[view]), codes - set(tui.KEYMAP[view]))

    def test_every_bound_key_is_in_the_help_sheet(self) -> None:
        for view, table in tui.KEYMAP.items():
            documented: set[int] = set()
            for keys, _desc in D.HELP_LINES[view]:
                documented |= _help_codes(keys)[0]
            with self.subTest(view=view):
                self.assertEqual(set(table) - documented, set())

    def test_footer_clicks_send_bound_keys(self) -> None:
        d = D(FakeWin(30, 200))
        for view in tui.KEYMAP:
            any_key = tui.CONFIRM_SPECS.get(view, (None, False, ""))[1]
            _text, hits = d._footer(view, 199)
            for _x0, _x1, code in hits:
                with self.subTest(view=view, code=code):
                    self.assertTrue(code in tui.KEYMAP[view] or code in tui.GLOBAL_KEYS or any_key)


class SingleConfirm(unittest.TestCase):
    def setUp(self) -> None:
        for p in (mock.patch.object(tui.curses, "flushinp"),
                  mock.patch.object(tui._compact, "list_compacted_vault", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.d = D(FakeWin(30, 120))

    def test_only_one_confirmation_is_open_at_a_time(self) -> None:
        s = _sess(1)
        self.d.pending_compact_sess = s
        self.d.pending_dedup = True
        self.assertFalse(self.d.pending_compact_sess)
        self.assertEqual(self.d.confirm.kind, "dedup_confirm")
        self.d.pending_compact_sess = None  # otro tipo: no cierra la de dedup
        self.assertTrue(self.d.pending_dedup)

    def test_switching_view_closes_the_confirmation(self) -> None:
        self.d.pending_compact_sess = _sess(1)
        self.d.handle_key(ord("u"))  # el modal no tiene `u`: no la deja pasar a la vista de debajo
        self.assertEqual(self.d.mode, "sessions")
        self.assertIsNotNone(self.d.confirm)
        with mock.patch.object(self.d, "trigger_quota_poll"):
            self.d.execute_action("accounts")  # el baton sin cuenta destino abre Cuentas así
        self.assertEqual(self.d.mode, "accounts")
        self.assertIsNone(self.d.confirm)

    def test_any_key_confirmation_cancels_with_its_message(self) -> None:
        self.d.mode = "vault"
        self.d.pending_vault_delete = {"path": "/x", "title": "t"}
        with mock.patch.object(tui._compact, "delete_vault_handoff") as rm:
            self.d.handle_key(ord("j"))
        rm.assert_not_called()
        self.assertIsNone(self.d.confirm)
        self.assertEqual(self.d.status_banner, tui.CONFIRM_SPECS["vault_confirm"][2])
        self.assertEqual(self.d.cursor, 0)  # la tecla solo cancela, no mueve

    def test_account_deletion_ignores_unbound_keys(self) -> None:
        self.d.mode = "accounts"
        self.d.pending_delete_info = {"slot": "pro2", "blocked_reason": ""}
        self.d.handle_key(ord("j"))
        self.assertIsNotNone(self.d.pending_delete_info)
        self.d.handle_key(27)
        self.assertIsNone(self.d.pending_delete_info)

    def test_confirmation_of_another_mode_does_not_capture_keys(self) -> None:
        self.d.pending_vault_delete = {"path": "/x", "title": "t"}  # vive en "vault"
        self.assertEqual(self.d._view_name(), "sessions")
        with mock.patch.object(self.d, "execute_action", return_value="") as ea:
            self.d.handle_key(ord("r"))
        ea.assert_called_once_with("refresh")


class Views(unittest.TestCase):
    def setUp(self) -> None:
        self.d = D(FakeWin(30, 120))
        self.d.sessions = [_sess(1), _sess(2), _sess(3)]

    def test_toggle_keeps_the_saved_session_between_views(self) -> None:
        self.d.cursor = 2
        with mock.patch.object(self.d, "trigger_quota_poll"):
            self.d.execute_action("memory")
            self.d.execute_action("accounts")  # memoria -> cuentas, sin pasar por sesiones
            self.assertEqual(self.d.mode, "accounts")
            self.assertIs(self.d.current_session(), self.d.sessions[2])
            self.d.execute_action("accounts")  # otra vez = volver
        self.assertEqual((self.d.mode, self.d.cursor), ("sessions", 2))

    def test_account_baton_uses_the_saved_session(self) -> None:
        self.d.cursor = 1
        self.d.execute_action("vault")
        self.d.mode = "accounts"
        self.d.reg = {"accounts": {"primary": {}, "pro2": {}}}
        self.d.cursor = 1
        with mock.patch.object(tui._accounts, "baton_pass_session", return_value=(True, "ok")) as bp:
            self.d.handle_key(ord("B"))
        self.assertIs(bp.call_args.args[0], self.d.sessions[1])
        self.assertEqual(bp.call_args.kwargs["target_slot"], "pro2")
        self.assertEqual((self.d.mode, self.d.cursor), ("sessions", 1))


if __name__ == "__main__":
    unittest.main()
