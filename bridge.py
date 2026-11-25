"""Unified Python SDK and CLI for the Keysight ADS 2026 bridge (GUI + Standalone Headless)."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any
import uuid


MAX_FRAME = 4 * 1024 * 1024
PROTOCOL = 1
VERSION = "2.3.5"
ParameterValue = str | list[str | list[str]]
STANDALONE_SESSION = "0" * 32


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def norm_path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def runtime_dir() -> Path:
    override = os.environ.get("ADS_BRIDGE_RUNTIME")
    if override:
        return Path(override).resolve()
    # Default under Path.home() rather than %LOCALAPPDATA% because Microsoft Store Python
    # virtualizes %LOCALAPPDATA% into Packages\...\LocalCache, hiding IPC files from Win32 ADS.
    default_dir = (Path.home() / ".ads_bridge").resolve()
    ensure_private_dir(default_dir)
    return default_dir


def ensure_private_dir(directory: Path) -> None:
    """Create directory and restrict Windows ACL to current user when newly created."""
    existed = directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    if not existed and os.name == "nt":
        username = os.environ.get("USERNAME")
        userdomain = os.environ.get("USERDOMAIN")
        principal = f"{userdomain}\\{username}" if userdomain and username else username
        if principal:
            try:
                subprocess.run(
                    ["icacls", str(directory), "/inheritance:r", "/grant:r", f"{principal}:(OI)(CI)F"],
                    capture_output=True,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise BridgeError("invalid_id", "Expected a 32-character lowercase hexadecimal ID.")
    return value


def resolve_session_id(value: str | None) -> str:
    if value in {None, "", "standalone"}:
        return STANDALONE_SESSION
    return identifier(value)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_frame(stream: Any) -> Any:
    line = stream.readline(MAX_FRAME + 1)
    if len(line) > MAX_FRAME or not line.endswith(b"\n"):
        raise BridgeError("invalid_frame", "Missing, incomplete or oversized JSON frame.")
    return json.loads(line)


def frame(value: Any) -> bytes:
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    if len(data) > MAX_FRAME:
        raise BridgeError("response_too_large", "The response exceeds the bridge size limit.")
    return data


def _pid_is_alive(pid: int) -> bool:
    """Fast (<0.1ms) Windows process liveness check to prune crashed/stale ADS session files."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # ERROR_ACCESS_DENIED (5) means process exists under higher privilege
            return ctypes.GetLastError() == 5
        try:
            exit_code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return int(exit_code.value) == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def list_sessions(*, prune_stale: bool = True) -> list[dict[str, Any]]:
    sessions = []
    for path in sorted((runtime_dir() / "sessions").glob("*.json")):
        try:
            entry = read_json(path)
            info = {key: entry[key] for key in ("session_id", "pid", "ads_version")}
            if "bridge_version" in entry:
                info["bridge_version"] = entry["bridge_version"]
            identifier(info["session_id"])
            if entry["protocol"] != PROTOCOL or path.stem != info["session_id"]:
                continue
            if prune_stale and not _pid_is_alive(int(info["pid"])):
                path.unlink(missing_ok=True)
                continue
            try:
                call(info["session_id"], "ping", timeout=0.4)
                info["reachable"] = True
            except BridgeError:
                info["reachable"] = False
            sessions.append(info)
        except (BridgeError, OSError, KeyError, ValueError, TypeError):
            continue
    return sessions


def call(session_id: str, operation: str, timeout: float = 20, **arguments: Any) -> Any:
    sid = identifier(session_id)
    try:
        entry = read_json(runtime_dir() / "sessions" / f"{sid}.json")
    except FileNotFoundError as exc:
        raise BridgeError("session_offline", f"No registered ADS session {sid}.") from exc
    if entry["protocol"] != PROTOCOL:
        raise BridgeError("protocol_mismatch", "The ADS session uses a different bridge protocol.")
    request = {"token": entry["token"], "op": operation, "args": arguments}
    try:
        with socket.create_connection(("127.0.0.1", entry["port"]), timeout=timeout) as connection:
            connection.sendall(frame(request))
            with connection.makefile("rb") as reader:
                response = read_frame(reader)
    except TimeoutError as exc:
        raise BridgeError(
            "request_timeout",
            "ADS response timed out. For edits or simulations, reconcile or look up the original request ID.",
        ) from exc
    except OSError as exc:
        raise BridgeError("session_offline", f"ADS session {sid} could not be reached.") from exc
    if "error" in response:
        raise BridgeError(response["error"]["code"], response["error"]["message"])
    return response["result"]


def job_directory(session_id: str, job_id: str) -> Path:
    return runtime_dir() / "jobs" / resolve_session_id(session_id) / identifier(job_id)


def worker_active(job: Path) -> bool:
    try:
        with (job / "worker.lock").open("r+b") as stream:
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            else:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                return False
    except FileNotFoundError:
        return False


def _read_worker_pid(directory: Path, status: dict[str, Any]) -> int | None:
    pid = status.get("worker_pid")
    if isinstance(pid, int) and pid > 0:
        return pid
    pid_path = directory / "worker.pid"
    if pid_path.is_file():
        try:
            recorded = read_json(pid_path).get("pid")
            if isinstance(recorded, int) and recorded > 0:
                return recorded
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return None


def _kill_process_tree(pid: int) -> bool:
    if os.name == "nt":
        proc = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    try:
        os.kill(pid, 9)
        return True
    except OSError:
        return False


def job_get(session_id: str, job_id: str) -> dict[str, Any]:
    directory = job_directory(session_id, job_id)
    path = directory / "status.json"
    try:
        status = read_json(path)
    except FileNotFoundError as exc:
        raise BridgeError("job_not_found", f"No job {job_id} for this ADS session.") from exc

    pid = _read_worker_pid(directory, status)
    if pid is not None and status.get("worker_pid") is None:
        status["worker_pid"] = pid

    cancel_path = directory / "cancel.flag"
    if status["state"] in {"starting", "running"} and cancel_path.is_file():
        try:
            cancel_meta = read_json(cancel_path)
            if isinstance(cancel_meta, dict):
                status.update(cancel_meta)
        except (OSError, ValueError, TypeError):
            status["state"] = "cancelled"
        return status

    if (
        status["state"] in {"starting", "running"}
        and "submitted_at" in status
        and time.time() - status["submitted_at"] > 20
        and not worker_active(directory)
    ):
        status = read_json(path)
        if status["state"] in {"starting", "running"}:
            status.update(state="outcome_unknown", message="No worker owns the job. Inspect its logs before resubmitting.")
    return status


def job_cancel(session_id: str, job_id: str) -> dict[str, Any]:
    """Terminate an active simulation worker and its entire hpeesofsim process tree on Windows."""
    directory = job_directory(session_id, job_id)
    status_path = directory / "status.json"
    cancel_path = directory / "cancel.flag"
    status = job_get(session_id, job_id)
    if status["state"] not in {"starting", "running"}:
        return status

    cancelled_at = time.time()
    pid = _read_worker_pid(directory, status)
    cancel_meta: dict[str, Any] = {
        "state": "cancelled",
        "cancelled_at": cancelled_at,
        "process_tree_terminated": False,
        "message": "Simulation job cancelled by client.",
    }
    if pid is not None:
        cancel_meta["worker_pid"] = pid
    # Persist cancel.flag before killing or updating status.json so worker.py cannot clobber cancellation.
    write_json(cancel_path, cancel_meta)

    killed = False
    if pid is None:
        pid = _read_worker_pid(directory, status)
    if pid is not None:
        killed = _kill_process_tree(pid)
        cancel_meta["worker_pid"] = pid
        cancel_meta["process_tree_terminated"] = killed
        write_json(cancel_path, cancel_meta)

    status.update(cancel_meta)
    write_json(status_path, status)
    return status


