#!/usr/bin/env python3
"""Tests for tools/agent_memory.py (isymem). Run: python3 tools/test_agent_memory.py

Every test uses a throwaway bank (ISYMEM_DIR / ISYMEM_STATE_DIR / HOME in a
temp dir): the real shared memory and TUI configs are never touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent / "agent_memory.py"


class IsymemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.mem = base / "memory"
        self.state = base / "state"
        self.home = base / "home"
        self.home.mkdir()
        self.env = {**os.environ, "ISYMEM_DIR": str(self.mem), "ISYMEM_STATE_DIR": str(self.state),
                    "HOME": str(self.home), "ISYMEM_AUTHOR": "test_agent"}
        self.env.pop("ISYMEM_SYNC_TARGETS", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, ok=True, env=None):
        p = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                           env=env or self.env, timeout=60)
        if ok:
            self.assertEqual(p.returncode, 0, f"{args}\nstdout={p.stdout}\nstderr={p.stderr}")
        return p

    def save(self, slug, *extra):
        return self.run_cli("save", slug, *extra)

    def test_create_writes_file_and_index(self):
        self.save("gpu-cap", "--name", "GPU cap", "--desc", "cap \"27B\" runs", "--type", "feedback",
                  "--why", "laptop froze", "--how", "use systemd-run")
        text = (self.mem / "gpu-cap.md").read_text()
        self.assertIn('description: "cap \\"27B\\" runs"', text)  # valid YAML double-quoted
        self.assertIn("  type: feedback", text)
        self.assertIn("  author: test_agent", text)
        self.assertIn("**Why:** laptop froze", text)
        self.assertEqual((self.mem / "MEMORY.md").read_text(),
                         '- [GPU cap](gpu-cap.md) — cap "27B" runs\n')

    def test_new_memory_requires_name_and_desc(self):
        p = self.run_cli("save", "x", "--desc", "d", ok=False)
        self.assertEqual(p.returncode, 2)
        self.assertFalse((self.mem / "x.md").exists())

    def test_update_appends_and_keeps_foreign_frontmatter(self):
        self.mem.mkdir(parents=True)
        (self.mem / "rule.md").write_text(
            "---\nname: rule\ndescription: \"old\"\nmetadata:\n  node_type: memory\n  type: feedback\n"
            "  originSessionId: abc-123\n  modified: 2026-01-01T00:00:00.000Z\n---\n\nOriginal body.\n")
        (self.mem / "MEMORY.md").write_text("- [Keep this title](rule.md) — old\n")
        self.save("rule", "--desc", "new desc", "--body", "Second fact.")
        text = (self.mem / "rule.md").read_text()
        self.assertIn("Original body.", text)
        self.assertIn("Second fact.", text)
        self.assertIn("originSessionId: abc-123", text)
        self.assertIn("  type: feedback", text)  # no --type => type kept, not reset to project
        self.assertIn('description: "new desc"', text)
        self.assertEqual((self.mem / "MEMORY.md").read_text(), "- [Keep this title](rule.md) — new desc\n")
        hist = list((self.state / "history" / "rule").glob("*.md"))
        self.assertEqual(len(hist), 1)
        self.assertIn("Original body.", hist[0].read_text())

    def test_replace_rewrites_body(self):
        self.save("s", "--name", "S", "--desc", "d", "--body", "old fact")
        self.save("s", "--body", "new fact", "--replace")
        text = (self.mem / "s.md").read_text()
        self.assertNotIn("old fact", text)
        self.assertIn("new fact", text)

    def test_refuses_secrets(self):
        for leak in ("LEASE_TOKEN=abcdef0123456789", "ghp_" + "a" * 36, "api_key = " + "Z" * 24):
            p = self.run_cli("save", "leak", "--name", "L", "--desc", "d", "--body", leak, ok=False)
            self.assertEqual(p.returncode, 2, leak)
            self.assertIn("REFUSED", p.stderr)
        self.assertFalse((self.mem / "leak.md").exists())
        # A redacted placeholder is fine.
        self.save("ok", "--name", "OK", "--desc", "d", "--body", "LEASE_TOKEN=<redacted-in-raw-file>")

    def test_archive_never_deletes(self):
        self.save("a", "--name", "A", "--desc", "d")
        self.save("b", "--name", "B", "--desc", "d")
        self.run_cli("archive", "a")
        self.assertFalse((self.mem / "a.md").exists())
        self.assertEqual(len(list((self.state / "archive").glob("a.*.md"))), 1)
        self.assertEqual((self.mem / "MEMORY.md").read_text(), "- [B](b.md) — d\n")
        self.assertIn("archived", self.run_cli("history", "a").stdout)
        self.run_cli("remove", "b")  # alias, same behaviour
        self.assertEqual(len(list((self.state / "archive").glob("b.*.md"))), 1)

    def test_recall_ranks_relevant_memory_first(self):
        self.save("never-kill-parent-pid", "--name", "Never kill derived PIDs", "--desc",
                  "print and check comm before any kill of a ppid", "--type", "feedback")
        self.save("gpu-cap", "--name", "GPU cap", "--desc", "llama.cpp runs under systemd-run MemoryMax")
        self.save("ziggrep", "--name", "ziggrep", "--desc", "search with ziggrep")
        out = self.run_cli("recall", "voy a matar un proceso colgado").stdout  # Spanish gloss -> kill
        first = out.splitlines()[1]
        self.assertIn("never-kill-parent-pid", first)
        out = self.run_cli("recall", "benchmark llama.cpp on the GPU").stdout
        self.assertIn("gpu-cap", out.splitlines()[1])
        self.assertEqual(self.run_cli("recall", "zzzz qqqq", ok=False).returncode, 1)

    def test_show_links_and_backlinks(self):
        self.save("a", "--name", "A", "--desc", "d", "--body", "see [[b]] and [[ghost]]")
        self.save("b", "--name", "B", "--desc", "d")
        out = self.run_cli("show", "a").stdout
        self.assertIn("Enlaza a  : b, ghost (no existe aun)", out)
        self.assertIn("Citada por: a", self.run_cli("show", "b").stdout)

    def test_doctor_flags_problems(self):
        self.save("good", "--name", "G", "--desc", "d")
        (self.mem / "orphan.md").write_text("no frontmatter\n")
        with open(self.mem / "MEMORY.md", "a") as fh:
            fh.write("- [Gone](gone.md) — missing file\n")
        p = self.run_cli("doctor", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("index points to missing file: gone.md", p.stdout)
        self.assertIn("unindexed file, not reachable from any indexed hub: orphan.md", p.stdout)
        self.assertIn("orphan.md: no YAML frontmatter", p.stdout)

    def test_concurrent_saves_lose_nothing(self):
        errors = []

        def worker(i):
            p = subprocess.run([sys.executable, str(TOOL), "save", f"m{i}", "--name", f"M{i}", "--desc", "d"],
                               capture_output=True, text=True, env=self.env, timeout=60)
            if p.returncode:
                errors.append(p.stderr)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        lines = (self.mem / "MEMORY.md").read_text().splitlines()
        self.assertEqual(sorted(lines), sorted(f"- [M{i}](m{i}.md) — d" for i in range(12)))

    def test_setup_inline_index_and_sync(self):
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "AGENTS.md").write_text("# my own codex rules\n")
        self.save("a", "--name", "A", "--desc", "first")
        out = self.run_cli("setup").stdout
        self.assertIn("qwen", out)
        self.assertIn("skipped (harness not installed", out)
        codex = (self.home / ".codex" / "AGENTS.md").read_text()
        self.assertTrue(codex.startswith("# my own codex rules\n"))
        self.assertIn("- [A](a.md) — first", codex)
        env = {**self.env, "ISYMEM_SYNC_TARGETS": "1"}
        self.run_cli("save", "b", "--name", "B", "--desc", "second", env=env)
        codex = (self.home / ".codex" / "AGENTS.md").read_text()
        self.assertIn("- [B](b.md) — second", codex)
        self.assertEqual(codex.count("<!-- isymem:setup begin -->"), 1)
        self.run_cli("setup", "--undo")
        self.assertEqual((self.home / ".codex" / "AGENTS.md").read_text(), "# my own codex rules\n")

    # ---- projects / anchors / graph

    def test_anchor_lifecycle_and_shortcut(self):
        out = self.run_cli("add", "isycode", "--todo", "fix daemon relaunch", "--link", "isycode-modes").stdout
        self.assertIn("(hub nuevo)", out)
        aid = out.split("#", 1)[1][:4]
        self.run_cli("add", "isycode", "--fail", "WDA cert expired on device")
        self.run_cli("add", "wins", "--todo", "SteamAPI relay")
        hub = (self.mem / "hub-isycode.md").read_text()
        self.assertRegex(hub, rf"- @todo#{aid} #isycode \d{{4}}-\d{{2}}-\d{{2}}: fix daemon relaunch \[\[isycode-modes\]\]")
        self.assertIn("[isycode](hub-isycode.md)", (self.mem / "MEMORY.md").read_text())
        # shortcut form: isymem <project> --fails --pending
        out = self.run_cli("isycode", "--fails", "--pendientes").stdout
        self.assertIn("fix daemon relaunch", out)
        self.assertIn("WDA cert expired", out)
        self.assertNotIn("SteamAPI", out)
        self.assertIn("SteamAPI", self.run_cli("--open").stdout)  # all projects
        self.run_cli("close", aid, "--note", "fixed in PR 31")
        self.assertRegex((self.mem / "hub-isycode.md").read_text(), rf"@done#{aid} .*✓\d{{4}}-\d{{2}}-\d{{2}} fixed in PR 31")
        self.assertNotIn("fix daemon relaunch", self.run_cli("isycode", "--todo", ok=False).stdout)
        self.assertIn("fixed in PR 31", self.run_cli("isycode", "--done").stdout)
        self.assertEqual(self.run_cli("close", aid, ok=False).returncode, 1)  # done is not closable
        card = self.run_cli("isycode").stdout
        self.assertIn("abiertos: 0 todo · 1 fail", card)

    def test_anchor_query_without_ziggrep(self):
        self.run_cli("add", "p1", "--rule", "never push on friday")
        env = {**self.env, "PATH": "/nonexistent"}
        p = subprocess.run(["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()],
                            sys.executable, str(TOOL), "p1", "--rules"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("never push on friday", p.stdout)

    def test_project_memories_leave_index_and_compact(self):
        self.save("rule-a", "--name", "Rule A", "--desc", "general rule", "--type", "feedback")
        self.save("isy-state", "--name", "Isy state", "--desc", "before hub", "--project", "isy")
        self.assertIn("isy-state.md", (self.mem / "MEMORY.md").read_text())  # no hub yet: indexed
        self.run_cli("hub", "isy", "--name", "ISy", "--desc", "the isy project")
        out = self.run_cli("compact").stdout
        self.assertIn("1 memories reached via hubs", out)
        idx = (self.mem / "MEMORY.md").read_text()
        self.assertNotIn("isy-state.md", idx)
        self.assertIn("- [Rule A](rule-a.md) — general rule", idx)
        self.assertIn("- [ISy](hub-isy.md) — the isy project", idx)
        self.assertLess(idx.index("rule-a.md"), idx.index("## Proyectos"))
        self.assertIn("[[isy-state]]", (self.mem / "hub-isy.md").read_text())
        # new project memory goes straight to the hub, general one before the projects section
        self.save("isy-more", "--name", "More", "--desc", "x", "--project", "isy")
        self.save("rule-b", "--name", "Rule B", "--desc", "y")
        idx = (self.mem / "MEMORY.md").read_text()
        self.assertNotIn("isy-more.md", idx)
        self.assertLess(idx.index("rule-b.md"), idx.index("## Proyectos"))
        self.assertIn("[[isy-more]]", (self.mem / "hub-isy.md").read_text())
        p = self.run_cli("doctor")
        self.assertNotIn("WARN", p.stdout)
        self.run_cli("archive", "isy-more")
        self.assertNotIn("[[isy-more]]", (self.mem / "hub-isy.md").read_text())

    def test_graph_neighbors(self):
        self.save("a", "--name", "A", "--desc", "d", "--project", "alpha", "--body", "uses [[b]]")
        self.save("b", "--name", "B", "--desc", "d", "--project", "beta")
        self.run_cli("hub", "alpha", "--desc", "alpha")
        self.run_cli("hub", "beta", "--desc", "beta")
        self.assertIn("vecinos (grafo por [[links]]): beta(1)", self.run_cli("alpha").stdout)
        out = self.run_cli("graph", "b").stdout
        self.assertIn("← a (alpha)", out)
        self.assertIn("a: → b (beta)", self.run_cli("graph", "alpha").stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
