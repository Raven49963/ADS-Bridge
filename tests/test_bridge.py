"""Offline unit tests for bridge protocol, ledger safety, and local utilities.

Run without ADS GUI: python -B -m unittest discover -s tests -v
"""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import bridge
import worker


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, ADS_BRIDGE_RUNTIME=self.directory.name)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.session = uuid.uuid4().hex
        self.job = uuid.uuid4().hex

    def test_utf8_roundtrip_and_frame_limit(self) -> None:
        data = {"workspace": "射频测试"}
        self.assertEqual(bridge.read_frame(BytesIO(bridge.frame(data))), data)
        with patch.object(bridge, "MAX_FRAME", 8), self.assertRaises(bridge.BridgeError):
            bridge.frame(data)
        with self.assertRaises(bridge.BridgeError):
            bridge.read_frame(BytesIO(b"{}"))

    def test_rejects_path_as_identifier(self) -> None:
        with self.assertRaises(bridge.BridgeError) as caught:
            bridge.job_get(self.session, "../status")
        self.assertEqual(caught.exception.code, "invalid_id")
        with self.assertRaises(bridge.BridgeError) as caught_sess:
            bridge.job_get("../escape", self.job)
        self.assertEqual(caught_sess.exception.code, "invalid_id")

    def test_standalone_session_alias_resolves_cleanly(self) -> None:
        self.assertEqual(bridge.resolve_session_id("standalone"), bridge.STANDALONE_SESSION)
        self.assertEqual(bridge.resolve_session_id(None), bridge.STANDALONE_SESSION)
        self.assertEqual(bridge.resolve_session_id(self.session), self.session)

    def test_standalone_retry_compares_context_of_existing_job(self) -> None:
        netlist = "R:R1 IN 0 R=50 Ohm\n"
        first, second = (Path(self.directory.name) / name for name in ("A", "B"))
        job = bridge.job_directory("standalone", self.job)
        job.mkdir(parents=True)
        # 2.0.0 recorded these directories under context, outside the request.
        saved = {
            "state": "succeeded",
            "request": {"source": "inline_netlist", "revision": hashlib.sha256(netlist.encode()).hexdigest(),
                        "block": None, "axis": None, "variable": None},
            "context": {"data_dir": str(first.resolve()), "model_dirs": [str(first.resolve()), str(second.resolve())]},
        }
        bridge.write_json(job / "status.json", saved)
        with patch.object(bridge.subprocess, "Popen") as launch:
            result = bridge.simulate_netlist(netlist, request_id=self.job, data_dir=first / ".",
                                             model_dirs=[first, second])
            self.assertEqual(result, saved)
            for data_dir, model_dirs in [(second, [first, second]), (first, [second, first]), (first, [])]:
                with self.subTest(data_dir=data_dir, model_dirs=model_dirs):
                    with self.assertRaises(bridge.BridgeError) as caught:
                        bridge.simulate_netlist(netlist, request_id=self.job, data_dir=data_dir, model_dirs=model_dirs)
                    self.assertEqual(caught.exception.code, "request_conflict")
            launch.assert_not_called()
        self.assertEqual(bridge.job_get("standalone", self.job), saved)

    def test_standalone_retry_cannot_overwrite_a_reserved_job(self) -> None:
        job = bridge.job_directory("standalone", self.job)
        job.mkdir(parents=True)
        with self.assertRaises(bridge.BridgeError) as caught:
            bridge.simulate_netlist("R:R1 IN 0 R=50 Ohm\n", request_id=self.job)
        self.assertEqual(caught.exception.code, "request_in_progress")
        self.assertFalse((job / "snapshot.ckt").exists())

    def test_cli_netlist_check_fails_with_json(self) -> None:
        netlist = Path(self.directory.name) / "bad.ckt"
        netlist.write_text("R:R1 IN 0 R=missing\n", encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", bridge.__file__, "netlist-check", str(netlist), "--mode", "dut"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_cli_simulate_exit_tracks_terminal_job_state(self) -> None:
        for state, expected in [("succeeded", 0), ("failed", 1), ("cancelled", 1), ("outcome_unknown", 1)]:
            with self.subTest(state=state):
                with patch.object(sys, "argv", ["bridge.py", "simulate", "test.ckt"]):
                    with patch.object(bridge, "simulate_netlist", return_value={"state": state}):
                        with patch("builtins.print") as output:
                            self.assertEqual(bridge.main(), expected)
                            self.assertEqual(json.loads(output.call_args.args[0]), {"state": state})

    def test_norm_path_key_case_insensitive_on_windows(self) -> None:
        p1 = Path(self.directory.name) / "SubFolder_WRK"
        p1.mkdir()
        if os.name == "nt":
            self.assertEqual(bridge.norm_path_key(str(p1).upper()), bridge.norm_path_key(str(p1).lower()))

    def test_completed_job_and_catalog_are_available_offline(self) -> None:
        path = bridge.job_directory(self.session, self.job)
        path.mkdir(parents=True)
        bridge.write_json(path / "status.json", {"state": "succeeded"})
        bridge.write_json(path / "catalog.json", {"blocks": ["AC1.AC"]})
        self.assertEqual(bridge.dataset_catalog(self.session, self.job), {"blocks": ["AC1.AC"]})
        self.assertEqual(bridge.job_list(self.session)[0]["state"], "succeeded")
        with self.assertRaises(bridge.BridgeError) as caught:
            bridge.job_result(self.session, self.job)
        self.assertEqual(caught.exception.code, "result_unavailable")

    def test_abandoned_worker_is_reconciled_as_unknown(self) -> None:
        path = bridge.job_directory(self.session, self.job)
        path.mkdir(parents=True)
        bridge.write_json(path / "status.json", {"state": "running", "submitted_at": time.time() - 90})
        self.assertEqual(bridge.job_get(self.session, self.job)["state"], "outcome_unknown")

    def test_job_cancel_marks_running_job_cancelled(self) -> None:
        path = bridge.job_directory("standalone", self.job)
        path.mkdir(parents=True)
        bridge.write_json(path / "status.json", {"state": "running", "submitted_at": time.time(), "worker_pid": None})
        cancelled = bridge.job_cancel("standalone", self.job)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(bridge.job_get("standalone", self.job)["state"], "cancelled")

    def test_an_interrupted_write_is_not_reported_saved(self) -> None:
        path = Path(self.directory.name) / "requests" / self.session / f"{self.job}.json"
        path.parent.mkdir(parents=True)
        bridge.write_json(path, {"result": {"state": "committed"}})
        self.assertEqual(bridge.request_get(self.session, self.job)["state"], "outcome_unknown")

    def test_async_simulate_persists_worker_pid_and_cancels_process_tree(self) -> None:
        fake_ads = Path(self.directory.name) / "fake_ads"
        (fake_ads / "tools" / "python").mkdir(parents=True)
        (fake_ads / "tools" / "python" / "python.exe").write_bytes(b"")

        class DummyProc:
            pid = 424242

        with patch.object(bridge, "_resolve_ads_root", return_value=fake_ads), \
             patch.object(bridge.subprocess, "Popen", return_value=DummyProc()), \
             patch.object(bridge, "_kill_process_tree", return_value=True) as kill_mock:
            submitted = bridge.simulate_netlist("R:R1 IN 0 R=50 Ohm\n", request_id=self.job, wait=False)
            self.assertEqual(submitted["worker_pid"], 424242)
            self.assertEqual(bridge.job_get("standalone", self.job)["worker_pid"], 424242)

            cancelled = bridge.job_cancel("standalone", self.job)
            kill_mock.assert_called_once_with(424242)
            self.assertEqual(cancelled["state"], "cancelled")
            self.assertTrue(cancelled["process_tree_terminated"])
            self.assertEqual(cancelled["worker_pid"], 424242)

    def test_simulate_timeout_kills_spawned_worker_tree(self) -> None:
        fake_ads = Path(self.directory.name) / "fake_ads_timeout"
        (fake_ads / "tools" / "python").mkdir(parents=True)
        (fake_ads / "tools" / "python" / "python.exe").write_bytes(b"")

        class HangingProc:
            pid = 515151

            def wait(self, timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired(cmd="worker.py", timeout=timeout or 0.01)

        with patch.object(bridge, "_resolve_ads_root", return_value=fake_ads), \
             patch.object(bridge.subprocess, "Popen", return_value=HangingProc()), \
             patch.object(bridge, "_kill_process_tree", return_value=True) as kill_mock:
            with self.assertRaises(bridge.BridgeError) as caught:
                bridge.simulate_netlist("R:R1 IN 0 R=50 Ohm\n", request_id=self.job, wait=True, timeout=0.01)
            self.assertEqual(caught.exception.code, "simulation_timeout")
            kill_mock.assert_called_once_with(515151)
            final = bridge.job_get("standalone", self.job)
            self.assertEqual(final["state"], "cancelled")
            self.assertTrue(final["process_tree_terminated"])

    def test_worker_cannot_clobber_concurrent_cancellation(self) -> None:
        job_dir = bridge.job_directory("standalone", self.job)
        job_dir.mkdir(parents=True)
        status_path = job_dir / "status.json"
        initial = {
            "job_id": self.job,
            "session_id": bridge.STANDALONE_SESSION,
            "request": {"source": "inline_netlist", "revision": "abc", "block": None, "axis": None, "variable": None},
            "state": "starting",
            "submitted_at": time.time(),
        }
        bridge.write_json(status_path, initial)
        (job_dir / "snapshot.ckt").write_text("R:R1 IN 0 R=50 Ohm\n", encoding="utf-8")

        real_write_json = worker.write_json

        def racing_write_json(path: Path, value: dict) -> None:
            if path == status_path and value.get("state") == "running":
                # Simulate job_cancel arriving right after worker read status='starting'
                bridge.write_json(job_dir / "cancel.flag", {
                    "state": "cancelled",
                    "cancelled_at": time.time(),
                    "process_tree_terminated": False,
                    "message": "Simulation job cancelled by client.",
                })
            real_write_json(path, value)

        with patch.object(worker, "write_json", side_effect=racing_write_json):
            rc = worker.run(job_dir)
        self.assertEqual(rc, 1)
        self.assertEqual(bridge.job_get("standalone", self.job)["state"], "cancelled")

    def test_dataset_analyze_flags_out_of_range_at_samples(self) -> None:
        class DummyVar:
            def __init__(self, name: str):
                self.name = name

        class DummySeries:
            def __init__(self, values: list):
                self._values = values

            def __iter__(self):
                return iter(self._values)

        class DummyTable:
            def __init__(self, rows: dict[str, list]):
                self._rows = rows

            def reset_index(self):
                return self

            def __len__(self) -> int:
                return len(next(iter(self._rows.values())))

            def __getitem__(self, key: str):
                return DummySeries(self._rows[key])

        class DummyBlock:
            name = "AC1.AC"
            ivars = [DummyVar("freq")]
            dvars = [DummyVar("VOUT")]

            def to_dataframe(self):
                return DummyTable({
                    "freq": [1.0, 10.0, 1000.0],
                    "VOUT": [1.0 + 0.0j, 0.8 - 0.2j, 0.1 - 0.05j],
                })

        class DummyDataset(dict):
            path = Path("synthetic.ds")

        ds = DummyDataset({"AC1.AC": DummyBlock()})
        analyzed = worker.analyze_dataset(ds, "AC1.AC", "freq", signals=["VOUT"], at=[1.0, 505.0, 1e9, -2.0])
        self.assertEqual(analyzed["out_of_range_at"], [1e9, -2.0])
        samples = analyzed["signals"]["VOUT"]["samples_at"]
        self.assertTrue(samples[0]["in_range"])
        self.assertEqual(samples[0]["status"], "exact")
        self.assertAlmostEqual(samples[0]["real"], 1.0)
        self.assertTrue(samples[1]["in_range"])
        self.assertEqual(samples[1]["status"], "interpolated")
        self.assertIsNotNone(samples[1]["real"])
        for oob in (samples[2], samples[3]):
            self.assertFalse(oob["in_range"])
            self.assertEqual(oob["status"], "out_of_range")
            self.assertIsNone(oob["real"])
            self.assertIsNone(oob["magnitude"])
            self.assertIsNotNone(oob["clamped_endpoint"]["real"])


if __name__ == "__main__":
    unittest.main()
