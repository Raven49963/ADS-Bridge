"""ADS Python GUI bridge entry point. Run start() on the ADS GUI main thread."""

from __future__ import annotations

from concurrent.futures import Future, TimeoutError
import hashlib
import hmac
from itertools import islice
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import secrets
import socket
import socketserver
import subprocess
from threading import Thread
import time
from typing import Any
import uuid

from PySide6.QtCore import QCoreApplication, QThread, QTimer
from keysight.ads import de
from keysight.ads.de import db_uu

from bridge import (
    BridgeError,
    ParameterValue,
    PROTOCOL,
    VERSION,
    ads_environment,
    ensure_private_dir,
    frame,
    identifier,
    job_cancel,
    job_get,
    norm_path_key,
    read_frame,
    read_json,
    runtime_dir,
    simulation_context_key,
    write_json,
)
import design_ops
import netlist_parser


HERE = Path(__file__).resolve().parent
_active: AdsBridge | None = None


def master_name(instance: Any) -> str:
    return design_ops.master_lcv_str(instance)


def _workspace_fingerprint(ws_path: Path) -> tuple[str, int | None]:
    """Combine normalized realpath with workspace state/lock ctime to detect close+reopen of same path."""
    norm = norm_path_key(ws_path)
    stamp: int | None = None
    for marker in ("saved.wrkstate", "lib.defs"):
        candidate = ws_path / marker
        try:
            if candidate.exists():
                st = candidate.stat()
                stamp = max(stamp or 0, st.st_mtime_ns)
        except OSError:
            pass
    return (norm, stamp)