def job_result(session_id: str, job_id: str) -> dict[str, Any]:
    status = job_get(session_id, job_id)
    if status["state"] != "succeeded":
        raise BridgeError("result_unavailable", f"Job state is {status['state']}.")
    try:
        return read_json(job_directory(session_id, job_id) / "result.json")
    except FileNotFoundError as exc:
        raise BridgeError("result_unavailable", "No preview selected. Use dataset.catalog and dataset.read.") from exc


def request_get(session_id: str, request_id: str) -> dict[str, Any]:
    path = runtime_dir() / "requests" / identifier(session_id) / f"{identifier(request_id)}.json"
    try:
        result = read_json(path)["result"]
    except FileNotFoundError as exc:
        raise BridgeError("request_not_found", "No persisted request has this ID.") from exc
    if result["state"] in {"applying", "creating", "committed"}:
        result["state"] = "outcome_unknown"
    return result


def job_list(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    sid = resolve_session_id(session_id)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise BridgeError("invalid_argument", "Job list limit must be between 1 and 200.")
    paths = (runtime_dir() / "jobs" / sid).glob("*/status.json")
    jobs = sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)
    return [job_get(sid, path.parent.name) for path in jobs[:limit]]


def job_log(session_id: str, job_id: str, stream: str = "sim") -> dict[str, Any]:
    job_get(session_id, job_id)
    if stream not in {"sim", "worker"}:
        raise BridgeError("invalid_argument", "Choose the sim or worker log.")
    path = job_directory(session_id, job_id) / f"{stream}.log"
    try:
        with path.open("rb") as log:
            size = log.seek(0, os.SEEK_END)
            log.seek(max(0, size - 16000))
            return {
                "stream": stream,
                "tail": log.read(16000).decode("utf-8", errors="replace"),
                "truncated": size > 16000,
            }
    except FileNotFoundError as exc:
        raise BridgeError("log_unavailable", "This job has not created that log.") from exc


def job_context(session_id: str, job_id: str) -> tuple[Path, dict[str, Any]]:
    status = job_get(session_id, job_id)
    if status["state"] != "succeeded":
        raise BridgeError("result_unavailable", f"Job state is {status['state']}.")
    return job_directory(session_id, job_id), status


def dataset_catalog(session_id: str, job_id: str) -> dict[str, Any]:
    directory, status = job_context(session_id, job_id)
    catalog = directory / "catalog.json"
    return read_json(catalog) if catalog.exists() else query_dataset(directory, status)


