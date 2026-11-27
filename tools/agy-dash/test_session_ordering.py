"""Regression tests for session recency ordering in agy_dash.sessions."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agy_dash import accounts, paths, procs, sessions


def _init_summaries_db(db_path: Path, rows: list[tuple]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT "",
            preview TEXT NOT NULL DEFAULT "",
            step_count INTEGER NOT NULL DEFAULT 0,
            last_modified_time DATETIME NOT NULL,
            workspace_uris TEXT NOT NULL DEFAULT "[]",
            status TEXT NOT NULL DEFAULT "CASCADE_RUN_STATUS_IDLE",
            nesting_depth INTEGER NOT NULL DEFAULT 0,
            killed NUMERIC NOT NULL DEFAULT 0,
            not_fully_idle NUMERIC NOT NULL DEFAULT 0
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO conversation_summaries
            (conversation_id, title, preview, step_count, last_modified_time,
             workspace_uris, status, nesting_depth, killed, not_fully_idle)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def _proc(pid: int, *, active_children: list[str] | None = None) -> procs.ProcMemInfo:
    return procs.ProcMemInfo(
        pid=pid,
        ppid=1,
        tty="pts/1",
        rss_kb=100_000,
        pss_kb=80_000,
        pss_anon_kb=50_000,
        shared_clean_kb=20_000,
        ksm_kb=4_000,
        swap_kb=0,
        active_children=list(active_children or []),
        in_dash=True,
    )


class SessionOrderingTests(unittest.TestCase):
    def _run_load(
        self,
        tmp: Path,
        rows: list[tuple],
        *,
        live_by_conv: dict[str, list[procs.ProcMemInfo]] | None = None,
        hib_reg: dict[str, dict] | None = None,
        extra_db_rows: list[tuple] | None = None,
        limit: int = 40,
    ) -> list[sessions.AgySession]:
        data_dir = tmp / "agy"
        db_path = data_dir / "conversation_summaries.db"
        brain_dir = data_dir / "brain"
        convs_dir = data_dir / "conversations"
        accts_dir = data_dir / "accounts"
        hib_file = data_dir / "dashboard_hibernated.json"
        titles_file = data_dir / "custom_titles.json"

        _init_summaries_db(db_path, rows)
        if extra_db_rows is not None:
            alt_db = accts_dir / "pro2" / "home" / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
            _init_summaries_db(alt_db, extra_db_rows)
        if hib_reg:
            hib_file.write_text(json.dumps(hib_reg), encoding="utf-8")

        with mock.patch.object(paths, "SUMMARIES_DB", db_path), \
             mock.patch.object(paths, "BRAIN_DIR", brain_dir), \
             mock.patch.object(paths, "CONVERSATIONS_DIR", convs_dir), \
             mock.patch.object(paths, "ACCOUNTS_DIR", accts_dir), \
             mock.patch.object(paths, "HIBERNATED_FILE", hib_file), \
             mock.patch.object(paths, "CUSTOM_TITLES_FILE", titles_file), \
             mock.patch.object(procs, "zig_scan", return_value=(100, {}, dict(live_by_conv or {}))), \
             mock.patch.object(sessions._tmuxctl, "_get_dash_window_map", return_value={}), \
             mock.patch.object(sessions._tmuxctl, "_get_dash_tty_map", return_value={}), \
             mock.patch.object(sessions._tmuxctl, "_get_bg_window_map", return_value={}), \
             mock.patch.object(sessions, "is_transcript_turn_active", return_value=False):
            _, _, loaded = sessions.load_sessions(limit=limit)
        return loaded

    def test_parse_iso_ts_handles_nanoseconds_z_naive_and_go_zero(self) -> None:
        self.assertEqual(sessions._parse_iso_ts(""), 0.0)
        self.assertEqual(sessions._parse_iso_ts(None), 0.0)
        self.assertEqual(sessions._parse_iso_ts("0001-01-01 00:00:00+00:00"), 0.0)
        self.assertEqual(sessions._parse_iso_ts("ahora"), 0.0)

        t_nano = sessions._parse_iso_ts("2026-11-25 16:00:48.524311998+00:00")
        t_z = sessions._parse_iso_ts("2026-11-25T16:00:48Z")
        t_naive = sessions._parse_iso_ts("2026-11-25 16:00:48")
        self.assertAlmostEqual(t_nano, t_z + 0.524311, places=5)
        self.assertEqual(t_z, t_naive)

    def test_active_and_hibernated_tiers_sort_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rows = [
                ("run-old", "Run Old", "", 10, "2026-11-25 10:00:00+00:00", "[]", "CASCADE_RUN_STATUS_RUNNING", 0, 0, 0),
                ("run-new", "Run New", "", 10, "2026-11-25 12:00:00+00:00", "[]", "CASCADE_RUN_STATUS_RUNNING", 0, 0, 0),
                ("idle-old", "Idle Old", "", 5, "2026-11-24 08:00:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
                ("idle-new", "Idle New", "", 5, "2026-11-25 09:00:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
                ("hib-old", "Hib Old", "", 5, "2026-11-22 10:00:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
                ("hib-new", "Hib New", "", 5, "2026-11-24 10:00:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
            ]
            live = {
                "run-old": [_proc(101)],
                "run-new": [_proc(102)],
                "idle-old": [_proc(103)],
                "idle-new": [_proc(104)],
                "live-fresh": [_proc(105)],  # UNKNOWN, not in DB yet ("ahora")
            }
            hib = {
                "hib-old": {"saved_pss_kb": 100_000, "hibernated_at": "2026-11-22T10:00:00Z"},
                "hib-new": {"saved_pss_kb": 100_000, "hibernated_at": "2026-11-24T10:00:00Z"},
            }
            got = self._run_load(tmp, rows, live_by_conv=live, hib_reg=hib)
            ids = [s.conversation_id for s in got]
            self.assertEqual(
                ids,
                ["run-new", "run-old", "live-fresh", "idle-new", "idle-old", "hib-new", "hib-old"],
            )

    def test_saved_uses_disk_mtime_when_db_timestamp_is_stale_and_closing_keeps_recent_on_top(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # Simulate DB clobbered backwards: recent chat has stale DB timestamp (18:14)
            # while older test chat has newer DB timestamp (23:24).
            rows = [
                ("old-test", "Old Test", "", 4, "2026-11-24 23:24:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
                ("recent-clobbered", "Recent Chat", "", 17, "2026-11-24 18:14:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
                ("just-closed", "Just Closed", "", 241, "2026-11-24 22:58:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
            ]
            # Write real disk files with ground-truth mtimes:
            #   just-closed     -> 2026-11-25 15:52:00 UTC (newest)
            #   recent-clobbered -> 2026-11-25 03:51:00 UTC (second newest)
            #   old-test        -> 2026-11-24 23:24:00 UTC (oldest)
            t_closed = dt.datetime(2026, 11, 25, 15, 52, 0, tzinfo=dt.timezone.utc).timestamp()
            t_recent = dt.datetime(2026, 11, 25, 3, 51, 0, tzinfo=dt.timezone.utc).timestamp()
            t_old = dt.datetime(2026, 11, 24, 23, 24, 0, tzinfo=dt.timezone.utc).timestamp()

            tx_closed = tmp / "agy" / "brain" / "just-closed" / ".system_generated" / "logs" / "transcript.jsonl"
            tx_closed.parent.mkdir(parents=True, exist_ok=True)
            tx_closed.write_text("{}\n", encoding="utf-8")
            os.utime(tx_closed, (t_closed, t_closed))

            convs = tmp / "agy" / "conversations"
            convs.mkdir(parents=True, exist_ok=True)
            cdb_recent = convs / "recent-clobbered.db"
            cdb_recent.write_bytes(b"sqlite")
            os.utime(cdb_recent, (t_recent, t_recent))

            cdb_old = convs / "old-test.db"
            cdb_old.write_bytes(b"sqlite")
            os.utime(cdb_old, (t_old, t_old))

            got = self._run_load(tmp, rows)
            self.assertEqual([s.conversation_id for s in got], ["just-closed", "recent-clobbered", "old-test"])
            self.assertEqual(got[0].last_modified, "2026-11-25 15:52:00")
            self.assertEqual(got[1].last_modified, "2026-11-25 03:51:00")

    def test_saved_merges_multiple_dbs_in_recency_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            primary_rows = [
                ("p-new", "Primary New", "", 10, "2026-11-25 12:00:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
                ("p-old", "Primary Old", "", 10, "2026-11-23 12:00:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
            ]
            alt_rows = [
                ("a-mid", "Alt Mid", "", 10, "2026-11-24 12:00:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
            ]
            got = self._run_load(tmp, primary_rows, extra_db_rows=alt_rows)
            self.assertEqual([s.conversation_id for s in got], ["p-new", "a-mid", "p-old"])

    def test_clobbered_db_row_beyond_sql_limit_is_recovered_via_disk_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # 90 filler sessions with 2026-11-24 timestamps so SQL LIMIT 80 excludes the clobbered row
            rows = [
                (
                    f"filler-{i:03d}",
                    f"Filler {i}",
                    "",
                    2,
                    f"2026-11-24 10:{i // 60:02d}:{i % 60:02d}+00:00",
                    "[]",
                    "CASCADE_RUN_STATUS_IDLE",
                    0,
                    0,
                    0,
                )
                for i in range(90)
            ]
            rows.append(
                ("clobbered-zero", "Clobbered Recent", "", 50, "0001-01-01 00:00:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0)
            )
            convs = tmp / "agy" / "conversations"
            convs.mkdir(parents=True, exist_ok=True)
            cdb = convs / "clobbered-zero.db"
            cdb.write_bytes(b"sqlite")
            t_now = dt.datetime(2026, 11, 25, 16, 0, 0, tzinfo=dt.timezone.utc).timestamp()
            os.utime(cdb, (t_now, t_now))

            got = self._run_load(tmp, rows, limit=10)
            self.assertEqual(got[0].conversation_id, "clobbered-zero")

    def test_empty_wal_file_does_not_bump_old_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rows = [
                ("recent-chat", "Recent", "", 10, "2026-11-25 10:00:00+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
                ("ancient-chat", "Ancient", "", 500, "2026-11-03 22:11:36+00:00", "[]", "CASCADE_RUN_STATUS_IDLE", 0, 0, 0),
            ]
            convs = tmp / "agy" / "conversations"
            convs.mkdir(parents=True, exist_ok=True)
            t_old = dt.datetime(2026, 11, 3, 22, 12, 0, tzinfo=dt.timezone.utc).timestamp()
            t_now = dt.datetime(2026, 11, 25, 16, 11, 0, tzinfo=dt.timezone.utc).timestamp()
            cdb = convs / "ancient-chat.db"
            cdb.write_bytes(b"sqlite")
            os.utime(cdb, (t_old, t_old))
            # SQLite read-only open leaves a 0-byte .db-wal with current mtime
            wal = convs / "ancient-chat.db-wal"
            wal.write_bytes(b"")
            os.utime(wal, (t_now, t_now))

            got = self._run_load(tmp, rows)
            self.assertEqual([s.conversation_id for s in got], ["recent-chat", "ancient-chat"])

    def test_ensure_isolated_home_shares_jetbox_summaries_proto(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            data = base / ".gemini" / "antigravity-cli"
            data.mkdir(parents=True)
            (data / "jetbox_summaries_proto.pb").write_bytes(b"proto")
            slot_home = accounts.ensure_isolated_home(
                "pro9",
                base_home=base,
                agy_data_dir=data,
                accounts_dir=data / "accounts",
            )
            link = slot_home / ".gemini" / "antigravity-cli" / "jetbox_summaries_proto.pb"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (data / "jetbox_summaries_proto.pb").resolve())


if __name__ == "__main__":
    unittest.main()
