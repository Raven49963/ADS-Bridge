"""Exercise project capabilities in an explicitly selected disposable ADS workspace."""

import argparse
import math
from pathlib import Path
import time
import uuid

import bridge


def verify(condition, message):
    if not condition:
        raise RuntimeError(message)


def request_id():
    return uuid.uuid4().hex


def gds_record_types(data):
    types = []
    pos = 0
    while pos + 4 <= len(data):
        size = int.from_bytes(data[pos:pos + 2], "big")
        verify(size >= 4 and size % 2 == 0 and pos + size <= len(data), "Malformed GDS record.")
        kind = data[pos + 2]
        types.append(kind)
        if kind == 4:
            return types
        pos += size
    raise RuntimeError("GDS ENDLIB record missing.")


def verify_live(sid, fixture):
    inspected = bridge.call(sid, "session.inspect", challenge="live-v1")
    verify(inspected["challenge"] == "live-v1" and inspected["on_gui_thread"] and inspected["in_ads_gui"],
           "The request must be served on the ADS GUI thread.")
    verify(inspected["write_enabled"] and Path(inspected["workspace_path"]).resolve() == fixture.resolve(),
           "Select the explicitly writable, disposable test workspace.")
    workspace_id = inspected["workspace_id"]
    library = "verification_" + request_id()[:8]
    examples = Path(__file__).with_name("examples")

    def call(operation, **arguments):
        return bridge.call(sid, operation, workspace_id=workspace_id, **arguments)

    def build(name, example):
        design = f"{library}:{name}:schematic"
        manifest = bridge.read_json(examples / example)
        rid = request_id()
        results = [call("design.build", design=design, request_id=rid, **manifest) for _ in range(2)]
        verify(results[0] == results[1] and results[0]["state"] == "saved", "Schematic retry changed the result.")
        return design

    def simulate(target, revision):
        rid = request_id()
        job = call("simulation.submit", design=target, revision=revision, request_id=rid)
        verify(job["job_id"] == rid, "Unexpected job ID.")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = bridge.job_get(sid, rid)
            if status["state"] not in {"starting", "running"}:
                verify(status["state"] == "succeeded", str(status))
                return rid
            time.sleep(0.25)
        raise TimeoutError(f"No result for job {rid}.")

    call("library.create", name=library, request_id=request_id(), layout_technology=True)
    rc = build("rc", "rc.json")
    original = call("design.read", design=rc)
    verify(len(original["instances"]) == 6 and "OUT" in original["nets"], "RC connectivity did not round-trip.")
    edits = [{"instance": "R1", "parameters": {"R": "6 kOhm"}}]
    edit = call("design.edit", design=rc, revision=original["revision"], changes=edits, request_id=request_id())
    try:
        call("design.edit", design=rc, revision=original["revision"], changes=edits, request_id=request_id())
    except bridge.BridgeError as exc:
        verify(exc.code == "design_changed", str(exc))
    else:
        raise RuntimeError("The old revision was accepted.")
    netlist = call("netlist.generate", design=rc)
    verify(netlist["revision"] == edit["revision"] and "6 kOhm" in netlist["text"], "Netlist did not contain the edit.")
    rc_job = simulate(rc, netlist["revision"])
    catalog = bridge.dataset_catalog(sid, rc_job)
    ac = next(block for block in catalog["blocks"] if block["name"] == "AC1.AC")
    verify("OUT" in ac["variables"] and "freq" in ac["axes"], "AC dataset schema did not match the fixture.")
    trace = {"block": "AC1.AC", "axis": "freq", "variable": "OUT"}
    rows = [*bridge.dataset_read(sid, rc_job, **trace, limit=16)["samples"],
            *bridge.dataset_read(sid, rc_job, **trace, offset=16, limit=16)["samples"]]
    error = max(abs(complex(real, imag) - 1 / (1 + 2j * math.pi * freq * 0.006)) for freq, real, imag in rows)
    verify(len(rows) == 31 and len({row[0] for row in rows}) == 31 and error < 1e-6,
           f"Invalid RC sweep, rows={len(rows)}, error={error}.")
    print(f"AC 31 points; max complex error {error:.3g} V; job {rc_job}", flush=True)

    sp = build("ports", "series_resistor_sp.json")
    sp_job = simulate(sp, call("netlist.generate", design=sp)["revision"])
    blocks = bridge.dataset_catalog(sid, sp_job)["blocks"]
    block = next(entry for entry in blocks if "S[2,1]" in entry["variables"])
    for variable, expected in {"S[2,1]": 2/3, "S[1,2]": 2/3, "S[1,1]": 1/3}.items():
        trace = bridge.dataset_read(sid, sp_job, block["name"], "freq", variable)["samples"]
        verify(len(trace) == 10 and max(abs(complex(r, i) - expected) for _, r, i in trace) < 1e-6,
               f"Incorrect S-parameter values for {variable}.")
    print(f"S-parameters 10 points; S21/S12 = 2/3 and S11 = 1/3; job {sp_job}", flush=True)

    layout = f"{library}:geometry:layout"
    call("layout.build", design=layout, request_id=request_id(), **bridge.read_json(examples / "layout.json"))
    geometry = call("layout.read", design=layout)
    bbox = geometry["bbox"]
    verify(len(geometry["shapes"]) == 2 and all(abs(bbox[key] - expected) < 1e-6 for key, expected in
           {"left": 0, "bottom": 0, "right": 30, "top": 5}.items()), f"Unexpected layout geometry: {geometry}")
    artifact = call("layout.export_gds", design=layout)
    contents = Path(artifact["artifact"]).read_bytes()
    records = gds_record_types(contents)
    verify(records[0] == 0 and records.count(8) == 2, "GDS must have a header and both polygon boundaries.")
    verify(len(call("catalog.list", library=library)["items"]) == 3, "Library catalog is incomplete.")
    verify(bridge.request_get(sid, edit["request_id"]) == edit, "Request ledger does not match the saved edit.")
    verify(len(bridge.job_list(sid)) >= 2 and "64-bit" in bridge.job_log(sid, sp_job)["tail"], "Job log is incomplete.")
    print(f"Layout 2 shapes, 30 × 5 ADS user units; library {library}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("fixture", type=Path)
    options = parser.parse_args()
    verify_live(options.session_id, options.fixture)