def dataset_read(
    session_id: str,
    job_id: str,
    block: str,
    axis: str,
    variable: str,
    offset: int = 0,
    limit: int = 250,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    directory, status = job_context(session_id, job_id)
    if type(limit) is not int or type(offset) is not int or offset < 0 or not 1 <= limit <= 1000:
        raise BridgeError("invalid_argument", "Use nonnegative offset and limit from 1 through 1000.")
    if any(not isinstance(value, str) or not 0 < len(value) <= 256 for value in (block, axis, variable)):
        raise BridgeError("invalid_argument", "Block, axis and variable must be nonempty strings up to 256 characters.")
    if filters is not None and (not isinstance(filters, dict) or len(filters) > 20):
        raise BridgeError("invalid_argument", "Filters must be an object with at most 20 axis selections.")
    return query_dataset(
        directory,
        status,
        dict(block=block, axis=axis, variable=variable, offset=offset, limit=limit, filters=filters),
    )


def dataset_table(
    session_id: str,
    job_id: str,
    block: str,
    columns: list[str],
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    directory, status = job_context(session_id, job_id)
    if not isinstance(columns, list) or not 1 <= len(columns) <= 20:
        raise BridgeError("invalid_argument", "Choose 1 through 20 dataset columns.")
    if any(not isinstance(value, str) or not 0 < len(value) <= 256 for value in [block, *columns]):
        raise BridgeError("invalid_argument", "Block and column names must be nonempty strings up to 256 characters.")
    if type(limit) is not int or type(offset) is not int or offset < 0 or not 1 <= limit <= 250:
        raise BridgeError("invalid_argument", "Use nonnegative offset and limit from 1 through 250.")
    return query_dataset(directory, status, dict(table=True, block=block, columns=columns, offset=offset, limit=limit))


def dataset_analyze(
    session_id: str,
    job_id: str,
    block: str,
    axis: str,
    signals: list[str] | None = None,
    at: list[float] | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute engineering signal metrics, -3 dB cutoffs, S-param checks, and point interpolations."""
    directory, status = job_context(session_id, job_id)
    if any(not isinstance(value, str) or not 0 < len(value) <= 256 for value in (block, axis)):
        raise BridgeError("invalid_argument", "Block and axis must be nonempty strings up to 256 characters.")
    if signals is not None and (not isinstance(signals, list) or not 1 <= len(signals) <= 32):
        raise BridgeError("invalid_argument", "signals must be a list of 1 through 32 variable names.")
    if at is not None and (not isinstance(at, list) or len(at) > 200 or any(type(x) not in (int, float) for x in at)):
        raise BridgeError("invalid_argument", "at must be a numeric list of up to 200 coordinate points.")
    return query_dataset(
        directory,
        status,
        dict(analyze=True, block=block, axis=axis, signals=signals, at=at, filters=filters),
    )


def _import_rf_module(name: str) -> Any:
    """Import an optional NumPy/SciPy/scikit-rf bridge module with an actionable BridgeError if missing."""
    try:
        return __import__(name)
    except ModuleNotFoundError as exc:
        raise BridgeError(
            "dependency_missing",
            f"Missing Python dependency '{exc.name}' required for '{name}'. "
            "Install RF dependencies with `pip install numpy scipy scikit-rf` or run with the ADS Bridge venv Python.",
        ) from exc


def dataset_compress(
    session_id_or_ds: str | Path,
    job_id: str | None = None,
    *,
    block: str | None = None,
    axis: str | None = None,
    signals: list[str] | None = None,
    max_curve_points: int = 24,
    band_hz: list[float] | None = None,
    load_ohm: float = 50.0,
    return_loss_spec_db: float = -10.0,
    export_touchstone: bool = False,
    vds_v: float | None = None,
    vds_signal: str | None = None,
    ids_signal: str | None = None,
    vout_signal: str | None = None,
    pin_dbm: float | None = None,
    filters: dict[str, Any] | None = None,
    sweep_axis: str | None = None,
    pin_from_sweep_axis: bool = False,
) -> dict[str, Any]:
    """Compress a simulation dataset block (or 2D sweep along `sweep_axis`) into compact RF/circuit JSON.

    Accepts either `(session_id, job_id)` or a direct `.ds` file path as the first argument (`job_id=None`).
    """
    rf_compress = _import_rf_module("rf_compress")

    if type(max_curve_points) is not int or not 4 <= max_curve_points <= 200:
        raise BridgeError("invalid_argument", "max_curve_points must be an integer between 4 and 200.")

    direct_ds: Path | None = None
    if job_id is None:
        cand = Path(session_id_or_ds).resolve()
        if not cand.is_file():
            raise BridgeError("file_not_found", f"Dataset file not found: {cand} (or pass both session_id and job_id).")
        direct_ds = cand
        directory = cand.parent
        status: dict[str, Any] = {"dataset": str(cand)}
        resolved_sid: str | None = None
    else:
        directory, status = job_context(str(session_id_or_ds), job_id)
        resolved_sid = resolve_session_id(str(session_id_or_ds))

    if sweep_axis:
        raw_sw = query_dataset(
            directory,
            status,
            dict(
                extract_sweep=True,
                sweep_axis=sweep_axis,
                block=block,
                axis=axis,
                signals=signals,
                filters=filters,
            ),
        )
        try:
            compressed = rf_compress.compress_sweep_slices(
                raw_sw["axis"],
                raw_sw["sweep_axis"],
                raw_sw["slices"],
                block_name=raw_sw["block"],
                max_curve_points=max_curve_points,
                band_hz=band_hz,
                load_ohm=load_ohm,
                return_loss_spec_db=return_loss_spec_db,
                vds_v=vds_v,
                vds_signal=vds_signal,
                ids_signal=ids_signal,
                vout_signal=vout_signal,
                pin_dbm=pin_dbm,
                pin_from_sweep_axis=pin_from_sweep_axis,
            )
        except (ValueError, RuntimeError) as exc:
            raise BridgeError("dataset_compress_failed", str(exc)) from exc
        if job_id is not None:
            compressed["job_id"] = job_id
            compressed["session_id"] = resolved_sid
        if direct_ds is not None:
            compressed["dataset"] = str(direct_ds)
        return compressed

    raw = query_dataset(
        directory,
        status,
        dict(extract_block=True, block=block, axis=axis, signals=signals, filters=filters),
    )
    sig_complex = {
        name: rf_compress.decode_complex_pairs(pairs)
        for name, pairs in raw["signals"].items()
    }
    ts_path = (directory / f"{Path(status['dataset']).stem}.s2p") if export_touchstone else None
    try:
        compressed = rf_compress.compress_waveform_block(
            raw["axis"],
            raw["axis_values"],
            sig_complex,
            block_name=raw["block"],
            max_curve_points=max_curve_points,
            band_hz=band_hz,
            load_ohm=load_ohm,
            return_loss_spec_db=return_loss_spec_db,
            touchstone_path=ts_path,
            vds_v=vds_v,
            vds_signal=vds_signal,
            ids_signal=ids_signal,
            vout_signal=vout_signal,
            pin_dbm=pin_dbm,
            outer_axes=raw.get("outer_axes") or None,
        )
    except (ValueError, RuntimeError) as exc:
        raise BridgeError("dataset_compress_failed", str(exc)) from exc
    if job_id is not None:
        compressed["job_id"] = job_id
        compressed["session_id"] = resolved_sid
    if direct_ds is not None:
        compressed["dataset"] = str(direct_ds)
    return compressed


def touchstone_compress(
    path: str | Path,
    *,
    max_curve_points: int = 24,
    band_hz: list[float] | None = None,
    return_loss_spec_db: float = -10.0,
) -> dict[str, Any]:
    """Compress a Touchstone (.s1p/.s2p/.sNp) file into compact RF engineering JSON via scikit-rf."""
    rf_compress = _import_rf_module("rf_compress")

    if type(max_curve_points) is not int or not 4 <= max_curve_points <= 200:
        raise BridgeError("invalid_argument", "max_curve_points must be an integer between 4 and 200.")
    try:
        return rf_compress.compress_touchstone_file(
            path,
            max_curve_points=max_curve_points,
            band_hz=band_hz,
            return_loss_spec_db=return_loss_spec_db,
        )
    except FileNotFoundError as exc:
        raise BridgeError("file_not_found", str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise BridgeError("touchstone_compress_failed", str(exc)) from exc


def ads_environment(root: str | Path) -> dict[str, str]:
    root_path = Path(root)
    env = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "KEYSIGHT_ADS_DATASET_DYNAMIC"):
        env.pop(name, None)
    env["HPEESOF_DIR"] = str(root_path)
    env["PATH"] = str(root_path / "bin") + os.pathsep + env.get("PATH", "")
    return env


def _resolve_ads_root(configured: str | Path | None = None) -> Path:
    import docs_pdk
    return docs_pdk.resolve_ads_root(configured)


def query_dataset(directory: Path, status: dict[str, Any], arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    root = _resolve_ads_root(status.get("ads_root"))
    python = root / "tools/python/python.exe"
    dataset = directory / Path(status["dataset"]).name
    command = [str(python), "-B", str(Path(__file__).with_name("worker.py")), "query", str(dataset)]
    if arguments is not None:
        command.append(json.dumps(arguments, allow_nan=False))
    try:
        reply = subprocess.run(
            command,
            env=ads_environment(root),
            cwd=directory,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError("dataset_reader_unavailable", str(exc)) from exc
    if reply.returncode:
        raise BridgeError("dataset_query_failed", reply.stderr[-3000:].decode("utf-8", errors="replace").strip())
    lines = [ln for ln in reply.stdout.splitlines() if ln.strip()]
    if not lines:
        raise BridgeError("dataset_query_failed", "Dataset worker returned empty output.")
    return json.loads(lines[-1])


def _load_netlist_input(netlist_or_path: str | Path) -> tuple[str, Path | None]:
    if isinstance(netlist_or_path, Path):
        p = netlist_or_path.resolve()
        if not p.is_file():
            raise BridgeError("file_not_found", f"Netlist file not found: {p}")
        return p.read_text(encoding="utf-8-sig"), p
    if not isinstance(netlist_or_path, str) or not netlist_or_path.strip("\ufeff \t\r\n"):
        raise BridgeError("invalid_argument", "Provide a non-empty netlist string, handle (nl_...), or file path.")
    cleaned = netlist_or_path.lstrip("\ufeff")
    stripped = cleaned.strip()
    if "\n" not in cleaned and len(stripped) <= 32 and stripped.startswith("nl_"):
        import rf_workflow
        if rf_workflow.is_netlist_handle(stripped):
            try:
                return rf_workflow.resolve_netlist_handle(runtime_dir(), stripped, ensure_private_dir)
            except FileNotFoundError as exc:
                raise BridgeError("netlist_not_found", str(exc)) from exc
    if "\n" not in cleaned and len(cleaned) <= 400:
        candidate = Path(cleaned)
        if candidate.suffix.lower() in {".ckt", ".net", ".scs", ".sp"} or candidate.exists():
            p = candidate.resolve()
            if not p.is_file():
                raise BridgeError("file_not_found", f"Netlist file not found: {p}")
            return p.read_text(encoding="utf-8-sig"), p
    return cleaned, None


def netlist_inspect(netlist_or_path: str | Path, *, search_dirs: list[str | Path] | None = None) -> dict[str, Any]:
    import netlist_parser
    text, src_path = _load_netlist_input(netlist_or_path)
    dirs = list(search_dirs or [])
    if src_path is not None and src_path.parent not in dirs:
        dirs.insert(0, src_path.parent)
    try:
        parsed = netlist_parser.inspect_netlist(text, search_dirs=dirs)
    except netlist_parser.NetlistError as exc:
        raise BridgeError("invalid_netlist", str(exc)) from exc
    if src_path is not None:
        parsed["source_file"] = str(src_path)
    return parsed


def netlist_check(
    netlist_or_path: str | Path,
    *,
    mode: str = "tb",
    search_dirs: list[str | Path] | None = None,
) -> dict[str, Any]:
    import netlist_parser
    text, src_path = _load_netlist_input(netlist_or_path)
    dirs = list(search_dirs or [])
    if src_path is not None and src_path.parent not in dirs:
        dirs.insert(0, src_path.parent)
    try:
        report = netlist_parser.check_netlist(text, mode=mode, search_dirs=dirs)
    except ValueError as exc:
        raise BridgeError("invalid_argument", str(exc)) from exc
    if src_path is not None:
        report["source_file"] = str(src_path)
    return report


def netlist_compress(
    netlist_or_path: str | Path,
    *,
    search_dirs: list[str | Path] | None = None,
    include_models: bool = True,
    include_subcircuits: bool = True,
    format: str = "json",
    store_handle: bool = False,
) -> dict[str, Any]:
    """Extract compact circuit parameters, topology, and tunable parameter refs from an ADS netlist."""
    import netlist_parser

    text, src_path = _load_netlist_input(netlist_or_path)
    dirs = list(search_dirs or [])
    if src_path is not None and src_path.parent not in dirs:
        dirs.insert(0, src_path.parent)
    try:
        compact = netlist_parser.compress_netlist(
            text,
            search_dirs=dirs,
            include_models=include_models,
            include_subcircuits=include_subcircuits,
            format=format,
        )
    except netlist_parser.NetlistError as exc:
        raise BridgeError("invalid_netlist", str(exc)) from exc
    if store_handle:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        handle = f"nl_{digest[:12]}"
        store_dir = runtime_dir() / "netlists"
        ensure_private_dir(store_dir)
        dest = store_dir / f"{handle}.ckt"
        if not dest.is_file():
            dest.write_text(text, encoding="utf-8")
        compact["netlist_id"] = handle
        compact["handle"] = handle
    if src_path is not None:
        compact["source_file"] = str(src_path)
    return compact


def netlist_register(
    netlist_or_path: str | Path,
    *,
    compact_format: str = "tabular",
    include_models: bool = True,
    include_subcircuits: bool = True,
    search_dirs: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Register an ADS netlist under a short handle (`nl_<12hex>`) so future calls avoid resending netlist text."""
    import netlist_parser
    import rf_workflow

    text, src_path = _load_netlist_input(netlist_or_path)
    dirs = list(search_dirs or [])
    if src_path is not None and src_path.parent not in dirs:
        dirs.insert(0, src_path.parent)
    try:
        return rf_workflow.register_netlist(
            text,
            runtime_dir(),
            ensure_private_dir,
            compact_format=compact_format,
            include_models=include_models,
            include_subcircuits=include_subcircuits,
            search_dirs=dirs,
        )
    except (netlist_parser.NetlistError, ValueError) as exc:
        raise BridgeError("invalid_netlist", str(exc)) from exc


def netlist_patch(
    netlist_or_handle: str | Path,
    updates: dict[str, str | float | int],
) -> dict[str, Any]:
    """Patch parameters on a registered netlist handle or file and return only the new handle + parameter delta."""
    import netlist_parser
    import rf_workflow

    parent_id = netlist_or_handle.strip() if rf_workflow.is_netlist_handle(netlist_or_handle) else None
    text, _ = _load_netlist_input(netlist_or_handle)
    try:
        return rf_workflow.patch_netlist(
            text,
            updates,
            runtime_dir(),
            ensure_private_dir,
            parent_id=parent_id,
        )
    except (netlist_parser.NetlistError, ValueError) as exc:
        raise BridgeError("invalid_argument", str(exc)) from exc


def job_log_summary(session_id_or_job: str, job_id: str | None = None, max_items: int = 8) -> dict[str, Any]:
    """Return a distilled (<300-token) simulation error/warning fault card for any job (defaults to standalone session)."""
    import rf_workflow

    if job_id is None:
        sid = STANDALONE_SESSION
        jid = session_id_or_job
    else:
        sid = resolve_session_id(session_id_or_job)
        jid = job_id
    status = job_get(sid, jid)
    directory = job_directory(sid, jid)
    summary = rf_workflow.distill_job_log(directory, max_items=max_items)
    summary["job_id"] = jid
    summary["session_id"] = sid
    summary["state"] = status.get("state")
    if status.get("code"):
        summary["code"] = status["code"]
    return summary


def simulation_context_key(context: dict[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    data_dir = context.get("data_dir")
    return (
        os.path.normcase(data_dir) if data_dir else None,
        tuple(os.path.normcase(path) for path in context.get("model_dirs", [])),
    )


_simulation_context_key = simulation_context_key


def simulate_netlist(
    netlist_or_path: str | Path,
    *,
    session_id: str = STANDALONE_SESSION,
    request_id: str | None = None,
    block: str | None = None,
    axis: str | None = None,
    variable: str | None = None,
    data_dir: str | Path | None = None,
    model_dirs: list[str | Path] | None = None,
    sweep: dict[str, Any] | None = None,
    save_to_data_dir: bool = False,
    dataset_name: str | None = None,
    wait: bool = True,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Run an ADS netlist directly using standalone 64-bit ADS Python (no GUI session required)."""
    sid = resolve_session_id(session_id)
    job_id = identifier(request_id) if request_id is not None else uuid.uuid4().hex
    trace = (block, axis, variable)
    if any(v is not None for v in trace) and not all(isinstance(v, str) and 0 < len(v) <= 256 for v in trace):
        raise BridgeError("invalid_argument", "Provide block, axis, and variable together or omit all three.")

    netlist_text, src_path = _load_netlist_input(netlist_or_path)
    if sweep is not None:
        import netlist_parser
        if not isinstance(sweep, dict) or not sweep.get("var"):
            raise BridgeError("invalid_argument", "sweep must be an object with 'var', 'start', 'stop', and 'step'/'lin'/'dec'.")
        try:
            netlist_text = netlist_parser.inject_sweep_plan(
                netlist_text,
                var=str(sweep["var"]),
                start=sweep["start"],
                stop=sweep["stop"],
                step=sweep.get("step"),
                dec=sweep.get("dec"),
                lin=sweep.get("lin"),
                analysis=sweep.get("analysis"),
            )
        except (KeyError, ValueError, netlist_parser.NetlistError) as exc:
            raise BridgeError("invalid_argument", str(exc)) from exc
    revision = hashlib.sha256(netlist_text.encode("utf-8")).hexdigest()
    spec = {
        "source": str(src_path) if src_path else "inline_netlist",
        "revision": revision,
        "block": block,
        "axis": axis,
        "variable": variable,
    }
    context = {
        "data_dir": str(Path(data_dir).resolve()) if data_dir else None,
        "model_dirs": [str(Path(path).resolve()) for path in (model_dirs or [])],
    }
    job = job_directory(sid, job_id)
    try:
        job.mkdir(parents=True)
    except FileExistsError:
        try:
            current = job_get(sid, job_id)
        except BridgeError as exc:
            if exc.code != "job_not_found":
                raise
            raise BridgeError("request_in_progress", "The simulation request directory exists but its status is not available yet.") from exc
        if current.get("request") != spec or simulation_context_key(current.get("context", {})) != simulation_context_key(context):
            raise BridgeError("request_conflict", "This request ID belongs to a different standalone simulation.")
        return current

    status_path = job / "status.json"
    status: dict[str, Any] = {
        "job_id": job_id,
        "session_id": sid,
        "request": spec,
        "state": "starting",
        "submitted_at": time.time(),
        "context": context,
    }
    try:
        root = _resolve_ads_root()
        python = root / "tools/python/python.exe"
        if not python.is_file():
            raise BridgeError("ads_unavailable", f"Standalone ADS Python not found at {python}")
        status["ads_root"] = str(root)
        (job / "snapshot.ckt").write_text(netlist_text, encoding="utf-8", newline="")
        write_json(status_path, status)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        with (job / "worker.log").open("wb") as log:
            proc = subprocess.Popen(
                [str(python), "-B", str(Path(__file__).with_name("worker.py")), str(job)],
                cwd=job,
                env=ads_environment(root),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        write_json(job / "worker.pid", {"pid": proc.pid})
    except (BridgeError, OSError) as exc:
        status.update(state="failed", code=getattr(exc, "code", "worker_start_failed"), message=str(exc))
        write_json(status_path, status)
        raise

    if (job / "cancel.flag").is_file():
        return job_cancel(sid, job_id)

    if not wait:
        return {**status, "worker_pid": proc.pid}

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        job_cancel(sid, job_id)
        raise BridgeError("simulation_timeout", f"Standalone simulation {job_id} exceeded {timeout}s and was cancelled.") from exc
    result = job_get(sid, job_id)
    if save_to_data_dir and data_dir and result.get("state") == "succeeded" and result.get("dataset"):
        src_ds = Path(result["dataset"])
        if src_ds.is_file():
            dst_dir = Path(data_dir).resolve()
            dst_dir.mkdir(parents=True, exist_ok=True)
            out_name = dataset_name if dataset_name else src_ds.name
            if not out_name.lower().endswith(".ds"):
                out_name += ".ds"
            dst_ds = dst_dir / out_name
            shutil.copy2(src_ds, dst_ds)
            result["exported_dataset"] = str(dst_ds)
    return result


def simulate_batch(
    netlists: list[str | Path],
    *,
    max_workers: int = 2,
    timeout: float = 300.0,
    model_dirs: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Execute multiple standalone ADS netlist simulations with bounded concurrency."""
    if not isinstance(netlists, list) or not 1 <= len(netlists) <= 32:
        raise BridgeError("invalid_argument", "Provide between 1 and 32 netlists.")
    if type(max_workers) is not int or not 1 <= max_workers <= 8:
        raise BridgeError("invalid_argument", "max_workers must be an integer between 1 and 8.")

    def _one(item: str | Path) -> dict[str, Any]:
        try:
            return simulate_netlist(item, model_dirs=model_dirs, wait=True, timeout=timeout)
        except BridgeError as exc:
            return {"state": "failed", "code": exc.code, "message": str(exc), "source": str(item)}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        runs = list(pool.map(_one, netlists))
    succeeded = sum(1 for r in runs if r.get("state") == "succeeded")
    return {"total": len(runs), "succeeded": succeeded, "session_id": STANDALONE_SESSION, "jobs": runs}


def optimize_netlist(
    netlist_or_path: str | Path,
    parameters: list[dict[str, Any]],
    objectives: list[dict[str, Any]],
    *,
    method: str = "nelder_mead",
    max_evals: int = 30,
    tol: float = 1e-4,
    data_dir: str | Path | None = None,
    model_dirs: list[str | Path] | None = None,
    timeout_per_sim: float = 90.0,
) -> dict[str, Any]:
    """Automatically optimize ADS circuit parameters in the bridge layer using a warm persistent ADS worker pool."""
    import netlist_parser
    rf_optimizer = _import_rf_module("rf_optimizer")
    rf_workflow = _import_rf_module("rf_workflow")

    # Validate parameters and objectives BEFORE starting ADS worker processes
    try:
        rf_optimizer._validate_parameters(parameters)
        rf_optimizer._validate_objectives(objectives)
    except ValueError as exc:
        raise BridgeError("invalid_argument", str(exc)) from exc

    netlist_text, src_path = _load_netlist_input(netlist_or_path)
    root = _resolve_ads_root()
    extract_specs: list[tuple[str | None, str | None]] = []
    for obj in objectives:
        if isinstance(obj, dict):
            pair = (obj.get("block"), obj.get("axis"))
            if pair not in extract_specs:
                extract_specs.append(pair)
    if not extract_specs:
        extract_specs = [(None, None)]

    pool_size = 2 if method == "grid_refine" else 1
    try:
        with rf_workflow.PersistentSimPool(
            root,
            ads_environment(root),
            runtime_dir(),
            STANDALONE_SESSION,
            pool_size=pool_size,
            data_dir=data_dir,
            model_dirs=model_dirs,
            timeout_per_sim=timeout_per_sim,
        ) as pool:
            def _sim_one(patched_nl: str) -> dict[str, Any]:
                return pool.simulate_and_extract(patched_nl, extract_specs)

            def _sim_batch(nl_list: list[str]) -> dict[str, Any]:
                with ThreadPoolExecutor(max_workers=min(pool.pool_size, len(nl_list))) as ex:
                    jobs = list(ex.map(_sim_one, nl_list))
                return {"total": len(jobs), "succeeded": sum(1 for j in jobs if j.get("state") == "succeeded"), "jobs": jobs}

            result = rf_optimizer.optimize_netlist(
                netlist_text,
                parameters,
                objectives,
                simulate_fn=_sim_one,
                extract_block_fn=pool.get_extracted_block,
                batch_simulate_fn=_sim_batch,
                method=method,
                max_evals=max_evals,
                tol=tol,
            )
    except (ValueError, netlist_parser.NetlistError) as exc:
        raise BridgeError("invalid_argument", str(exc)) from exc

    best_nl = result.pop("best_netlist", None)
    if best_nl:
        opt_dir = runtime_dir() / "artifacts" / STANDALONE_SESSION / "optimizations"
        ensure_private_dir(opt_dir)
        best_path = opt_dir / f"opt_{uuid.uuid4().hex[:12]}.ckt"
        best_path.write_text(best_nl, encoding="utf-8")
        result["best_netlist_path"] = str(best_path)
        result["best_netlist_revision"] = hashlib.sha256(best_nl.encode("utf-8")).hexdigest()
        result["best_netlist_compact"] = netlist_parser.compress_netlist(best_nl)
    if src_path is not None:
        result["source_file"] = str(src_path)
    return result


def design_eval_step(
    netlist_or_handle: str | Path,
    *,
    updates: dict[str, str | float | int] | None = None,
    sweep: dict[str, Any] | None = None,
    specs: list[dict[str, Any]] | None = None,
    erc_mode: str | None = "tb",
    compress_block: str | None = None,
    compress_axis: str | None = None,
    signals: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    max_curve_points: int = 16,
    band_hz: list[float] | None = None,
    data_dir: str | Path | None = None,
    model_dirs: list[str | Path] | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Atomic single-turn macro: Upfront Spec Check -> Patch -> ERC -> 64-bit ADS Simulation -> Spec Scorecard."""
    import netlist_parser
    rf_optimizer = _import_rf_module("rf_optimizer")
    rf_workflow = _import_rf_module("rf_workflow")

    # Validate declarative specs in <1 ms BEFORE launching any ADS worker process
    if specs:
        try:
            rf_optimizer._validate_objectives(specs)
        except ValueError as exc:
            raise BridgeError("invalid_argument", str(exc)) from exc

    text, _ = _load_netlist_input(netlist_or_handle)
    root = _resolve_ads_root()
    extract_specs: list[tuple[str | None, str | None]] = []
    for sp in (specs or ()):
        if isinstance(sp, dict):
            pair = (sp.get("block"), sp.get("axis"))
            if pair not in extract_specs:
                extract_specs.append(pair)
    if compress_block is not None or not specs:
        pair = (compress_block, compress_axis)
        if pair not in extract_specs:
            extract_specs.append(pair)

    try:
        # Fast path: run simulation + in-process dataset extraction in 1 persistent worker when no sweep injection fails
        with rf_workflow.PersistentSimPool(
            root,
            ads_environment(root),
            runtime_dir(),
            STANDALONE_SESSION,
            pool_size=1,
            data_dir=data_dir,
            model_dirs=model_dirs,
            timeout_per_sim=timeout,
        ) as pool:
            def _sim_one(nl_text: str, **kw: Any) -> dict[str, Any]:
                sw = kw.get("sweep")
                if sw is not None:
                    if not isinstance(sw, dict) or not sw.get("var"):
                        raise BridgeError("invalid_argument", "sweep must be an object with 'var', 'start', 'stop', and 'step'/'lin'/'dec'.")
                    nl_text = netlist_parser.inject_sweep_plan(
                        nl_text,
                        var=str(sw["var"]),
                        start=sw["start"],
                        stop=sw["stop"],
                        step=sw.get("step"),
                        dec=sw.get("dec"),
                        lin=sw.get("lin"),
                        analysis=sw.get("analysis"),
                    )
                return pool.simulate_and_extract(nl_text, extract_specs)

            return rf_workflow.run_design_eval_step(
                text,
                runtime_dir(),
                ensure_private_dir,
                simulate_fn=_sim_one,
                extract_block_fn=pool.get_extracted_block,
                extract_sweep_fn=pool.get_extracted_sweep,
                updates=updates,
                sweep=sweep,
                specs=specs,
                erc_mode=erc_mode,
                compress_block=compress_block,
                compress_axis=compress_axis,
                signals=signals,
                filters=filters,
                max_curve_points=max_curve_points,
                band_hz=band_hz,
                data_dir=data_dir,
                model_dirs=model_dirs,
                timeout=timeout,
            )
    except (ValueError, netlist_parser.NetlistError) as exc:
        raise BridgeError("invalid_argument", str(exc)) from exc


def sensitivity_scan(
    netlist_or_handle: str | Path,
    parameters: list[str | dict[str, Any]],
    objectives: list[dict[str, Any]],
    *,
    step_pct: float = 5.0,
    max_workers: int = 4,
    data_dir: str | Path | None = None,
    model_dirs: list[str | Path] | None = None,
    timeout_per_sim: float = 90.0,
) -> dict[str, Any]:
    """Rank circuit parameters by local +/-step_pct perturbation sensitivity using a parallel warm worker pool."""
    import netlist_parser
    rf_optimizer = _import_rf_module("rf_optimizer")
    rf_workflow = _import_rf_module("rf_workflow")

    try:
        rf_optimizer._validate_objectives(objectives)
    except ValueError as exc:
        raise BridgeError("invalid_argument", str(exc)) from exc

    text, _ = _load_netlist_input(netlist_or_handle)
    root = _resolve_ads_root()
    extract_specs: list[tuple[str | None, str | None]] = []
    for obj in objectives:
        if isinstance(obj, dict):
            pair = (obj.get("block"), obj.get("axis"))
            if pair not in extract_specs:
                extract_specs.append(pair)
    if not extract_specs:
        extract_specs = [(None, None)]

    pool_size = max(1, min(int(max_workers), 4, 1 + 2 * max(1, len(parameters))))
    try:
        with rf_workflow.PersistentSimPool(
            root,
            ads_environment(root),
            runtime_dir(),
            STANDALONE_SESSION,
            pool_size=pool_size,
            data_dir=data_dir,
            model_dirs=model_dirs,
            timeout_per_sim=timeout_per_sim,
        ) as pool:
            def _sim_one(nl: str) -> dict[str, Any]:
                return pool.simulate_and_extract(nl, extract_specs)

            def _sim_batch(nl_list: list[str]) -> dict[str, Any]:
                with ThreadPoolExecutor(max_workers=min(pool.pool_size, len(nl_list))) as ex:
                    jobs = list(ex.map(_sim_one, nl_list))
                return {"total": len(jobs), "succeeded": sum(1 for j in jobs if j.get("state") == "succeeded"), "jobs": jobs}

            return rf_optimizer.sensitivity_scan(
                text,
                parameters,
                objectives,
                simulate_fn=_sim_one,
                extract_block_fn=pool.get_extracted_block,
                batch_simulate_fn=_sim_batch,
                step_pct=step_pct,
            )
    except (ValueError, netlist_parser.NetlistError) as exc:
        raise BridgeError("invalid_argument", str(exc)) from exc


def gui_launch(
    workspace_path: str | Path,
    *,
    design: str | None = None,
    dds_path: str | Path | None = None,
    writable: bool = False,
    wait_timeout: float = 45.0,
) -> dict[str, Any]:
    """Launch foreground Keysight ADS 2026 GUI with live bridge auto-started and optionally open design & .dds."""
    ws = Path(workspace_path)
    if not ws.is_absolute():
        raise BridgeError("invalid_workspace", "gui_launch workspace_path must be an absolute path.")
    resolved_ws = ws.resolve()
    if not resolved_ws.is_dir():
        raise BridgeError("invalid_workspace", f"Workspace directory not found: {resolved_ws}")

    root = _resolve_ads_root()
    ads_exe = root / "bin" / "ads.exe"
    if not ads_exe.is_file():
        raise BridgeError("ads_unavailable", f"ADS GUI executable not found at {ads_exe}")

    # Reuse an already-reachable live GUI session open on the exact same workspace if present
    target_key = norm_path_key(resolved_ws)
    active_session: dict[str, Any] | None = None
    for sess in list_sessions():
        if sess.get("reachable"):
            try:
                insp = call(sess["session_id"], "session.inspect", challenge="gui-launch-reuse", timeout=2.0)
                if insp.get("workspace_path") and norm_path_key(insp["workspace_path"]) == target_key:
                    active_session = sess
                    break
            except BridgeError:
                continue

    launched_new = False
    if active_session is None:
        boot_dir = runtime_dir() / "boot"
        ensure_private_dir(boot_dir)
        boot_ael = boot_dir / f"start_bridge_{uuid.uuid4().hex[:8]}.ael"
        bridge_root_str = str(Path(__file__).resolve().parent).replace("\\", "\\\\")
        if writable:
            ws_arg = f"r'{str(resolved_ws).replace(chr(92), chr(92) * 2)}'"
            start_call = f"ads_addon.start(writable_workspace={ws_arg})"
        else:
            start_call = "ads_addon.start()"
        py_stmt = (
            f"import sys; p=r'{bridge_root_str}'; "
            f"(p in sys.path) or sys.path.insert(0, p); import ads_addon; {start_call}"
        )
        boot_ael.write_text(f'decl py_code = "{py_stmt}";\npython_exec(py_code);\n', encoding="ascii")

        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        env = ads_environment(root)
        subprocess.Popen(
            [str(ads_exe), str(resolved_ws), "-m", str(boot_ael)],
            cwd=str(resolved_ws),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        launched_new = True
        deadline = time.monotonic() + float(wait_timeout)
        while time.monotonic() < deadline:
            for sess in list_sessions():
                if sess.get("reachable"):
                    try:
                        insp = call(sess["session_id"], "session.inspect", challenge="gui-launch-wait", timeout=2.0)
                        if insp.get("workspace_path") and norm_path_key(insp["workspace_path"]) == target_key:
                            active_session = sess
                            break
                    except BridgeError:
                        continue
            if active_session is not None:
                break
            time.sleep(1.0)
        if active_session is None:
            raise BridgeError("gui_launch_timeout", f"ADS GUI did not register a live bridge session within {wait_timeout}s.")

    sid = active_session["session_id"]
    insp = call(sid, "session.inspect", challenge="gui-launch-ready")
    out: dict[str, Any] = {
        "ok": True,
        "launched_new": launched_new,
        "session_id": sid,
        "pid": active_session["pid"],
        "bridge_version": active_session.get("bridge_version"),
        "workspace_id": insp.get("workspace_id"),
        "workspace_path": insp.get("workspace_path"),
    }
    if design and insp.get("workspace_id"):
        out["design_window"] = call(
            sid,
            "window.open_design",
            workspace_id=insp["workspace_id"],
            design=design,
            editable=writable,
        )
    if dds_path is not None:
        out["dds_window"] = call(sid, "window.open_dds", path=str(dds_path))
    return out


def dds_build(
    workspace_path: str | Path,
    dataset_name: str,
    output_dds: str | Path,
    pages: list[dict[str, Any]],
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Build a native multi-plot Keysight ADS Data Display (.dds) file via standalone ADS Python."""
    ws = Path(workspace_path)
    if not ws.is_absolute():
        raise BridgeError("invalid_workspace", "dds_build workspace_path must be an absolute path.")
    resolved_ws = ws.resolve()
    root = _resolve_ads_root()
    python = root / "tools/python/python.exe"
    if not python.is_file():
        raise BridgeError("ads_unavailable", f"Standalone ADS Python not found at {python}")

    scratch = runtime_dir() / "headless_ipc"
    ensure_private_dir(scratch)
    token = uuid.uuid4().hex
    req_path = scratch / f"{token}.dds.req.json"
    resp_path = scratch / f"{token}.dds.resp.json"
    write_json(req_path, {
        "workspace_path": str(resolved_ws),
        "dataset_name": dataset_name,
        "output_dds": str(output_dds),
        "pages": pages,
    })
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.run(
            [str(python), "-B", str(Path(__file__).with_name("worker.py")), "dds", str(req_path), str(resp_path)],
            cwd=scratch,
            env=ads_environment(root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            creationflags=flags,
        )
        if resp_path.is_file():
            payload = read_json(resp_path)
            if "error" in payload:
                raise BridgeError(payload["error"]["code"], payload["error"]["message"])
            return payload["result"]
        stderr_tail = (proc.stderr or proc.stdout or b"")[-3000:].decode("utf-8", errors="replace").strip()
        raise BridgeError("dds_build_failed", stderr_tail or f"DDS worker exited with code {proc.returncode}")
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("dds_timeout", f"DDS builder timed out after {timeout}s.") from exc
    finally:
        req_path.unlink(missing_ok=True)
        resp_path.unlink(missing_ok=True)


def _verify_headless_workspace_safe(ws_path: Path, write: bool) -> None:
    if not ws_path.is_absolute():
        raise BridgeError("invalid_workspace", "Use an absolute workspace path for headless automation.")
    if write:
        # Never write into an ADS workspace currently open in a reachable GUI session
        target_key = norm_path_key(ws_path)
        for sess in list_sessions():
            if sess.get("reachable"):
                try:
                    insp = call(sess["session_id"], "session.inspect", challenge="lock-check", timeout=2.0)
                    if insp.get("workspace_path") and norm_path_key(insp["workspace_path"]) == target_key:
                        raise BridgeError(
                            "workspace_locked_by_gui",
                            f"Workspace '{ws_path}' is currently open in live ADS GUI session {sess['session_id']}. "
                            "Use GUI session tools instead of headless write.",
                        )
                except BridgeError as exc:
                    if exc.code == "workspace_locked_by_gui":
                        raise


def headless_call(
    workspace_path: str | Path,
    operation: str,
    *,
    write: bool = False,
    create_workspace: bool = False,
    timeout: float = 120.0,
    **arguments: Any,
) -> dict[str, Any]:
    """Execute a schematic, component, netlist, or layout operation via standalone ADS Python."""
    ws = Path(workspace_path)
    if not ws.is_absolute():
        raise BridgeError("invalid_workspace", "Headless workspace_path must be an absolute path.")
    resolved_ws = ws.resolve()
    _verify_headless_workspace_safe(resolved_ws, write=write)

    root = _resolve_ads_root()
    python = root / "tools/python/python.exe"
    if not python.is_file():
        raise BridgeError("ads_unavailable", f"Standalone ADS Python not found at {python}")

    artifact_dir = runtime_dir() / "artifacts" / STANDALONE_SESSION
    spec = {
        "workspace_path": str(resolved_ws),
        "write": bool(write),
        "create_workspace": bool(create_workspace),
        "artifact_dir": str(artifact_dir),
        "op": operation,
        "args": arguments,
    }
    return _invoke_headless_worker(root, python, spec, timeout=timeout)


def headless_workflow(
    workspace_path: str | Path | None,
    steps: list[dict[str, Any]],
    *,
    write: bool = False,
    create_workspace: bool = False,
    continue_on_error: bool = False,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Run multiple workspace/schematic/layout steps in a single standalone ADS Python session.

    Pass `workspace_path=None` (with `write=False`) for read-only built-in library queries such as `component.describe`.
    """
    if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
        raise BridgeError("invalid_argument", "steps must be a list of 1 through 50 {op, args} objects.")
    if workspace_path is None:
        if write:
            raise BridgeError("invalid_workspace", "Headless write workflows require an explicit absolute workspace_path.")
        resolved_ws = (runtime_dir() / "readonly_builtin_wrk").resolve()
        if not (resolved_ws / "lib.defs").is_file():
            create_workspace = True
            # Temporarily authorize creating the internal read-only scratch workspace only
            ws_create_flag = True
        else:
            ws_create_flag = False
    else:
        ws = Path(workspace_path)
        if not ws.is_absolute():
            raise BridgeError("invalid_workspace", "Headless workspace_path must be an absolute path.")
        resolved_ws = ws.resolve()
        _verify_headless_workspace_safe(resolved_ws, write=write)
        ws_create_flag = bool(create_workspace)

    root = _resolve_ads_root()
    python = root / "tools/python/python.exe"
    if not python.is_file():
        raise BridgeError("ads_unavailable", f"Standalone ADS Python not found at {python}")

    artifact_dir = runtime_dir() / "artifacts" / STANDALONE_SESSION
    spec = {
        "workspace_path": str(resolved_ws),
        "write": bool(write or (workspace_path is None and ws_create_flag)),
        "create_workspace": ws_create_flag,
        "continue_on_error": bool(continue_on_error),
        "artifact_dir": str(artifact_dir),
        "steps": steps,
    }
    return _invoke_headless_worker(root, python, spec, timeout=timeout)


def _invoke_headless_worker(root: Path, python: Path, spec: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    scratch = runtime_dir() / "headless_ipc"
    ensure_private_dir(scratch)
    token = uuid.uuid4().hex
    req_path = scratch / f"{token}.req.json"
    resp_path = scratch / f"{token}.resp.json"
    write_json(req_path, spec)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.run(
            [str(python), "-B", str(Path(__file__).with_name("worker.py")), "headless", str(req_path), str(resp_path)],
            cwd=scratch,
            env=ads_environment(root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            creationflags=flags,
        )
        if resp_path.is_file():
            payload = read_json(resp_path)
            if "error" in payload:
                raise BridgeError(payload["error"]["code"], payload["error"]["message"])
            return payload["result"]
        stderr_tail = (proc.stderr or proc.stdout or b"")[-3000:].decode("utf-8", errors="replace").strip()
        raise BridgeError("ads_headless_error", stderr_tail or f"Headless worker exited with code {proc.returncode}")
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("headless_timeout", f"Headless ADS worker timed out after {timeout}s.") from exc
    finally:
        req_path.unlink(missing_ok=True)
        resp_path.unlink(missing_ok=True)


def bridge_status() -> dict[str, Any]:
    import docs_pdk
    return docs_pdk.environment_status(runtime_dir(), list_sessions())


def docs_search(query: str, category: str = "all", limit: int = 10) -> dict[str, Any]:
    import docs_pdk
    try:
        return docs_pdk.search_docs(query, category=category, limit=limit)
    except ValueError as exc:
        raise BridgeError("invalid_argument", str(exc)) from exc


def pdk_list(include_builtin: bool = True) -> dict[str, Any]:
    import docs_pdk
    return docs_pdk.list_pdk(include_builtin=include_builtin)


def pdk_describe(library: str) -> dict[str, Any]:
    import docs_pdk
    try:
        return docs_pdk.describe_pdk(library)
    except (ValueError, KeyError) as exc:
        raise BridgeError("library_not_found", str(exc)) from exc


def pdk_search(query: str, library: str | None = None, limit: int = 25) -> dict[str, Any]:
    import docs_pdk
    try:
        return docs_pdk.search_pdk(query, library_name=library, limit=limit)
    except ValueError as exc:
        raise BridgeError("invalid_argument", str(exc)) from exc


LOCAL_OPERATIONS = {
    "job.get": job_get,
    "job.list": job_list,
    "job.log": job_log,
    "job.result": job_result,
    "job.cancel": job_cancel,
    "dataset.catalog": dataset_catalog,
    "dataset.read": dataset_read,
    "dataset.table": dataset_table,
    "dataset.analyze": dataset_analyze,
    "dataset.compress": dataset_compress,
    "request.get": request_get,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Inspect ADS installation, simulator, runtime, and sessions.")
    sub.add_parser("sessions", help="List registered ADS GUI sessions.")

    inspect_cmd = sub.add_parser("inspect", help="Echo a challenge from a live ADS GUI session.")
    inspect_cmd.add_argument("session_id")
    inspect_cmd.add_argument("--challenge", default=None)

    invoke = sub.add_parser("call", help="Call a live session or local job/dataset operation.")
    invoke.add_argument("session_id")
    invoke.add_argument("operation")
    source = invoke.add_mutually_exclusive_group()
    source.add_argument("--args", default="{}", help="JSON object containing tool arguments.")
    source.add_argument("--args-file", type=Path, help="Read tool arguments from a UTF-8 JSON file.")

    for command in ("job", "result", "cancel"):
        group = sub.add_parser(command)
        group.add_argument("session_id")
        group.add_argument("job_id")

    nl_inspect = sub.add_parser("netlist-inspect", help="Parse an ADS netlist file into structured AST & summary.")
    nl_inspect.add_argument("file", type=Path)

    nl_check = sub.add_parser("netlist-check", help="Run semantic ERC on an ADS netlist file.")
    nl_check.add_argument("file", type=Path)
    nl_check.add_argument("--mode", choices=("tb", "dut"), default="tb")

    nl_comp = sub.add_parser("netlist-compress", help="Compress an ADS netlist into compact circuit-parameter JSON.")
    nl_comp.add_argument("file", type=Path)
    nl_comp.add_argument("--format", choices=("json", "tabular"), default="json")

    nl_reg = sub.add_parser("netlist-register", help="Register an ADS netlist under a short nl_... handle.")
    nl_reg.add_argument("file", type=Path)
    nl_reg.add_argument("--format", choices=("tabular", "json"), default="tabular")

    eval_cmd = sub.add_parser("eval-step", help="Run atomic Patch -> ERC -> Simulation -> Spec Scorecard step.")
    eval_cmd.add_argument("target", help="Netlist file path or nl_... handle.")
    eval_cmd.add_argument("--spec-file", type=Path, required=True, help="JSON file with optional updates/specs/sweep.")

    sens_cmd = sub.add_parser("sensitivity", help="Run parallel local +/-step_pct parameter sensitivity scan.")
    sens_cmd.add_argument("target", help="Netlist file path or nl_... handle.")
    sens_cmd.add_argument("--spec-file", type=Path, required=True, help="JSON file with parameters, objectives, optional step_pct.")

    ds_comp = sub.add_parser("dataset-compress", help="Compress a completed simulation dataset via NumPy + scikit-rf.")
    ds_comp.add_argument("session_id")
    ds_comp.add_argument("job_id")
    ds_comp.add_argument("--block", default=None)
    ds_comp.add_argument("--axis", default=None)
    ds_comp.add_argument("--max-points", type=int, default=24)

    ts_comp = sub.add_parser("touchstone-compress", help="Compress a Touchstone (.s1p/.s2p/.sNp) file via scikit-rf.")
    ts_comp.add_argument("file", type=Path)
    ts_comp.add_argument("--max-points", type=int, default=24)

    opt_cmd = sub.add_parser("optimize", help="Run automatic closed-loop circuit parameter optimization.")
    opt_cmd.add_argument("file", type=Path)
    opt_cmd.add_argument("--spec-file", type=Path, required=True, help="JSON file with 'parameters', 'objectives', optional 'method'/'max_evals'.")

    sim_cmd = sub.add_parser("simulate", help="Simulate a standalone ADS netlist (.ckt) via 64-bit hpeesofsim.")
    sim_cmd.add_argument("file", type=Path)
    sim_cmd.add_argument("--timeout", type=float, default=180.0)

    hl_cmd = sub.add_parser("headless", help="Run an operation in standalone ADS Python without ADS GUI.")
    hl_cmd.add_argument("workspace_path", type=Path)
    hl_cmd.add_argument("operation")
    hl_cmd.add_argument("--write", action="store_true")
    hl_cmd.add_argument("--create-workspace", action="store_true")
    hl_src = hl_cmd.add_mutually_exclusive_group()
    hl_src.add_argument("--args", default="{}")
    hl_src.add_argument("--args-file", type=Path)

    docs_cmd = sub.add_parser("docs", help="Search installed Keysight ADS Python/API documentation.")
    docs_cmd.add_argument("query")
    docs_cmd.add_argument("--category", choices=("all", "example", "python_api", "diagnostics"), default="all")
    docs_cmd.add_argument("--limit", type=int, default=10)

    pdk_cmd = sub.add_parser("pdk", help="List, describe, or search ADS built-in and custom PDK libraries.")
    pdk_cmd.add_argument("action", choices=("list", "describe", "search"))
    pdk_cmd.add_argument("target", nargs="?", default="")
    pdk_cmd.add_argument("--library", default=None)
    pdk_cmd.add_argument("--limit", type=int, default=25)

    args = parser.parse_args()
    try:
        if args.command == "status":
            result: Any = bridge_status()
        elif args.command == "sessions":
            result = list_sessions()
        elif args.command == "inspect":
            result = call(args.session_id, "session.inspect", challenge=args.challenge or uuid.uuid4().hex)
        elif args.command == "call":
            arguments = read_json(args.args_file) if args.args_file else json.loads(args.args)
            if not isinstance(arguments, dict):
                raise BridgeError("invalid_argument", "--args must be a JSON object.")
            local = LOCAL_OPERATIONS.get(args.operation)
            result = local(args.session_id, **arguments) if local else call(args.session_id, args.operation, **arguments)
        elif args.command in {"job", "result", "cancel"}:
            fn = {"job": job_get, "result": job_result, "cancel": job_cancel}[args.command]
            result = fn(args.session_id, args.job_id)
        elif args.command == "netlist-inspect":
            result = netlist_inspect(args.file)
        elif args.command == "netlist-check":
            result = netlist_check(args.file, mode=args.mode)
        elif args.command == "netlist-compress":
            result = netlist_compress(args.file, format=args.format)
        elif args.command == "netlist-register":
            result = netlist_register(args.file, compact_format=args.format)
        elif args.command == "eval-step":
            ev_obj = read_json(args.spec_file)
            result = design_eval_step(
                args.target,
                updates=ev_obj.get("updates"),
                sweep=ev_obj.get("sweep"),
                specs=ev_obj.get("specs"),
                erc_mode=ev_obj.get("erc_mode", "tb"),
                compress_block=ev_obj.get("compress_block"),
                compress_axis=ev_obj.get("compress_axis"),
            )
        elif args.command == "sensitivity":
            s_obj = read_json(args.spec_file)
            result = sensitivity_scan(
                args.target,
                s_obj["parameters"],
                s_obj["objectives"],
                step_pct=float(s_obj.get("step_pct", 5.0)),
            )
        elif args.command == "dataset-compress":
            result = dataset_compress(args.session_id, args.job_id, block=args.block, axis=args.axis, max_curve_points=args.max_points)
        elif args.command == "touchstone-compress":
            result = touchstone_compress(args.file, max_curve_points=args.max_points)
        elif args.command == "optimize":
            spec_obj = read_json(args.spec_file)
            result = optimize_netlist(
                args.file,
                spec_obj["parameters"],
                spec_obj["objectives"],
                method=spec_obj.get("method", "nelder_mead"),
                max_evals=int(spec_obj.get("max_evals", 30)),
                tol=float(spec_obj.get("tol", 1e-4)),
            )
        elif args.command == "simulate":
            result = simulate_netlist(args.file, wait=True, timeout=args.timeout)
        elif args.command == "headless":
            hl_args = read_json(args.args_file) if args.args_file else json.loads(args.args)
            if not isinstance(hl_args, dict):
                raise BridgeError("invalid_argument", "--args must be a JSON object.")
            result = headless_call(
                args.workspace_path,
                args.operation,
                write=args.write,
                create_workspace=args.create_workspace,
                **hl_args,
            )
        elif args.command == "docs":
            result = docs_search(args.query, category=args.category, limit=args.limit)
        else:
            if args.action == "list":
                result = pdk_list()
            elif args.action == "describe":
                result = pdk_describe(args.target)
            else:
                result = pdk_search(args.target, library=args.library, limit=args.limit)
    except (BridgeError, ValueError, TypeError, OSError) as exc:
        print(json.dumps({"error": {"code": getattr(exc, "code", "invalid_argument"), "message": str(exc)}}))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.command == "simulate":
        return int(result["state"] != "succeeded")
    if isinstance(result, dict) and result.get("ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