def monitor_worker(worker: subprocess.Popen[bytes], status_path: Path) -> None:
    exit_code = worker.wait()
    if status_path.with_name("cancel.flag").is_file():
        return
    try:
        status = read_json(status_path)
    except OSError:
        return
    if status.get("state") in {"starting", "running"}:
        status.update(
            state="failed",
            code="worker_exited",
            exit_code=exit_code,
            log=str(status_path.with_name("worker.log")),
            finished_at=time.time(),
        )
        write_json(status_path, status)


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(5)
        try:
            request = read_frame(self.rfile)
            token = request.get("token")
            if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.server.bridge.token.encode()):
                raise BridgeError("unauthorized", "Invalid session credentials.")
            result = self.server.bridge.invoke(request["op"], request["args"])
            output = frame({"result": result})
        except (BridgeError, KeyError, TypeError, ValueError, AttributeError) as exc:
            output = frame({"error": {"code": getattr(exc, "code", "invalid_argument"), "message": str(exc)}})
        except OSError:
            return
        try:
            self.wfile.write(output)
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class AdsBridge:
    def __init__(self, writable_workspace: str | Path | None, model_dirs: tuple[Path, ...], workspace_roots: tuple[Path, ...]):
        self.session_id = uuid.uuid4().hex
        self.token = secrets.token_urlsafe(32)
        self.ads_root = Path(os.environ["HPEESOF_DIR"])
        self.runtime = runtime_dir()
        self.workspace_path: Path | None = None
        self.workspace_key: str | None = None
        self._workspace_open_seen = False
        self.workspace_id: str | None = None
        self.writable_workspace = Path(writable_workspace).resolve() if writable_workspace else None
        self.writable_key = norm_path_key(self.writable_workspace) if self.writable_workspace else None
        self.model_dirs = model_dirs
        self.workspace_roots = workspace_roots
        self.workspace_root_keys = tuple(norm_path_key(p) for p in workspace_roots)
        self.ledger = self.runtime / "requests" / self.session_id
        self.worker: subprocess.Popen[bytes] | None = None
        self.active_job_id: str | None = None
        self.queue: Queue[tuple[Future[Any], Any, dict[str, Any]]] = Queue(maxsize=32)
        self.running = True
        self._busy = False
        self.operations = {
            "session.inspect": self.inspect,
            "workspace.open": self.open_workspace,
            "catalog.list": self.catalog,
            "component.describe": self.describe_component,
            "library.create": self.create_library,
            "design.list": self.list_designs,
            "design.read": self.read_design,
            "design.check": self.check_design,
            "design.build": self.build_schematic,
            "design.edit": self.edit_schematic,
            "design.update": self.update_schematic,
            "design.add_instance": self.add_instance,
            "design.add_wire": self.add_wire,
            "design.add_pin": self.add_pin,
            "design.set_parameter": self.set_parameter,
            "netlist.generate": self.generate_netlist,
            "layout.build": self.build_layout,
            "layout.read": self.read_layout,
            "layout.export_gds": self.export_gds,
            "session.capture": self.capture,
            "simulation.submit": self.submit,
            "simulation.cancel": self.cancel_simulation,
            "window.open_design": self.open_design_window,
            "window.open_dds": self.open_dds_window,
            "request.get": self.get_request,
            "request.reconcile": self.reconcile_request,
        }
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.bridge = self  # type: ignore[attr-defined]
        sessions_dir = self.runtime / "sessions"
        self.record = sessions_dir / f"{self.session_id}.json"
        try:
            ensure_private_dir(sessions_dir)
            write_json(self.record, {
                "session_id": self.session_id,
                "pid": os.getpid(),
                "protocol": PROTOCOL,
                "ads_version": self.ads_root.name,
                "bridge_version": VERSION,
                "port": self.server.server_address[1],
                "token": self.token,
            })
        except Exception:
            self.server.server_close()
            raise
        app = QCoreApplication.instance()
        self.timer = QTimer(app)
        self.timer.timeout.connect(self.drain)
        self.timer.start(40)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        app.aboutToQuit.connect(stop)

    def invoke(self, operation: str, args: dict[str, Any]) -> Any:
        if not self.running:
            raise BridgeError("session_offline", "ADS bridge is stopping.")
        if operation == "ping":
            return {"session_id": self.session_id, "bridge_version": VERSION}
        if not isinstance(operation, str) or operation not in self.operations or not isinstance(args, dict):
            raise BridgeError("unknown_operation", "Unknown ADS operation or invalid arguments.")
        future: Future[Any] = Future()
        try:
            self.queue.put_nowait((future, self.operations[operation], args))
        except Full as exc:
            raise BridgeError("ads_busy", "The ADS operation queue is full.") from exc
        try:
            return future.result(timeout=20)
        except TimeoutError as exc:
            if future.cancel():
                raise BridgeError("ads_busy", "ADS did not begin executing the request.") from exc
            raise BridgeError("outcome_unknown", "ADS began the request. Reconcile or look up its original request ID.") from exc

    def _sync_workspace_state(self) -> Any:
        """Track workspace open/close transitions on the GUI thread to invalidate stale IDs."""
        is_open = bool(de.workspace_is_open())
        if not is_open:
            self._workspace_open_seen = False
            self.workspace_path = None
            self.workspace_key = None
            self.workspace_id = None
            return None

        ws = de.active_workspace()
        if ws is None or not getattr(ws, "is_open", True):
            self._workspace_open_seen = False
            self.workspace_path = None
            self.workspace_key = None
            self.workspace_id = None
            return None

        resolved = Path(ws.path).resolve()
        key = norm_path_key(resolved)
        if not self._workspace_open_seen or key != self.workspace_key:
            self._workspace_open_seen = True
            self.workspace_path = resolved
            self.workspace_key = key
            self.workspace_id = uuid.uuid4().hex
        return ws

    def drain(self) -> None:
        if self._busy:
            return
        # Always observe workspace open/close transitions at 40ms cadence even when idle!
        try:
            self._sync_workspace_state()
        except Exception:
            pass
        try:
            future, operation, args = self.queue.get_nowait()
        except Empty:
            return
        if not future.set_running_or_notify_cancel():
            return
        self._busy = True
        self.timer.stop()
        try:
            future.set_result(operation(**args))
        except BridgeError as exc:
            future.set_exception(exc)
        except TypeError as exc:
            future.set_exception(BridgeError("invalid_argument", str(exc)))
        except Exception as exc:
            future.set_exception(BridgeError("ads_error", f"{type(exc).__name__}: {exc}"))
        finally:
            self._busy = False
            if self.running:
                self.timer.start(40)

    def workspace(self, expected: str | None = None) -> Any:
        ws = self._sync_workspace_state()
        if expected is not None and expected != self.workspace_id:
            raise BridgeError("workspace_changed", "Inspect the current ADS workspace and select the target again.")
        return ws

    def inspect(self, challenge: str = "") -> dict[str, Any]:
        if not isinstance(challenge, str) or len(challenge) > 128:
            raise BridgeError("invalid_argument", "Challenge must be a string up to 128 characters.")
        self.workspace()
        return {
            "session_id": self.session_id,
            "pid": os.getpid(),
            "ads_version": self.ads_root.name,
            "bridge_version": VERSION,
            "operations": sorted(self.operations),
            "challenge": challenge,
            "in_ads_gui": de.is_pde_app(),
            "on_gui_thread": QThread.currentThread() == QCoreApplication.instance().thread(),
            "workspace_id": self.workspace_id,
            "workspace_path": str(self.workspace_path) if self.workspace_path else None,
            "write_enabled": self.workspace_key is not None and self.workspace_key == self.writable_key,
            "workspace_open_enabled": bool(self.workspace_roots),
        }

    def open_workspace(self, path: str) -> dict[str, Any]:
        if not isinstance(path, str) or not path:
            raise BridgeError("invalid_workspace", "Use the absolute path of an existing ADS workspace.")
        supplied = Path(path)
        if not supplied.is_absolute():
            raise BridgeError("invalid_workspace", "Use an absolute ADS workspace path.")
        target = supplied.resolve()
        if not any(target.is_relative_to(root) for root in self.workspace_roots):
            raise BridgeError("workspace_not_allowed", "This workspace is outside the locally configured roots.")
        active = self.workspace()
        if active is not None:
            if norm_path_key(target) == self.workspace_key:
                return self.inspect()
            raise BridgeError("workspace_already_open", "Close the current workspace in ADS before opening another.")
        if not target.is_dir() or not (target / "lib.defs").is_file():
            raise BridgeError("invalid_workspace", "An existing ADS workspace with a lib.defs file is required.")
        de.open_workspace(target)
        return self.inspect()

    def require_workspace(self, workspace_id: str) -> Any:
        workspace = self.workspace(identifier(workspace_id))
        if workspace is None:
            raise BridgeError("no_workspace", "Open an ADS workspace.")
        return workspace

    def require_write(self, workspace: Any, library: str | None = None) -> None:
        # Re-verify normalized active workspace path on every write transaction
        live_key = norm_path_key(workspace.path)
        if self.writable_key is None or live_key != self.writable_key or self.workspace_key != self.writable_key:
            raise BridgeError("write_disabled", "This workspace was not explicitly enabled for editing in ADS.")
        if library is not None and library not in workspace.writable_library_names:
            raise BridgeError("write_disabled", f"Library {library} is not writable.")

    @staticmethod
    def revision(netlist: str) -> str:
        return hashlib.sha256(netlist.encode("utf-8")).hexdigest()

    def list_designs(self, workspace_id: str) -> dict[str, Any]:
        workspace = self.require_workspace(workspace_id)
        views = []
        for name in workspace.writable_library_names:
            for cell in workspace.open_library(name).cells:
                for view in cell.views:
                    views.append(str(view.lcv_name))
                    if len(views) == 500:
                        return {"views": sorted(views), "limit_reached": True}
        return {"views": sorted(views), "limit_reached": False}

    def open_design(self, workspace_id: str, design: str, write: bool = False) -> Any:
        workspace = self.require_workspace(workspace_id)
        library, cell, view = design_ops.split_lcv(design, {"schematic"})
        if write:
            self.require_write(workspace, library)
        if design_ops.get_view(workspace, library, cell, view) is None:
            raise BridgeError("design_not_found", f"Design {design} does not exist in the active workspace.")
        return db_uu.open_design(design, db_uu.DesignMode.APPEND if write else db_uu.DesignMode.READ_ONLY)

    def describe_component(
        self,
        workspace_id: str,
        master: str | None = None,
        *,
        library: str | None = None,
        cell: str | None = None,
        compact: bool = False,
    ) -> dict[str, Any]:
        workspace = self.require_workspace(workspace_id)
        resolved = master or (f"{library}:{cell}" if library and cell else None)
        if not resolved:
            raise BridgeError("invalid_argument", "Provide 'master' or both 'library' and 'cell'.")
        return design_ops.describe_component(workspace, resolved, compact=bool(compact))

    def read_design(self, workspace_id: str, design: str) -> dict[str, Any]:
        target = self.open_design(workspace_id, design)
        payload = design_ops.read_schematic_details(target)
        payload["active_workspace_path"] = str(self.workspace_path)
        return payload

    def check_design(self, workspace_id: str, design: str, mode: str = "tb") -> dict[str, Any]:
        target = self.open_design(workspace_id, design)
        search_dirs = [str(self.workspace_path), *[str(p) for p in self.model_dirs]] if self.workspace_path else None
        return design_ops.check_schematic(target, mode=mode, search_dirs=search_dirs)

    def get_request(self, request_id: str) -> dict[str, Any]:
        from bridge import request_get
        return request_get(self.session_id, request_id)

    def reconcile_request(self, request_id: str) -> dict[str, Any]:
        """Reconcile an interrupted or outcome_unknown write request against live OpenAccess state."""
        path = self.ledger / f"{identifier(request_id)}.json"
        if not path.exists():
            raise BridgeError("request_not_found", "No persisted request has this ID.")
        record = read_json(path)
        result = record["result"]
        if result["state"] not in {"applying", "creating", "committed", "outcome_unknown"}:
            return result

        op = record.get("operation")
        meta = record.get("target_meta") or {}
        ws = self.workspace()
        reconciled_state = "outcome_unknown"
        evidence = "Workspace is closed or changed; open original workspace to reconcile."

        if ws is not None and meta.get("workspace_key") == self.workspace_key:
            if op == "library.create" and meta.get("library"):
                exists = meta["library"] in ws.library_names
                reconciled_state = "saved" if exists else "not_applied"
                evidence = f"Library '{meta['library']}' exists={exists} in active workspace."
            elif op in {"schematic.build", "layout.build"} and meta.get("design"):
                lib, cell, view = design_ops.split_lcv(meta["design"], {"schematic", "layout"})
                exists = design_ops.get_view(ws, lib, cell, view) is not None
                reconciled_state = "saved" if exists else "not_applied"
                evidence = f"View '{meta['design']}' exists={exists} in active workspace."
            elif op in {"design.set_parameter", "design.edit", "design.update"} and meta.get("design"):
                lib, cell, view = design_ops.split_lcv(meta["design"], {"schematic"})
                if design_ops.get_view(ws, lib, cell, view) is not None:
                    d = db_uu.open_design(meta["design"], db_uu.DesignMode.READ_ONLY)
                    live_rev = self.revision(d.generate_netlist())
                    if live_rev == meta.get("pre_revision"):
                        reconciled_state = "not_applied"
                        evidence = "Live netlist revision still matches pre-edit revision."
                    else:
                        reconciled_state = "saved"
                        result["revision"] = live_rev
                        evidence = "Live netlist revision advanced beyond pre-edit revision."

        result["state"] = reconciled_state
        result["reconciliation"] = evidence
        record["result"] = result
        write_json(path, record)
        return result

    def mutate(
        self,
        operation: str,
        request_id: str,
        arguments: Any,
        action: Any,
        *,
        target_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = self.ledger / f"{identifier(request_id)}.json"
        digest = self.revision(json.dumps([operation, arguments], sort_keys=True, allow_nan=False))
        self.ledger.mkdir(parents=True, exist_ok=True)
        if path.exists():
            record = read_json(path)
            if record["digest"] != digest:
                raise BridgeError("request_conflict", "This request ID belongs to a different operation or arguments.")
            result = record["result"]
            if "error" in result:
                raise BridgeError(result["error"]["code"], result["error"]["message"])
            return result
        result: dict[str, Any] = {"request_id": request_id, "state": "applying"}

        def record_state(state: str) -> None:
            result["state"] = state
            write_json(path, {
                "digest": digest,
                "operation": operation,
                "target_meta": target_meta or {},
                "result": result,
            })

        record_state("applying")
        try:
            payload = action(record_state)
        except Exception as exc:
            code = getattr(exc, "code", "ads_error")
            result["error"] = {"code": code, "message": str(exc)}
            state = result["state"]
            record_state("outcome_unknown" if state in {"committed", "creating"} else "failed")
            raise BridgeError(code, str(exc)) from exc
        result.update(payload)
        result["active_workspace_path"] = str(self.workspace_path)
        record_state("saved")
        return result

    def checked_target(self, workspace_id: str, design: str, revision: str, *, write: bool = False) -> Any:
        target = self.open_design(workspace_id, design, write=write)
        if revision != self.revision(target.generate_netlist()):
            raise BridgeError("design_changed", "The netlist revision changed. Read the design again.")
        return target

    def set_parameter(
        self,
        workspace_id: str,
        design: str,
        revision: str,
        instance: str,
        parameter: str,
        magnitude: float,
        unit: str,
        request_id: str,
    ) -> dict[str, Any]:
        arguments = (workspace_id, design, revision, instance, parameter, magnitude, unit)
        units = {
            "R": {"Ohm", "kOhm", "MOhm"},
            "C": {"fF", "pF", "nF", "uF", "F"},
            "L": {"pH", "nH", "uH", "mH", "H"},
        }
        if parameter not in units or unit not in units[parameter]:
            raise BridgeError("invalid_parameter", "Supported typed units: R (Ohm/kOhm/MOhm), C (fF..F), L (pH..H).")
        if type(magnitude) not in (int, float) or not 0 < magnitude <= 1e18 or not math.isfinite(magnitude):
            raise BridgeError("invalid_parameter", "The magnitude must be positive and finite.")

        def edit(record_state: Any) -> dict[str, Any]:
            target = self.checked_target(workspace_id, design, revision, write=True)
            if instance not in target.instances:
                raise BridgeError("instance_not_found", f"Instance '{instance}' does not exist.")
            device = target.instances[instance]
            if master_name(device) != f"ads_rflib:{parameter}:symbol":
                raise BridgeError("invalid_parameter", f"Select a matching ads_rflib:{parameter}:symbol component.")
            with db_uu.Transaction(target, command="ADS Bridge parameter edit") as transaction:
                applied = design_ops.apply_instance_parameters(device, {parameter: f"{magnitude:.15g} {unit}"})[0]
                transaction.commit()
            record_state("committed")
            target.save_design()
            return {
                "old_value": applied["old_value"],
                "value": device.parameters[parameter].value,
                "revision": self.revision(target.generate_netlist()),
            }

        return self.mutate(
            "design.set_parameter",
            request_id,
            arguments,
            edit,
            target_meta={"workspace_key": self.workspace_key, "design": design, "pre_revision": revision},
        )

    def catalog(self, workspace_id: str, library: str | None = None, search: str = "", offset: int = 0, limit: int = 100) -> dict[str, Any]:
        return design_ops.catalog(self.require_workspace(workspace_id), library, search, offset, limit)

    def create_library(self, workspace_id: str, name: str, request_id: str, layout_technology: bool = False) -> dict[str, Any]:
        def create(record_state: Any) -> dict[str, Any]:
            workspace = self.require_workspace(workspace_id)
            self.require_write(workspace)
            return design_ops.create_library(workspace, name, layout_technology, record_state)

        return self.mutate(
            "library.create",
            request_id,
            [workspace_id, name, layout_technology],
            create,
            target_meta={"workspace_key": self.workspace_key, "library": name},
        )

    def build_view(
        self,
        workspace_id: str,
        design: str,
        request_id: str,
        manifest: Any,
        view: str,
        prepare: Any,
        populate: Any,
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        def build(record_state: Any) -> dict[str, Any]:
            workspace = self.require_workspace(workspace_id)
            library, cell, _ = design_ops.split_lcv(design, {view})
            design_ops.new_name(cell)
            self.require_write(workspace, library)
            existing = design_ops.get_view(workspace, library, cell, view)
            if existing is not None and not replace:
                raise BridgeError("design_exists", f"{design} already exists. Pass replace=True to overwrite.")
            plan = prepare(workspace)
            replace_schematic = existing is not None and view == "schematic"
            if existing is not None and not replace_schematic:
                workspace.open_library(library).get_cell_if_exists(cell).delete_view(view)
            record_state("creating")
            create = db_uu.create_layout if view == "layout" else db_uu.create_schematic
            target = db_uu.open_design(design, db_uu.DesignMode.APPEND) if replace_schematic else create(design)
            try:
                with db_uu.Transaction(target, command="ADS Bridge build view") as transaction:
                    if replace_schematic:
                        target.clear_design()
                    populate(target, plan)
                    transaction.commit()
            except Exception:
                if not replace_schematic:
                    target.close_design()
                    workspace.open_library(library).get_cell_if_exists(cell).delete_view(view)
                record_state("rolled_back")
                raise
            record_state("committed")
            target.save_design()
            out: dict[str, Any] = {"design": design}
            if view == "schematic":
                out["revision"] = self.revision(target.generate_netlist())
            return out

        return self.mutate(
            f"{view}.build",
            request_id,
            [workspace_id, design, manifest, replace],
            build,
            target_meta={"workspace_key": self.workspace_key, "design": design},
        )

    def build_schematic(
        self,
        workspace_id: str,
        design: str,
        instances: list[dict[str, Any]],
        wires: list[dict[str, Any]],
        request_id: str,
        pins: list[dict[str, Any]] | None = None,
        variables: dict[str, str] | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        manifest = [instances, wires, pins, variables]
        return self.build_view(
            workspace_id,
            design,
            request_id,
            manifest,
            "schematic",
            lambda workspace: design_ops.schematic_plan(workspace, instances, wires, pins=pins, variables=variables),
            design_ops.populate_schematic,
            replace=replace,
        )

    def edit_schematic(self, workspace_id: str, design: str, revision: str, changes: list[dict[str, Any]], request_id: str) -> dict[str, Any]:
        def edit(record_state: Any) -> dict[str, Any]:
            target = self.checked_target(workspace_id, design, revision, write=True)
            seen: set[tuple[str, str]] = set()
            grouped: list[tuple[Any, dict[str, ParameterValue]]] = []
            for change in design_ops.records(changes, 100, "changes"):
                inst_name = change["instance"]
                if inst_name not in target.instances:
                    raise BridgeError("instance_not_found", f"Instance '{inst_name}' does not exist.")
                inst_obj = target.instances[inst_name]
                params = design_ops.parameter_values(change["parameters"])
                for key in params:
                    if (inst_name, key) in seen:
                        raise BridgeError("invalid_manifest", f"Repeated parameter {inst_name}.{key}.")
                    if key not in inst_obj.parameters:
                        raise BridgeError("invalid_parameter", f"Unknown parameter {inst_name}.{key}.")
                    seen.add((inst_name, key))
                grouped.append((inst_obj, params))
            if not seen:
                raise BridgeError("invalid_manifest", "Supply at least one parameter change.")
            result = []
            with db_uu.Transaction(target, command="ADS Bridge edit parameters") as transaction:
                for inst_obj, params in grouped:
                    result.extend(design_ops.apply_instance_parameters(inst_obj, params))
                transaction.commit()
            record_state("committed")
            target.save_design()
            return {"changes": result, "revision": self.revision(target.generate_netlist())}

        return self.mutate(
            "design.edit",
            request_id,
            [workspace_id, design, revision, changes],
            edit,
            target_meta={"workspace_key": self.workspace_key, "design": design, "pre_revision": revision},
        )

    def update_schematic(
        self,
        workspace_id: str,
        design: str,
        revision: str,
        ops: list[dict[str, Any]],
        request_id: str,
    ) -> dict[str, Any]:
        def apply_ops(record_state: Any) -> dict[str, Any]:
            workspace = self.require_workspace(workspace_id)
            target = self.checked_target(workspace_id, design, revision, write=True)
            with db_uu.Transaction(target, command="ADS Bridge incremental update") as transaction:
                applied = design_ops.apply_incremental_ops(workspace, target, ops)
                transaction.commit()
            record_state("committed")
            target.save_design()
            return {"applied": applied, "revision": self.revision(target.generate_netlist())}

        return self.mutate(
            "design.update",
            request_id,
            [workspace_id, design, revision, ops],
            apply_ops,
            target_meta={"workspace_key": self.workspace_key, "design": design, "pre_revision": revision},
        )

    def add_instance(
        self,
        workspace_id: str,
        design: str,
        revision: str,
        name: str,
        master: str,
        at: list[float],
        request_id: str,
        angle: float = 0.0,
        parameters: dict[str, ParameterValue] | None = None,
    ) -> dict[str, Any]:
        op = {"op": "add_instance", "name": name, "master": master, "at": at, "angle": angle, "parameters": parameters or {}}
        return self.update_schematic(workspace_id, design, revision, [op], request_id)

    def add_wire(
        self,
        workspace_id: str,
        design: str,
        revision: str,
        request_id: str,
        from_endpoint: str | None = None,
        to_endpoint: str | None = None,
        points: list[list[float]] | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        op: dict[str, Any] = {"op": "add_wire", "label": label}
        if from_endpoint is not None or to_endpoint is not None:
            op["from"] = from_endpoint
            op["to"] = to_endpoint
        if points is not None:
            op["points"] = points
        return self.update_schematic(workspace_id, design, revision, [op], request_id)

    def add_pin(
        self,
        workspace_id: str,
        design: str,
        revision: str,
        name: str,
        request_id: str,
        direction: str = "inputOutput",
        at: list[float] | None = None,
        connect: str | None = None,
        angle: float = 0.0,
    ) -> dict[str, Any]:
        op: dict[str, Any] = {"op": "add_pin", "name": name, "direction": direction, "angle": angle}
        if at is not None:
            op["at"] = at
        if connect is not None:
            op["connect"] = connect
        return self.update_schematic(workspace_id, design, revision, [op], request_id)

    def generate_netlist(self, workspace_id: str, design: str, inspect: bool = True) -> dict[str, Any]:
        text = self.open_design(workspace_id, design).generate_netlist()
        out: dict[str, Any] = {"design": design, "revision": self.revision(text), "text": text}
        if inspect:
            try:
                search_dirs = [str(self.workspace_path), *[str(p) for p in self.model_dirs]] if self.workspace_path else None
                out["parsed"] = netlist_parser.inspect_netlist(text, search_dirs=search_dirs)
            except netlist_parser.NetlistError as exc:
                out["parse_diagnostic"] = str(exc)
        return out

    def build_layout(self, workspace_id: str, design: str, shapes: list[dict[str, Any]], request_id: str, replace: bool = False) -> dict[str, Any]:
        return self.build_view(
            workspace_id,
            design,
            request_id,
            shapes,
            "layout",
            lambda workspace: design_ops.layout_plan(shapes),
            design_ops.populate_layout,
            replace=replace,
        )

    def open_layout(self, workspace_id: str, design: str) -> Any:
        workspace = self.require_workspace(workspace_id)
        library, cell, view = design_ops.split_lcv(design, {"layout"})
        if design_ops.get_view(workspace, library, cell, view) is None:
            raise BridgeError("design_not_found", f"Layout {design} does not exist in the active workspace.")
        return db_uu.open_design(design)

    def read_layout(self, workspace_id: str, design: str, limit: int = 100) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise BridgeError("invalid_argument", "Layout limit must be from 1 through 1000.")
        target = self.open_layout(workspace_id, design)
        shapes = []
        for shape in islice(db_uu.ShapeIter(target), limit + 1):
            shapes.append({
                "type": type(shape).__name__,
                "layer": str(shape.layer_id),
                "bbox": design_ops.box_values(shape.bbox),
            })
        return {
            "design": design,
            "units": "ADS layout user units",
            "bbox": design_ops.box_values(target.bbox),
            "shapes": shapes[:limit],
            "limit_reached": len(shapes) > limit,
        }

    def artifact(self, suffix: str) -> Path:
        directory = self.runtime / "artifacts" / self.session_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / (uuid.uuid4().hex + suffix)

    def export_gds(self, workspace_id: str, design: str) -> dict[str, Any]:
        from keysight.ads import ael

        target = self.open_layout(workspace_id, design)
        path = self.artifact(".gds")
        ael.call.gds_export_design(None, target, str(path))
        if not path.is_file() or path.stat().st_size == 0:
            raise BridgeError("export_failed", "ADS did not produce a GDS file. Check the layout technology and GDS mapping.")
        return {"design": design, "artifact": str(path), "bytes": path.stat().st_size}

    def capture(self) -> dict[str, Any]:
        from keysight.ads.de import app

        window = app.window.main_pyside_widget()
        if window is None:
            raise BridgeError("window_unavailable", "ADS does not have a main window.")
        path = self.artifact(".png")
        image = window.grab()
        if image.isNull() or not image.save(str(path), "PNG"):
            raise BridgeError("capture_failed", "ADS could not capture its main window.")
        return {"artifact": str(path), "width": image.width(), "height": image.height()}

    def submit(
        self,
        workspace_id: str,
        design: str,
        revision: str,
        request_id: str,
        block: str | None = None,
        axis: str | None = None,
        variable: str | None = None,
    ) -> dict[str, Any]:
        job_id = identifier(request_id)
        spec = dict(
            workspace_id=workspace_id,
            design=design,
            revision=revision,
            block=block,
            axis=axis,
            variable=variable,
        )
        if any(not isinstance(value, str) or not value or len(value) > 256 for value in (workspace_id, design, revision)):
            raise BridgeError("invalid_argument", "Simulation fields must be nonempty strings up to 256 characters.")
        trace = (block, axis, variable)
        if any(value is not None for value in trace) and not all(isinstance(value, str) and 0 < len(value) <= 256 for value in trace):
            raise BridgeError("invalid_argument", "Provide block, axis, and variable together or omit all three.")
        data = self.workspace_path / "data" if self.workspace_path else None
        context = {
            "data_dir": str(data) if data and data.is_dir() else None,
            "model_dirs": [str(path) for path in self.model_dirs],
        }
        job = self.runtime / "jobs" / self.session_id / job_id
        if job.exists():
            try:
                current = job_get(self.session_id, job_id)
            except BridgeError as exc:
                if exc.code != "job_not_found":
                    raise
                raise BridgeError("request_in_progress", "The simulation request directory exists but its status is not available yet.") from exc
            if current.get("request") != spec or (
                "context" in current and simulation_context_key(current.get("context", {})) != simulation_context_key(context)
            ):
                raise BridgeError("request_conflict", "This request ID belongs to a different simulation.")
            return current
        if self.worker is not None and self.worker.poll() is None:
            raise BridgeError("simulator_busy", "This ADS bridge session already has a running simulation.")
        netlist = self.open_design(workspace_id, design).generate_netlist()
        if self.revision(netlist) != revision:
            raise BridgeError("design_changed", "The netlist revision changed. Read the design again.")
        job.mkdir(parents=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        status: dict[str, Any] = {
            "job_id": job_id,
            "session_id": self.session_id,
            "request": spec,
            "state": "starting",
            "submitted_at": time.time(),
            "ads_root": str(self.ads_root),
            "context": context,
        }
        status_path = job / "status.json"
        try:
            (job / "snapshot.ckt").write_text(netlist, encoding="utf-8", newline="")
            write_json(status_path, status)
            with (job / "worker.log").open("wb") as log:
                self.worker = subprocess.Popen(
                    [str(self.ads_root / "tools/python/python.exe"), "-B", str(HERE / "worker.py"), str(job)],
                    cwd=job,
                    env=ads_environment(self.ads_root),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
            self.active_job_id = job_id
            write_json(job / "worker.pid", {"pid": self.worker.pid})
            Thread(target=monitor_worker, args=(self.worker, status_path), daemon=True).start()
            if (job / "cancel.flag").is_file():
                return job_cancel(self.session_id, job_id)
            return {**status, "worker_pid": self.worker.pid}
        except OSError as exc:
            status.update(state="failed", code="worker_start_failed", message=str(exc))
            write_json(status_path, status)
            return status

    def cancel_simulation(self, job_id: str) -> dict[str, Any]:
        return job_cancel(self.session_id, job_id)

    def open_design_window(self, workspace_id: str, design: str, editable: bool = True) -> dict[str, Any]:
        from keysight.ads import ael
        from keysight.ads.de import app

        workspace = self.require_workspace(workspace_id)
        library, cell, view = design_ops.split_lcv(design, {"schematic", "layout", "symbol"})
        if design_ops.get_view(workspace, library, cell, view) is None:
            raise BridgeError("design_not_found", f"Design {design} does not exist in the active workspace.")
        can_write = (
            self.writable_key is not None
            and norm_path_key(workspace.path) == self.writable_key
            and self.workspace_key == self.writable_key
        )
        mode = db_uu.DesignMode.APPEND if (editable and can_write) else db_uu.DesignMode.READ_ONLY
        target = db_uu.open_design(design, mode)
        method = "open_design"
        for fn_name in ("de_bring_design_to_top_or_open_new_window", "de_show_context_in_new_window"):
            try:
                getattr(ael.call, fn_name)(target)
                method = fn_name
                break
            except Exception:
                continue
        try:
            ael.call.de_view_all()
        except Exception:
            pass
        win_type = app.WindowType.LAYOUT_WINDOW if view == "layout" else app.WindowType.SCHEMATIC_WINDOW
        titles = [w.title for w in app.find_windows_by_type(win_type)]
        return {"design": design, "mode": str(mode).split(".")[-1], "method": method, "windows": titles}

    def open_dds_window(self, path: str) -> dict[str, Any]:
        from keysight.ads import ael

        dds_path = Path(path)
        if not dds_path.is_absolute() and self.workspace_path is not None:
            dds_path = (self.workspace_path / dds_path).resolve()
        else:
            dds_path = dds_path.resolve()
        if not dds_path.is_file():
            raise BridgeError("file_not_found", f"Data Display file not found: {dds_path}")
        try:
            ael.call.de_open_datadisplay(str(dds_path))
            launcher = "ael:de_open_datadisplay"
        except Exception:
            dds_exe = self.ads_root / "bin" / "dds.exe"
            if not dds_exe.is_file():
                raise BridgeError("dds_unavailable", f"ADS Data Display executable not found at {dds_exe}")
            subprocess.Popen([str(dds_exe), str(dds_path)], cwd=str(dds_path.parent))
            launcher = "dds.exe"
        return {"dds_path": str(dds_path), "launcher": launcher}

    def stop(self) -> None:
        self.running = False
        self.record.unlink(missing_ok=True)
        try:
            self.timer.stop()
            self.timer.deleteLater()
            try:
                QCoreApplication.instance().aboutToQuit.disconnect(stop)
            except Exception:
                pass
        finally:
            self.server.shutdown()
            self.server.server_close()


def start(writable_workspace: str | Path | None = None, model_dirs: Any = (), workspace_roots: Any = ()) -> str:
    """Start this ADS session. Optionally enable writes to one disposable workspace."""
    global _active
    if not de.is_pde_app() or QThread.currentThread() != QCoreApplication.instance().thread():
        raise RuntimeError("Start the bridge in the ADS Python Console on the GUI thread.")
    if _active is not None:
        raise RuntimeError("The bridge is already running. Call stop() before reconfiguring.")
    directories = tuple(Path(path).resolve(strict=True) for path in model_dirs)
    if not all(path.is_dir() for path in directories):
        raise ValueError("Model search paths must be directories.")
    roots = tuple(Path(path).resolve(strict=True) for path in workspace_roots)
    if not all(path.is_dir() for path in roots):
        raise ValueError("Workspace roots must be directories.")
    _active = AdsBridge(writable_workspace, directories, roots)
    print(f"ADS Bridge session {_active.session_id}")
    return _active.session_id


def stop() -> None:
    global _active
    if _active is not None:
        _active.stop()
        _active = None
