"""Pruebas unitarias del prototipo de compactación del hook de AGY."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agy_dash import autocompact as ac  # noqa: E402


class AutoCompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.transcript = self.root / "transcript.jsonl"
        self.heartbeats = self.root / "heartbeats"
        self.heartbeats.mkdir()
        self.paths_patch = mock.patch.object(ac._paths, "HEARTBEATS_DIR", self.heartbeats)
        self.paths_patch.start()
        self.addCleanup(self.paths_patch.stop)
        self.env = {"AGY_DASH_MACHINE_COMPACT": "1"}
        self.payload = {
            "conversationId": "conv-123",
            "transcriptPath": str(self.transcript),
            "modelName": "gemini-test",
            "invocationNum": 1,
        }
        self.spawn = mock.Mock()
        self.measure = mock.Mock(return_value={"used_tokens": 250000})

    def write_transcript(self, *events: dict | str) -> None:
        lines = [event if isinstance(event, str) else json.dumps(event) for event in events]
        self.transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def decide(self, *, used_tokens: int = 250000, **kwargs: object) -> dict:
        self.measure.return_value = {"used_tokens": used_tokens}
        return ac.decide(
            self.payload,
            env=self.env,
            measure=self.measure,
            spawn=self.spawn,
            now=1000,
            **kwargs,
        )

    def test_disabled_by_default(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "hola"})
        self.assertEqual(
            ac.decide(
                self.payload,
                env={},
                measure=self.measure,
                spawn=self.spawn,
                now=1000,
            ),
            {},
        )
        self.measure.assert_not_called()
        self.spawn.assert_not_called()

    def test_threshold_below_and_at_or_above(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "sigue"})
        self.assertEqual(self.decide(used_tokens=249999), {})
        self.spawn.assert_not_called()

        result = self.decide(used_tokens=250000)
        self.assertEqual(result["terminationBehavior"], "terminate")
        self.assertEqual(len(self.spawn.call_args_list), 1)

    def test_threshold_can_be_configured_above_and_below(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "sigue"})
        self.env["AGY_DASH_AUTO_COMPACT_TOKENS"] = "300000"
        self.assertEqual(self.decide(used_tokens=299999), {})
        self.env["AGY_DASH_AUTO_COMPACT_TOKENS"] = "200000"
        self.assertEqual(self.decide(used_tokens=200001)["terminationBehavior"], "terminate")

    def test_compact_command_fires_below_threshold_and_keeps_followup(self) -> None:
        self.write_transcript(
            {
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\n/compactnvidia continúa con el resumen\n</USER_REQUEST>",
            }
        )
        result = self.decide(used_tokens=12)
        self.assertEqual(result["terminationBehavior"], "terminate")
        self.assertEqual(self.spawn.call_args.args[0][-2:], ["--resume-prompt", "continúa con el resumen"])

    def test_invocation_three_does_not_fire(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "/compactnvidia"})
        self.payload["invocationNum"] = 3
        self.assertEqual(self.decide(used_tokens=500000), {})
        self.measure.assert_not_called()
        self.spawn.assert_not_called()

    def test_antidebounce_suppresses_for_600_seconds(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "sigue"})
        (self.heartbeats / "conv-123.autocompact").write_text("500", encoding="utf-8")
        self.assertEqual(self.decide(used_tokens=500000), {})
        self.measure.assert_not_called()
        self.spawn.assert_not_called()

    def test_corrupt_transcript_lines_are_ignored(self) -> None:
        self.write_transcript(
            "{bad json",
            {"type": "USER_INPUT", "content": "primero"},
            "not json either",
            {"type": "USER_INPUT", "content": "<USER_REQUEST>último texto</USER_REQUEST>"},
        )
        self.assertEqual(ac.last_user_input(self.transcript), "último texto")

    def test_invalid_hook_json_returns_empty_object(self) -> None:
        self.assertEqual(ac.hook_main("{no-json", env=self.env), "{}")

    def test_spawn_receives_exact_argv(self) -> None:
        self.write_transcript({"type": "USER_INPUT", "content": "mensaje de seguimiento"})
        with mock.patch.object(ac._paths, "SELF_PATH", self.root / "agy_dashboard.py"):
            self.assertEqual(self.decide(used_tokens=250000)["terminationBehavior"], "terminate")
        self.assertEqual(
            self.spawn.call_args.args[0],
            [
                sys.executable,
                str(self.root / "agy_dashboard.py"),
                "self-compact",
                "conv-123",
                "--resume-prompt",
                "mensaje de seguimiento",
            ],
        )


if __name__ == "__main__":
    unittest.main()
