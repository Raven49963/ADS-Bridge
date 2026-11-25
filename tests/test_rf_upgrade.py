"""Unit tests for ADS Bridge 2.1.0 upgrades:
- Netlist semantic compression, parameter patching, and SweepPlan injection
- RF/circuit simulation data compression (NumPy + scikit-rf S-parameters, Touchstone, AC, HB, Tran, LTTB)
- Bridge-layer automatic derivative-free circuit parameter optimizer
- Obstacle-aware orthogonal schematic routing
"""

from __future__ import annotations

import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge
import design_ops
import netlist_parser
import rf_compress
import rf_optimizer
import rf_workflow
import worker


VERBOSE_ADS_NETLIST = """\
; Top Design: "rf_lib:l_match_2400:schematic"
; Netlisted using Keysight ADS 2026 Update 0.1
Options ResourceUsage=yes UseNutmegFormat=no EnableOptim=no TopDesignName="rf_lib:l_match_2400:schematic" DcopOutputNodeVoltages=yes DcopOutputPinCurrents=yes DcopOutputAllSweepPoints=no DcopOutputDcopType=0
Component Module="LinearCollapse" Type="ModelExtractor"
X=1.0
L_val=4.7 nH
C_val=1.2 pF
Port:TermG1 N_in 0 Num=1 Z=50 Ohm Noise=yes P[1]=dbmtow(0) Pac=dbmtow(0) Freq[1]=2.4 GHz
L:L1 N_in N_mid L=L_val Temp=25 Trise=0 Mode=1
C:C1 N_mid 0 C=C_val Temp=25 Trise=0
R:Rload N_mid N_out R=10 Ohm Temp=25 Tnom=25 TC1=0 TC2=0 Noise=yes
Port:TermG2 N_out 0 Num=2 Z=50 Ohm Noise=yes
S_Param:SP1 CalcS=yes CalcY=no CalcZ=no GroupDelayAperture=1e-4 FreqConversion=no FreqConversionPort=1 StatusLevel=2 CalcNoise=no SortNoise=0 BandwidthForNoise=1.0 Hz DevOpPtLevel=0 SweepVar="freq" SweepPlan="SP1_stim" OutputPlan="SP1_Output"
SweepPlan: SP1_stim Start=1.0 GHz Stop=4.0 GHz Step=0.05 GHz
OutputPlan:SP1_Output Type="Output" UseEquationNestLevel=yes EquationNestLevel=2 UseSavedEquationNestLevel=yes SavedEquationNestLevel=2
"""


class NetlistCompressionAndPatchingTests(unittest.TestCase):
    def test_compress_netlist_strips_boilerplate_and_extracts_tunables(self) -> None:
        compact = netlist_parser.compress_netlist(VERBOSE_ADS_NETLIST)
        self.assertEqual(compact["schema"], "ads_bridge.netlist_compact.v1")
        self.assertEqual(compact["design"], "rf_lib:l_match_2400:schematic")
        # Unused stub X=1.0 should be dropped while active L_val/C_val are preserved
        self.assertNotIn("X", compact["vars"])
        self.assertIn("L_val", compact["vars"])
        self.assertIn("C_val", compact["vars"])
        # Options/OutputPlan/SweepPlan cards should be folded or omitted from components
        comp_types = {c["type"] for c in compact["components"]}
        self.assertNotIn("Options", comp_types)
        self.assertNotIn("OutputPlan", comp_types)
        self.assertEqual(len(compact["analyses"]), 1)
        self.assertIn("sweep_plan", compact["analyses"][0])
        # Boilerplate params like Temp/Trise/Noise should be stripped from L1/Rload
        rload = next(c for c in compact["components"] if c["id"] == "Rload")
        self.assertNotIn("Temp", rload.get("params", {}))
        self.assertNotIn("Noise", rload.get("params", {}))
        # Tunable refs should include VAR:L_val, VAR:C_val, and Rload.R
        refs = {p["ref"] for p in compact["tunable_params"]}
        self.assertIn("VAR:L_val", refs)
        self.assertIn("VAR:C_val", refs)
        self.assertIn("Rload.R", refs)
        self.assertGreater(compact["stats"]["vs_inspect_ratio"], 2.0)
        self.assertGreater(compact["stats"]["raw_chars"], 0)
        # On a realistic multi-block ADS netlist with verbose solver cards, compression_ratio > 1.0 vs raw text too
        verbose_hb = (
            VERBOSE_ADS_NETLIST
            + 'HarmonicBalance:HB1 Freq[1]=13.56 MHz Order[1]=15 StatusLevel=2 MaxIter=100 '
              'UseKrylov=no KrylovRestartLength=20 KrylovDim=50 KrylovTol=1e-4 '
              'AutoSelectSolver=yes Oversample=4 FundOversample=4 SamplFreq=0 '
              'SteadyState=yes OutputPlan="HB1_Output" UseSweepPlan=no '
              'CalcNoise=no SortNoise=0 BandwidthForNoise=1.0 Hz DevOpPtLevel=0 '
              'Restart=no SaveSolns=yes UseAllSS=yes MatrixSolver=0\n'
            + 'Tran:HB1_tran HB_Sol=1 StopTime=1 us MaxTimeStep=1 ns StatusLevel=2\n'
            + 'OutputPlan:HB1_Output Type="Output" UseEquationNestLevel=yes EquationNestLevel=2 UseSavedEquationNestLevel=yes SavedEquationNestLevel=2\n'
        )
        compact_hb = netlist_parser.compress_netlist(verbose_hb)
        self.assertGreater(compact_hb["stats"]["compression_ratio"], 1.0)
        self.assertGreater(compact_hb["stats"]["vs_inspect_ratio"], 3.0)

    def test_apply_netlist_parameters_var_and_instance(self) -> None:
        patched = netlist_parser.apply_netlist_parameters(
            VERBOSE_ADS_NETLIST,
            {"VAR:L_val": "6.8 nH", "Rload.R": "25 Ohm"},
        )
        parsed = netlist_parser.inspect_netlist(patched)
        self.assertAlmostEqual(parsed["variables"]["L_val"]["si_value"], 6.8e-9, places=15)
        rload = next(i for i in parsed["instances"] if i["name"] == "Rload")
        self.assertAlmostEqual(rload["parsed_parameters"]["R"]["si_value"], 25.0, places=9)

        # Var: component line form should also patch cleanly
        var_card_nl = "Var:VAR1 R_val=3 kOhm\nR:R1 IN OUT R=R_val\n"
        patched_card = netlist_parser.apply_netlist_parameters(var_card_nl, {"VAR:R_val": "1.5 kOhm"})
        self.assertIn("R_val=1.5 kOhm", patched_card)

        with self.assertRaises(netlist_parser.NetlistError):
            netlist_parser.apply_netlist_parameters(VERBOSE_ADS_NETLIST, {"VAR:DOES_NOT_EXIST": "1"})

    def test_inject_sweep_plan(self) -> None:
        base = (
            "R_val=3 kOhm\n"
            "V_Source:SRC1 IN 0 Type=\"V_AC\" Vac=polar(1,0)\n"
            "R:R1 IN OUT R=R_val\n"
            "C:C1 OUT 0 C=1 uF\n"
            "AC:AC1 Start=1 Hz Stop=1000 Hz Dec=10\n"
        )
        swept = netlist_parser.inject_sweep_plan(base, var="R_val", start="1 kOhm", stop="5 kOhm", lin=5)
        self.assertIn('SweepVar="R_val"', swept)
        self.assertIn('SweepPlan="SW_BRIDGE"', swept)
        self.assertIn("SweepPlan: SW_BRIDGE Start=1 kOhm Stop=5 kOhm Lin=5", swept)
        parsed = netlist_parser.inspect_netlist(swept)
        self.assertTrue(any(i["model_type"] == "SweepPlan" and i["name"] == "SW_BRIDGE" for i in parsed["instances"]))


class RFCompressionTests(unittest.TestCase):
    def test_lttb_preserves_endpoints_and_sharp_notch(self) -> None:
        xs = np.linspace(1e9, 3e9, 1001)
        ys = -1.0 - 35.0 * np.exp(-((xs - 2.4e9) / 1.5e7) ** 2)
        ys[777] = 2.5  # sharp spike
        idx = rf_compress.lttb_downsample_indices(xs, ys, max_points=20)
        self.assertEqual(idx[0], 0)
        self.assertEqual(idx[-1], len(xs) - 1)
        self.assertIn(int(np.argmin(ys)), idx)
        self.assertIn(int(np.argmax(ys)), idx)
        self.assertLessEqual(len(idx), 22)

    def test_compress_sparam_and_touchstone_roundtrip(self) -> None:
        # 2-port symmetric series R=20 Ohm + shunt resonator (passive, reciprocal, matched around 2.4 GHz)
        freqs = np.linspace(1.0e9, 4.0e9, 601)
        z0 = 50.0
        w = 2.0 * math.pi * freqs
        l_sh = 2.2e-9
        c_sh = 1.0 / ((2.0 * math.pi * 2.4e9) ** 2 * l_sh)
        y_sh = 1.0 / (1j * w * l_sh) + 1j * w * c_sh  # zero at 2.4 GHz (open circuit -> perfect pass-through)
        # Shunt admittance ABCD -> S-matrix for lossless 2-port resonator
        denom = 2.0 + y_sh * z0
        s11 = -(y_sh * z0) / denom
        s21 = 2.0 / denom
        s_mat = np.zeros((len(freqs), 2, 2), dtype=np.complex128)
        s_mat[:, 0, 0] = s11
        s_mat[:, 1, 1] = s11
        s_mat[:, 1, 0] = s21
        s_mat[:, 0, 1] = s21

        with tempfile.TemporaryDirectory() as tmp:
            ts_target = Path(tmp) / "bpf_2400.s2p"
            comp = rf_compress.compress_sparam_network(
                freqs,
                s_mat,
                z0_ohm=50.0,
                max_curve_points=20,
                band_hz=(2.0e9, 2.8e9),
                return_loss_spec_db=-10.0,
                touchstone_path=ts_target,
                name="bpf_2400",
            )
            self.assertEqual(comp["kind"], "sparameter")
            self.assertIsNotNone(comp["engine"]["scikit_rf"])
            self.assertTrue(comp["network_properties"]["is_passive"])
            self.assertTrue(comp["network_properties"]["is_reciprocal"])
            self.assertAlmostEqual(comp["parameters"]["S[1,1]"]["best_match_freq_hz"], 2.4e9, delta=1e7)
            self.assertGreater(comp["parameters"]["S[1,1]"]["matched_bw_hz"], 1e8)
            self.assertIn("z_at_best_match_ohm", comp["parameters"]["S[1,1]"])
            self.assertGreater(comp["compression"]["compression_ratio"], 5.0)
            self.assertTrue(Path(comp["touchstone_file"]).is_file())

            # Round-trip Touchstone file compression
            ts_comp = rf_compress.compress_touchstone_file(comp["touchstone_file"], max_curve_points=16)
            self.assertEqual(ts_comp["network_properties"]["n_ports"], 2)
            self.assertAlmostEqual(ts_comp["parameters"]["S[1,1]"]["best_match_freq_hz"], 2.4e9, delta=1e7)

    def test_compress_ac_hb_and_transient_blocks(self) -> None:
        # 1. AC RC lowpass with fc = 1000 Hz
        freqs = np.logspace(1, 5, 401)
        rc = 1.0 / (2.0 * math.pi * 1000.0)
        vout = 1.0 / (1.0 + 1j * 2.0 * math.pi * freqs * rc)
        ac_comp = rf_compress.compress_waveform_block("freq", freqs, {"VOUT": vout}, block_name="AC1.AC", max_curve_points=16)
        self.assertEqual(ac_comp["kind"], "ac")
        self.assertAlmostEqual(ac_comp["signals"]["VOUT"]["crossings_3db_hz"][0], 1000.0, delta=10.0)
        self.assertGreater(ac_comp["compression"]["compression_ratio"], 5.0)

        # 2. HB spectrum with PA efficiency metrics (f0=13.56 MHz, Vout1=20 V peak -> 4 W into 50 Ohm, Idc=0.22 A @ 20 V -> 90.9% DE)
        hb_freqs = np.arange(0, 6) * 13.56e6
        v_hb = np.array([0.0, 20.0 + 0j, 0.2 + 0j, 0.1 + 0j, 0.05 + 0j, 0.02 + 0j])
        i_hb = np.array([0.22 + 0j, 0.1 + 0j, 0.05 + 0j, 0.02 + 0j, 0.01 + 0j, 0.005 + 0j])
        hb_comp = rf_compress.compress_waveform_block(
            "freq",
            hb_freqs,
            {"Vout": v_hb, "IDS.i": i_hb},
            block_name="HB1.HB",
            load_ohm=50.0,
            vds_v=20.0,
            ids_signal="IDS.i",
            vout_signal="Vout",
            pin_dbm=15.0,
        )
        self.assertEqual(hb_comp["kind"], "harmonic_balance")
        self.assertAlmostEqual(hb_comp["signals"]["Vout"]["power_w"], 4.0, places=4)
        self.assertAlmostEqual(hb_comp["signals"]["Vout"]["h2_dbc"], -40.0, places=3)
        self.assertAlmostEqual(hb_comp["pa_metrics"]["drain_eff_percent"], 4.0 / 4.4 * 100.0, places=2)

        # 3. Transient sinusoid at 10 MHz
        ts = np.linspace(0.0, 500e-9, 1001)
        vt = 12.0 + 10.0 * np.sin(2.0 * math.pi * 10e6 * ts)
        tr_comp = rf_compress.compress_waveform_block("time", ts, {"Vdrain": vt}, block_name="Tran1.Tran", max_curve_points=24)
        self.assertEqual(tr_comp["kind"], "transient")
        self.assertAlmostEqual(tr_comp["signals"]["Vdrain"]["pk_to_pk"], 20.0, delta=0.05)
        self.assertAlmostEqual(tr_comp["signals"]["Vdrain"]["estimated_freq_hz"], 10e6, delta=5e5)


class RFOptimizerTests(unittest.TestCase):
    def _make_synthetic_rc_sim(self):
        freqs = np.logspace(0, 4, 201)
        store: dict[str, dict] = {}
        counter = {"n": 0}

        def sim_fn(nl_text: str) -> dict:
            parsed = netlist_parser.inspect_netlist(nl_text)
            r_val = parsed["variables"]["R_val"]["si_value"]
            c_val = 1e-6
            vout = 1.0 / (1.0 + 1j * 2.0 * math.pi * freqs * r_val * c_val)
            counter["n"] += 1
            jid = f"sim_{counter['n']}"
            store[jid] = {
                "block": "AC1.AC",
                "axis": "freq",
                "axis_values": freqs.tolist(),
                "signals": {"VOUT": [[float(v.real), float(v.imag)] for v in vout]},
            }
            return {"state": "succeeded", "job_id": jid}

        def batch_fn(nl_list: list[str]) -> dict:
            jobs = [sim_fn(nl) for nl in nl_list]
            return {"total": len(jobs), "succeeded": len(jobs), "jobs": jobs}

        def extract_fn(job_id: str, block: str | None, axis: str | None) -> dict:
            return store[job_id]

        return sim_fn, batch_fn, extract_fn

    def test_optimizer_converges_across_methods(self) -> None:
        base_nl = (
            "R_val=3 kOhm\n"
            "V_Source:SRC1 IN 0 Type=\"V_AC\" Vac=polar(1,0)\n"
            "R:R1 IN OUT R=R_val\n"
            "C:C1 OUT 0 C=1 uF\n"
            "AC:AC1 Start=1 Hz Stop=10 kHz Dec=50\n"
        )
        r_exact = 1.0 / (2.0 * math.pi * 250.0 * 1e-6)  # ~636.62 Ohm
        params = [{"ref": "VAR:R_val", "min": 100.0, "max": 5000.0, "init": 3000.0, "unit": "Ohm", "scale": "log"}]
        objs = [{"id": "fc", "block": "AC1.AC", "axis": "freq", "signal": "VOUT", "metric": "cutoff_3db_hz", "op": "eq", "target": 250.0}]

        for method in ("nelder_mead", "powell", "grid_refine"):
            sim_fn, batch_fn, extract_fn = self._make_synthetic_rc_sim()
            res = rf_optimizer.optimize_netlist(
                base_nl,
                params,
                objs,
                simulate_fn=sim_fn,
                extract_block_fn=extract_fn,
                batch_simulate_fn=batch_fn,
                method=method,
                max_evals=20,
                tol=1e-4,
            )
            self.assertTrue(res["ok"], f"{method} failed: {res}")
            r_opt = res["best_parameters"]["VAR:R_val"]["value"]
            self.assertLess(abs(r_opt - r_exact) / r_exact, 0.02, f"{method} R_opt={r_opt} vs {r_exact}")
            self.assertLess(res["best_cost"], res["initial_cost"] * 0.01)

    def test_optimizer_validation_errors(self) -> None:
        base_nl = "R_val=1 kOhm\nR:R1 A 0 R=R_val\n"
        dummy = lambda *_a, **_kw: {"state": "failed"}
        with self.assertRaises(ValueError):
            rf_optimizer.optimize_netlist(
                base_nl,
                [{"ref": "VAR:R_val", "min": 10.0, "max": 5.0}],
                [{"metric": "cutoff_3db_hz", "op": "eq", "target": 100.0}],
                simulate_fn=dummy,
                extract_block_fn=dummy,
            )
        with self.assertRaises(ValueError):
            rf_optimizer.optimize_netlist(
                base_nl,
                [{"ref": "VAR:R_val", "min": 1.0, "max": 10.0}],
                [{"metric": "not_a_real_metric", "op": "eq", "target": 1.0}],
                simulate_fn=dummy,
                extract_block_fn=dummy,
            )


class ObstacleAwareRoutingTests(unittest.TestCase):
    def _hits_any(self, pts: list[tuple[float, float]], obstacles: list[tuple[float, float]]) -> bool:
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            for ox, oy in obstacles:
                if abs(x1 - x2) <= 1e-6 and abs(ox - x1) <= 1e-6 and min(y1, y2) - 1e-6 <= oy <= max(y1, y2) + 1e-6:
                    return True
                if abs(y1 - y2) <= 1e-6 and abs(oy - y1) <= 1e-6 and min(x1, x2) - 1e-6 <= ox <= max(x1, x2) + 1e-6:
                    return True
        return False

    def test_clear_straight_and_detour_around_obstacles(self) -> None:
        # 1. Clear straight horizontal line
        pts = design_ops.route_orthogonal((0.0, 0.0), (4.0, 0.0), obstacles=[(2.0, 1.0)])
        self.assertEqual(pts, [(0.0, 0.0), (4.0, 0.0)])

        # 2. Blocked straight horizontal line -> must jog around (2.0, 0.0)
        obs_straight = [(2.0, 0.0)]
        pts_detour = design_ops.route_orthogonal((0.0, 0.0), (4.0, 0.0), obstacles=obs_straight)
        self.assertGreater(len(pts_detour), 2)
        self.assertFalse(self._hits_any(pts_detour, obs_straight))

        # 3. Diagonal pair with primary L-bend blocked -> picks alternate L-bend
        obs_l1 = [(4.0, 0.0)]
        pts_l2 = design_ops.route_orthogonal((0.0, 0.0), (4.0, 4.0), obstacles=obs_l1)
        self.assertEqual(pts_l2, [(0.0, 0.0), (0.0, 4.0), (4.0, 4.0)])
        self.assertFalse(self._hits_any(pts_l2, obs_l1))

        # 4. Both L-bends blocked -> Z-detour avoiding both obstacles
        obs_both = [(4.0, 0.0), (0.0, 4.0)]
        pts_z = design_ops.route_orthogonal((0.0, 0.0), (4.0, 4.0), obstacles=obs_both)
        self.assertGreaterEqual(len(pts_z), 4)
        self.assertFalse(self._hits_any(pts_z, obs_both))


class TokenEfficiencyV22Tests(unittest.TestCase):
    def test_tabular_compression_and_param_diff(self) -> None:
        tab = netlist_parser.compress_netlist(VERBOSE_ADS_NETLIST, format="tabular")
        js = netlist_parser.compress_netlist(VERBOSE_ADS_NETLIST, format="json")
        self.assertEqual(tab["schema"], "ads_bridge.netlist_tabular.v2")
        self.assertEqual(tab["topo_cols"], ["id", "type", "nodes", "params"])
        self.assertLess(tab["stats"]["compact_chars"], js["stats"]["compact_chars"])
        self.assertGreater(tab["stats"]["vs_inspect_ratio"], 5.0)
        self.assertTrue(any(row[0] == "VAR:L_val" for row in tab["params"]))

        patched = netlist_parser.apply_netlist_parameters(VERBOSE_ADS_NETLIST, {"VAR:L_val": "2.2 nH"})
        delta = netlist_parser.diff_netlist_params(VERBOSE_ADS_NETLIST, patched)
        self.assertEqual(delta["changed"], {"VAR:L_val": ["4.7 nH", "2.2 nH"]})
        self.assertGreaterEqual(delta["unchanged_count"], 2)

    def test_netlist_handle_register_patch_and_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rt = Path(tmp)
            reg = rf_workflow.register_netlist(VERBOSE_ADS_NETLIST, rt, lambda p: p.mkdir(parents=True, exist_ok=True))
            hid = reg["netlist_id"]
            self.assertTrue(rf_workflow.is_netlist_handle(hid))
            loaded_text, _ = rf_workflow.resolve_netlist_handle(rt, hid, lambda p: p.mkdir(parents=True, exist_ok=True))
            self.assertIn("L_val=4.7 nH", loaded_text)

            patched = rf_workflow.patch_netlist(
                loaded_text,
                {"VAR:C_val": "3.3 pF"},
                rt,
                lambda p: p.mkdir(parents=True, exist_ok=True),
                parent_id=hid,
            )
            self.assertEqual(patched["parent_id"], hid)
            self.assertNotEqual(patched["netlist_id"], hid)
            self.assertEqual(patched["delta"]["changed"], {"VAR:C_val": ["1.2 pF", "3.3 pF"]})

    def test_distill_simulation_log_dedupes_and_hints(self) -> None:
        noisy_log = "\n".join([
            "Keysight Technologies hpeesofsim (*) 64-bit",
            "Copyright 1989-2026 Keysight Technologies",
            "Number of threads: 8",
            "Warning detected by hpeesofsim during DC analysis: Floating node N_float.",
            "Warning detected by hpeesofsim during DC analysis: Floating node N_float.",
            "Error detected by hpeesofsim during netlist flattening: Undefined variable `BAD_VAR`.",
            "Error detected by hpeesofsim during netlist flattening: Undefined variable `BAD_VAR`.",
            "Total stopwatch time: 0.42 seconds.",
        ])
        card = worker.distill_simulation_log(noisy_log)
        self.assertEqual(card["error_count"], 2)
        self.assertEqual(len(card["unique_errors"]), 1)
        self.assertEqual(card["unique_errors"][0]["count"], 2)
        self.assertEqual(card["warning_count"], 2)
        self.assertTrue(any("VAR/parameter" in h for h in card["hints"]))
        self.assertTrue(any("floating DC node" in h for h in card["hints"]))

    def test_design_eval_step_and_sensitivity_scan_offline(self) -> None:
        freqs = np.logspace(0, 4, 201)
        base_nl = (
            "R_val=2 kOhm\n"
            "C_val=1 uF\n"
            "V_Source:SRC1 IN 0 Type=\"V_AC\" Vac=polar(1,0)\n"
            "R:R1 IN OUT R=R_val\n"
            "C:C1 OUT 0 C=C_val\n"
            "AC:AC1 Start=1 Hz Stop=10 kHz Dec=50\n"
        )
        store: dict[str, dict] = {}
        counter = {"n": 0}

        def sim_fn(nl_text: str, **_kw) -> dict:
            parsed = netlist_parser.inspect_netlist(nl_text)
            r_v = parsed["variables"]["R_val"]["si_value"]
            c_v = parsed["variables"]["C_val"]["si_value"]
            vout = 1.0 / (1.0 + 1j * 2.0 * math.pi * freqs * r_v * c_v)
            counter["n"] += 1
            jid = f"j_{counter['n']}"
            store[jid] = {
                "block": "AC1.AC",
                "axis": "freq",
                "axis_values": freqs.tolist(),
                "signals": {"VOUT": [[float(v.real), float(v.imag)] for v in vout]},
            }
            return {"state": "succeeded", "job_id": jid}

        def batch_fn(nl_list: list[str]) -> dict:
            jobs = [sim_fn(nl) for nl in nl_list]
            return {"total": len(jobs), "succeeded": len(jobs), "jobs": jobs}

        def extract_fn(job_id: str, block: str | None, axis: str | None) -> dict:
            return store[job_id]

        with tempfile.TemporaryDirectory() as tmp:
            rt = Path(tmp)
            # 1. Atomic eval step with patch + passing scorecard
            ev = rf_workflow.run_design_eval_step(
                base_nl,
                rt,
                lambda p: p.mkdir(parents=True, exist_ok=True),
                simulate_fn=sim_fn,
                extract_block_fn=extract_fn,
                updates={"VAR:R_val": "1 kOhm"},
                specs=[
                    {"id": "fc_min", "block": "AC1.AC", "axis": "freq", "signal": "VOUT", "metric": "cutoff_3db_hz", "op": "ge", "target": 150.0}
                ],
            )
            self.assertTrue(ev["ok"] and ev["all_pass"], ev)
            self.assertEqual(ev["scorecard"][0][3], "PASS")
            self.assertEqual(ev["delta"]["changed"], {"VAR:R_val": ["2 kOhm", "1 kOhm"]})

            # 2. ERC short-circuit before simulation on broken netlist
            bad_ev = rf_workflow.run_design_eval_step(
                "R:R1 A 0 R=-5 Ohm\n",
                rt,
                lambda p: p.mkdir(parents=True, exist_ok=True),
                simulate_fn=sim_fn,
                extract_block_fn=extract_fn,
            )
            self.assertFalse(bad_ev["ok"])
            self.assertEqual(bad_ev["stage"], "erc")

        # 3. Sensitivity scan on R_val & C_val: both have negative slope on fc = 1/(2*pi*R*C)
        sens = rf_optimizer.sensitivity_scan(
            base_nl,
            ["VAR:R_val", "VAR:C_val"],
            [{"id": "fc", "block": "AC1.AC", "axis": "freq", "signal": "VOUT", "metric": "cutoff_3db_hz", "op": "eq", "target": 80.0}],
            simulate_fn=sim_fn,
            extract_block_fn=extract_fn,
            batch_simulate_fn=batch_fn,
            step_pct=5.0,
        )
        self.assertTrue(sens["ok"])
        self.assertEqual(sens["evaluations"], 5)
        self.assertEqual(len(sens["ranked_params"]), 2)
        for row in sens["ranked_params"]:
            self.assertLess(row["effects"]["fc"]["delta_per_1pct"], 0.0)

    def test_mcp_profile_tool_counts(self) -> None:
        import mcp_server
        self.assertEqual(len(mcp_server.RF_CORE_TOOLS), 13)
        self.assertEqual(len(mcp_server.HEADLESS_TOOLS), 19)
        self.assertTrue(mcp_server.RF_CORE_TOOLS < mcp_server.HEADLESS_TOOLS)
        with self.assertRaises(ValueError):
            mcp_server.apply_mcp_profile("nonexistent_profile")


class AuditRecheckV221RegressionTests(unittest.TestCase):
    """Regression tests covering all 9 findings from ADS-BRIDGE-2.2.0-RECHECK.md."""

    def test_h1_duplicate_spec_ids_rejected_and_dual_freq_fails_accurately(self) -> None:
        freqs = np.logspace(0, 4, 201)
        rc = 3000.0 * 1e-6
        vout = 1.0 / (1.0 + 1j * 2.0 * math.pi * freqs * rc)
        raw_block = {
            "block": "AC1.AC",
            "axis": "freq",
            "axis_values": freqs.tolist(),
            "signals": {"VOUT": [[float(v.real), float(v.imag)] for v in vout]},
        }
        dup_specs = [
            {"id": "gain", "block": "AC1.AC", "axis": "freq", "signal": "VOUT", "metric": "db20_at", "at_hz": 1000.0, "op": "ge", "target": -1.0},
            {"id": "gain", "block": "AC1.AC", "axis": "freq", "signal": "VOUT", "metric": "db20_at", "at_hz": 1.0, "op": "ge", "target": -1.0},
        ]
        with self.assertRaises(ValueError):
            rf_optimizer.evaluate_specs_scorecard(dup_specs, lambda _b, _a: raw_block)

        distinct_specs = [
            {**dup_specs[0], "id": "gain_1khz"},
            {**dup_specs[1], "id": "gain_1hz"},
        ]
        score = rf_optimizer.evaluate_specs_scorecard(distinct_specs, lambda _b, _a: raw_block)
        self.assertFalse(score["all_pass"])
        self.assertEqual(score["scorecard"][0][3], "FAIL")
        self.assertEqual(score["scorecard"][1][3], "PASS")

    def test_h2_band_hz_isolates_matched_bw_and_rejects_empty_band(self) -> None:
        freqs = np.linspace(1.0e9, 5.0e9, 401)
        s_mat = np.zeros((len(freqs), 2, 2), dtype=np.complex128)
        # Matched (-20 dB) only across 1-3 GHz; poor (-2 dB) across 3-5 GHz
        s11_mag = np.where(freqs <= 3.0e9, 0.1, 0.794)
        s21_mag = np.sqrt(1.0 - s11_mag ** 2)
        s_mat[:, 0, 0] = s11_mag
        s_mat[:, 1, 1] = s11_mag
        s_mat[:, 1, 0] = s21_mag
        s_mat[:, 0, 1] = s21_mag

        in_matched = rf_compress.compress_sparam_network(freqs, s_mat, band_hz=(1.0e9, 3.0e9))
        self.assertAlmostEqual(in_matched["parameters"]["S[1,1]"]["matched_bw_hz"], 2.0e9, delta=2e7)

        unmatched_band = rf_compress.compress_sparam_network(freqs, s_mat, band_hz=(4.0e9, 5.0e9))
        self.assertEqual(unmatched_band["parameters"]["S[1,1]"]["matched_bw_hz"], 0.0)
        self.assertEqual(unmatched_band["parameters"]["S[1,1]"]["matched_bands_hz"], [])

        with self.assertRaises(ValueError):
            rf_compress.compress_sparam_network(freqs, s_mat, band_hz=(6.0e9, 7.0e9))

    def test_h3_hb_no_fake_pae_without_pin_and_no_voltage_dbm_for_current_probe(self) -> None:
        hb_freqs = np.arange(0, 5) * 13.56e6
        v_hb = np.array([0.0, 20.0 + 0j, 0.2 + 0j, 0.1 + 0j, 0.05 + 0j])
        i_hb = np.array([0.22 + 0j, 0.1 + 0j, 0.05 + 0j, 0.02 + 0j, 0.01 + 0j])

        hb_no_pin = rf_compress.compress_waveform_block(
            "freq", hb_freqs, {"Vout": v_hb, "IDS.i": i_hb},
            block_name="HB1.HB", load_ohm=50.0, vds_v=20.0, ids_signal="IDS.i", vout_signal="Vout",
        )
        self.assertAlmostEqual(hb_no_pin["pa_metrics"]["drain_eff_percent"], 4.0 / 4.4 * 100.0, places=2)
        self.assertIsNone(hb_no_pin["pa_metrics"]["pae_percent"])
        self.assertIsNone(hb_no_pin["pa_metrics"]["gain_db"])
        # Current probe must NOT receive voltage-derived power_w / power_dbm / harmonic dbm
        self.assertEqual(hb_no_pin["signals"]["IDS.i"]["quantity"], "current")
        self.assertIsNone(hb_no_pin["signals"]["IDS.i"]["power_w"])
        self.assertIsNone(hb_no_pin["signals"]["IDS.i"]["power_dbm"])
        self.assertTrue(all(h["dbm"] is None for h in hb_no_pin["signals"]["IDS.i"]["harmonics"]))

        # Requesting pae_percent metric without pin_dbm must fail cleanly
        raw_hb = {
            "block": "HB1.HB",
            "axis": "freq",
            "axis_values": hb_freqs.tolist(),
            "signals": {
                "Vout": [[float(v.real), float(v.imag)] for v in v_hb],
                "IDS.i": [[float(i.real), float(i.imag)] for i in i_hb],
            },
        }
        with self.assertRaises(ValueError):
            rf_optimizer.extract_objective_metric(
                raw_hb,
                rf_optimizer._validate_objectives([{
                    "id": "pae", "metric": "pae_percent", "op": "ge", "target": 80.0,
                    "signal": "Vout", "vds_v": 20.0, "ids_signal": "IDS.i",
                }])[0],
            )

    def test_h4_missing_null_complex_sample_rejected_not_zeroed_to_minus_360db(self) -> None:
        raw_block = {
            "block": "AC1.AC",
            "axis": "freq",
            "axis_values": [10.0, 100.0, 1000.0],
            "signals": {"VOUT": [[1.0, 0.0], [None, 0.0], [0.5, 0.0]]},
        }
        obj = rf_optimizer._validate_objectives([{
            "id": "at100", "metric": "db20_at", "op": "ge", "target": -3.0,
            "signal": "VOUT", "at_hz": 100.0,
        }])[0]
        with self.assertRaises(ValueError):
            rf_optimizer.extract_objective_metric(raw_block, obj)

        # Bracketing a missing sample must also be rejected
        obj_mid = rf_optimizer._validate_objectives([{
            "id": "at50", "metric": "db20_at", "op": "ge", "target": -3.0,
            "signal": "VOUT", "at_hz": 50.0,
        }])[0]
        with self.assertRaises(ValueError):
            rf_optimizer.extract_objective_metric(raw_block, obj_mid)

    def test_h5_and_m1_multiline_continuation_sweep_and_comment_safety(self) -> None:
        multiline_nl = (
            'R_val=3 kOhm ; R=999 Ohm comment\n'
            'V_Source:SRC1 VIN 0 Type="V_AC" Vac=polar(1,0)\n'
            'R:R1 VIN VOUT \\\n'
            '    R=100 Ohm ; R=777 Ohm\n'
            'C:C1 VOUT 0 C=1 uF ; L=5 nH\n'
            'AC:AC1 CalcNoise=no \\\n'
            '    SweepVar="freq" SweepPlan="AC1_stim" \\\n'
            '    OutputPlan="AC1_Output"\n'
            'SweepPlan: AC1_stim Start=1 Hz Stop=1000 Hz Dec=10\n'
            'OutputPlan:AC1_Output Type="Output"\n'
        )
        # M1: patch parameter on continuation line succeeds and preserves comment
        patched = netlist_parser.apply_netlist_parameters(multiline_nl, {"R1.R": "200 Ohm"})
        self.assertIn("R=200 Ohm ; R=777 Ohm", patched)

        # M1: comment-only parameter C1.L must NOT be modified
        with self.assertRaises(netlist_parser.NetlistError):
            netlist_parser.apply_netlist_parameters(multiline_nl, {"C1.L": "10 nH"})

        # H5: parameter sweep over multi-line AC injects ParamSweep without touching `\` continuations
        swept_param = netlist_parser.inject_sweep_plan(multiline_nl, var="R_val", start="1 kOhm", stop="5 kOhm", lin=5)
        self.assertIn('ParamSweep:SW_BRIDGE_CTRL SimInstanceName[1]="AC1" SweepVar="R_val" SweepPlan="SW_BRIDGE"', swept_param)
        self.assertNotIn("\\ SweepVar", swept_param)

        # H5: freq sweep over multi-line AC cleanly rewrites full logical statement with zero stray `\`
        swept_freq = netlist_parser.inject_sweep_plan(multiline_nl, var="freq", start="10 Hz", stop="10 kHz", dec=20)
        self.assertNotIn("\\ SweepVar", swept_freq)
        self.assertEqual(swept_freq.count('SweepVar="freq"'), 1)

    def test_m2_real_ac_divider_and_zero_start_linear_ac_not_misclassified(self) -> None:
        # Purely real resistive divider AC sweep (0.5 + 0j across all points)
        freqs = np.logspace(1, 5, 10)
        v_div = np.full(10, 0.5 + 0j)
        ac_div = rf_compress.compress_waveform_block("freq", freqs, {"VOUT": v_div}, block_name="AC1.AC")
        self.assertEqual(ac_div["kind"], "ac")
        self.assertIn("db20_max", ac_div["signals"]["VOUT"])
        self.assertIn("ripple_db", ac_div["signals"]["VOUT"])
        self.assertNotIn("rms", ac_div["signals"]["VOUT"])
        self.assertAlmostEqual(ac_div["signals"]["VOUT"]["db20_max"], -6.0206, places=3)

        # Zero-start linear AC block must stay 'ac', never misclassified as 'harmonic_balance'
        lin_freqs = np.linspace(0.0, 90.0, 10)
        ac_lin = rf_compress.compress_waveform_block("freq", lin_freqs, {"VOUT": v_div}, block_name="AC1.AC")
        self.assertEqual(ac_lin["kind"], "ac")
        self.assertIn("db20_max", ac_lin["signals"]["VOUT"])

    def test_m3_batch_chunking_over_32_jobs_preserves_order(self) -> None:
        # 16 parameters -> 33 netlists (1 baseline + 32 perturbations)
        var_lines = "\n".join(f"P{i}={i + 1} Ohm" for i in range(16))
        nl = var_lines + "\nR:R1 A 0 R=P0\nAC:AC1 Start=1 Hz Stop=10 Hz Lin=5\n"
        chunk_sizes: list[int] = []
        store: dict[str, dict] = {}
        seq = {"n": 0}

        def batch_fn(nl_chunk: list[str]) -> dict:
            self.assertLessEqual(len(nl_chunk), 32)
            chunk_sizes.append(len(nl_chunk))
            jobs = []
            for item in nl_chunk:
                seq["n"] += 1
                jid = f"b_{seq['n']}"
                p = netlist_parser.inspect_netlist(item)
                val = p["variables"]["P0"]["si_value"]
                store[jid] = {
                    "block": "AC1.AC",
                    "axis": "freq",
                    "axis_values": [1.0, 5.0, 10.0],
                    "signals": {"VOUT": [[val, 0.0], [val, 0.0], [val, 0.0]]},
                }
                jobs.append({"state": "succeeded", "job_id": jid})
            return {"total": len(jobs), "succeeded": len(jobs), "jobs": jobs}

        sens = rf_optimizer.sensitivity_scan(
            nl,
            [f"VAR:P{i}" for i in range(16)],
            [{"id": "mag5", "block": "AC1.AC", "axis": "freq", "signal": "VOUT", "metric": "mag_at", "at_hz": 5.0, "op": "eq", "target": 1.0}],
            simulate_fn=lambda _x: {"state": "failed"},
            extract_block_fn=lambda jid, _b, _a: store[jid],
            batch_simulate_fn=batch_fn,
            step_pct=5.0,
        )
        self.assertTrue(sens["ok"])
        self.assertEqual(chunk_sizes, [32, 1])
        self.assertEqual(sens["evaluations"], 33)
        self.assertEqual(sens["ranked_params"][0]["ref"], "VAR:P0")


class AuditRecheckV222RegressionTests(unittest.TestCase):
    """Regression tests covering all 5 findings from ADS-BRIDGE-2.2.1-RECHECK.md."""

    def test_h1_s_db_at_rejects_ac_voltage_and_accepts_single_sparam_curve(self) -> None:
        ac_block = {
            "block": "AC1.AC",
            "axis": "freq",
            "axis_values": [1.0, 100.0, 1000.0],
            "signals": {"VOUT": [[1.0, 0.0], [0.5, 0.0], [0.157, 0.0]]},
        }
        bad_spec = [
            {"id": "fake_s11", "block": "AC1.AC", "axis": "freq", "signal": "VOUT", "metric": "s_db_at", "at_hz": 1000.0, "op": "le", "target": -10.0}
        ]
        with self.assertRaises(ValueError):
            rf_optimizer.evaluate_specs_scorecard(bad_spec, lambda _b, _a: ac_block)

        # Single valid S[1,1] curve inside an S-parameter block MUST still work
        sp_single = {
            "block": "SP1.SP",
            "axis": "freq",
            "axis_values": [1e9, 2e9, 3e9],
            "signals": {"S[1,1]": [[0.3, 0.0], [0.1, 0.0], [0.3, 0.0]]},
        }
        good_spec = [
            {"id": "s11_2g", "block": "SP1.SP", "axis": "freq", "signal": "S[1,1]", "metric": "s_db_at", "at_hz": 2e9, "op": "le", "target": -15.0}
        ]
        score = rf_optimizer.evaluate_specs_scorecard(good_spec, lambda _b, _a: sp_single)
        self.assertTrue(score["all_pass"])
        self.assertAlmostEqual(score["scorecard"][0][1], -20.0, places=3)

    def test_h2_current_probe_rejected_for_pout_in_scorecard_and_compressor(self) -> None:
        hb_block = {
            "block": "HB1.HB",
            "axis": "freq",
            "axis_values": [0.0, 13.56e6, 27.12e6],
            "signals": {
                "VOUT": [[0.0, 0.0], [20.0, 0.0], [0.2, 0.0]],
                "VOPA.i": [[0.22, 0.0], [0.0109055, 0.0], [0.001, 0.0]],
            },
        }
        bad_pout_spec = [
            {"id": "pout_from_i", "block": "HB1.HB", "axis": "freq", "signal": "VOPA.i", "metric": "pout_w", "op": "le", "target": 1.0}
        ]
        with self.assertRaises(ValueError):
            rf_optimizer.evaluate_specs_scorecard(bad_pout_spec, lambda _b, _a: hb_block)

        # Even if caller explicitly passes vout_signal="VOPA.i" to compress_waveform_block, it stays current with power_w=None
        comp = rf_compress.compress_waveform_block(
            "freq",
            hb_block["axis_values"],
            {k: rf_compress.decode_complex_pairs(v) for k, v in hb_block["signals"].items()},
            block_name="HB1.HB",
            vout_signal="VOPA.i",
            vds_v=20.0,
            ids_signal="VOPA.i",
        )
        self.assertEqual(comp["signals"]["VOPA.i"]["quantity"], "current")
        self.assertIsNone(comp["signals"]["VOPA.i"]["power_w"])
        self.assertNotIn("pa_metrics", comp)

    def test_m1_missing_freq_splits_matched_bw_and_missing_harmonic_makes_thd_none(self) -> None:
        # 5 freq points: 1, 2, 4, 5 GHz matched (-20 dB); 3 GHz missing (NaN) -> MUST split into two 1 GHz bands, NOT 4 GHz
        freqs = np.array([1e9, 2e9, 3e9, 4e9, 5e9], dtype=np.float64)
        s_mat = np.zeros((5, 2, 2), dtype=np.complex128)
        for idx in (0, 1, 3, 4):
            s_mat[idx, 0, 0] = 0.1
            s_mat[idx, 1, 1] = 0.1
            s_mat[idx, 1, 0] = 0.9
            s_mat[idx, 0, 1] = 0.9
        s_mat[2, :, :] = complex(math.nan, math.nan)
        sp_comp = rf_compress.compress_sparam_network(freqs, s_mat, return_loss_spec_db=-10.0)
        self.assertEqual(sp_comp["missing_samples"], 4)
        self.assertEqual(len(sp_comp["parameters"]["S[1,1]"]["matched_bands_hz"]), 2)
        self.assertAlmostEqual(sp_comp["parameters"]["S[1,1]"]["matched_bw_hz"], 1.0e9, delta=1e3)

        # HB with valid fundamental and missing unique higher harmonic -> thd_percent MUST be None (never 0%)
        hb_freqs = np.array([0.0, 13.56e6, 27.12e6], dtype=np.float64)
        v_missing_h2 = np.array([0.0 + 0j, 20.0 + 0j, complex(math.nan, math.nan)], dtype=np.complex128)
        hb_comp = rf_compress.compress_waveform_block("freq", hb_freqs, {"Vout": v_missing_h2}, block_name="HB1.HB")
        self.assertIsNone(hb_comp["signals"]["Vout"]["thd_percent"])
        self.assertEqual(hb_comp["missing_samples"], 1)

    def test_m2_reinject_sweep_plan_replaces_without_duplicate_controller(self) -> None:
        base = (
            'R_val=3 kOhm\n'
            'V_Source:SRC1 VIN 0 Type="V_AC" Vac=polar(1,0)\n'
            'R:R1 VIN VOUT R=R_val\n'
            'C:C1 VOUT 0 C=1 uF\n'
            'AC:AC1 CalcNoise=no \\\n'
            '    SweepVar="freq" SweepPlan="AC1_stim"\n'
            'SweepPlan: AC1_stim Start=1 Hz Stop=1000 Hz Dec=10\n'
        )
        sw1 = netlist_parser.inject_sweep_plan(base, var="R_val", start="1 kOhm", stop="5 kOhm", lin=5)
        sw2 = netlist_parser.inject_sweep_plan(sw1, var="R_val", start="2 kOhm", stop="10 kOhm", lin=5)
        self.assertEqual(sw2.count("ParamSweep:SW_BRIDGE_CTRL"), 1)
        self.assertEqual(sw2.count("SweepPlan: SW_BRIDGE"), 1)
        self.assertIn("Start=2 kOhm Stop=10 kOhm Lin=5", sw2)
        self.assertNotIn("Start=1 kOhm", sw2)


class RetroPerfAndStabilityV230Tests(unittest.TestCase):
    """Tests for 2.3.0 retroactive fixes eliminating stalls and repetitive friction."""

    def test_distill_log_preserves_indented_hpeesofsim_detail_lines(self) -> None:
        raw_log = (
            "Warning detected by hpeesofsim in topology check during circuit set up.\n"
            "    Number of nodes with no DC path to ground (topology corrected): 3\n\n"
            "Error detected by hpeesofsim during HB analysis `HB1'.\n"
            "    No convergence.\n"
        )
        card = worker.distill_simulation_log(raw_log)
        self.assertEqual(card["error_count"], 1)
        self.assertIn("No convergence.", card["unique_errors"][0]["msg"])
        self.assertEqual(card["warning_count"], 1)
        self.assertIn("Number of nodes with no DC path to ground", card["unique_warnings"][0]["msg"])

    def test_strip_analysis_controllers_handles_multiline_continuations(self) -> None:
        nl = (
            "R_val=1 kOhm\n"
            "R:R1 IN OUT R=R_val\n"
            'HarmonicBalance:HB1 Freq[1]=13.56 MHz \\\n'
            '    Order[1]=15 Oversample[1]=4 \\\n'
            '    OutputPlan="HB1_Output"\n'
            'OutputPlan:HB1_Output Type="Output"\n'
            "SweepPlan: SW1 Start=0 Stop=15 Step=1\n"
        )
        stripped = netlist_parser.strip_analysis_controllers(nl)
        self.assertIn("R:R1 IN OUT R=R_val", stripped)
        self.assertNotIn("HarmonicBalance", stripped)
        self.assertNotIn("Order[1]=15", stripped)
        self.assertNotIn("OutputPlan", stripped)
        self.assertNotIn("SweepPlan", stripped)

    def test_list_sessions_prunes_dead_pid_stale_records(self) -> None:
        import os
        with tempfile.TemporaryDirectory() as tmp:
            old_env = os.environ.get("ADS_BRIDGE_RUNTIME")
            os.environ["ADS_BRIDGE_RUNTIME"] = tmp
            try:
                sdir = Path(tmp) / "sessions"
                sdir.mkdir(parents=True, exist_ok=True)
                dead_sid = "a" * 32
                dead_file = sdir / f"{dead_sid}.json"
                bridge.write_json(dead_file, {
                    "session_id": dead_sid,
                    "pid": 99999999,
                    "protocol": bridge.PROTOCOL,
                    "ads_version": "ADS2026_Update0.1",
                    "bridge_version": "2.3.0",
                    "port": 65000,
                    "token": "x",
                })
                sessions = bridge.list_sessions(prune_stale=True)
                self.assertEqual(sessions, [])
                self.assertFalse(dead_file.exists())
            finally:
                if old_env is None:
                    os.environ.pop("ADS_BRIDGE_RUNTIME", None)
                else:
                    os.environ["ADS_BRIDGE_RUNTIME"] = old_env

    def test_pin_polarity_hints_and_safe_install_addon_import(self) -> None:
        self.assertIn("V_DC", design_ops._PIN_POLARITY_HINTS)
        self.assertIn("P_1Tone", design_ops._PIN_POLARITY_HINTS)
        self.assertIn("SDD1P", design_ops._PIN_POLARITY_HINTS)
        self.assertIn("SDD3P", design_ops._PIN_POLARITY_HINTS)
        self.assertIn("Diode", design_ops._PIN_POLARITY_HINTS)
        import install_addon
        with self.assertRaises(RuntimeError):
            install_addon.install()


class DogfoodAuditV231Tests(unittest.TestCase):
    def test_multidim_2d_sweep_guard_and_batch_slice_compression(self) -> None:
        # Unfiltered 2D HB block with outer_axes must raise clearly instead of fabricating 7,000,000% THD
        freqs_1d = [0.0, 13.56e6, 27.12e6, 40.68e6]
        sigs = {"VOUT": [0.0 + 0j, 20.0 + 0j, 0.2 + 0j, 0.05 + 0j]}
        with self.assertRaises(ValueError) as ctx:
            rf_compress.compress_waveform_block(
                "freq",
                freqs_1d,
                sigs,
                block_name="SW_BRIDGE_CTRL.HB1.HB",
                outer_axes={"Pin_dBm": [0.0, 5.0, 10.0, 15.0]},
            )
        self.assertIn("Pin_dBm", str(ctx.exception))

        # Non-monotonic flattened freq axis must also be rejected even without outer_axes metadata
        with self.assertRaises(ValueError):
            rf_compress.compress_waveform_block(
                "freq",
                freqs_1d + freqs_1d,
                {"VOUT": sigs["VOUT"] + sigs["VOUT"]},
                block_name="HB1.HB",
            )

        # Single-pass sweep compression with dynamic vds_signal + pin_from_sweep_axis
        slices = [
            {
                "sweep_value": 0.0,
                "axis_values": freqs_1d,
                "signals": {
                    "VOUT": [[0.0, 0.0], [10.0, 0.0], [0.1, 0.0], [0.02, 0.0]],
                    "VDS": [[5.6, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]],
                    "I_DS.i": [[0.22, 0.0], [0.01, 0.0], [0.0, 0.0], [0.0, 0.0]],
                },
            },
            {
                "sweep_value": 15.0,
                "axis_values": freqs_1d,
                "signals": {
                    "VOUT": [[0.0, 0.0], [20.32, 0.0], [0.2, 0.0], [0.05, 0.0]],
                    "VDS": [[10.12, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]],
                    "I_DS.i": [[0.438, 0.0], [0.02, 0.0], [0.0, 0.0], [0.0, 0.0]],
                },
            },
        ]
        sw = rf_compress.compress_sweep_slices(
            "freq",
            "Pin_dBm",
            slices,
            block_name="SW_BRIDGE_CTRL.HB1.HB",
            vds_signal="VDS",
            ids_signal="I_DS.i",
            vout_signal="VOUT",
            pin_from_sweep_axis=True,
        )
        self.assertEqual(sw["sweep_points"], 2)
        self.assertAlmostEqual(sw["slices"][0]["pa_metrics"]["vds_v"], 5.6, places=4)
        self.assertAlmostEqual(sw["slices"][1]["pa_metrics"]["vds_v"], 10.12, places=4)
        self.assertEqual(sw["slices"][1]["pa_metrics"]["vds_source"], "signal:VDS")
        self.assertGreater(sw["slices"][1]["pa_metrics"]["pae_percent"], 90.0)
        self.assertLess(sw["slices"][1]["pa_metrics"]["pae_percent"], 100.0)

    def test_pa_metrics_warns_when_efficiency_exceeds_100_percent(self) -> None:
        comp = rf_compress.compress_waveform_block(
            "freq",
            [0.0, 13.56e6, 27.12e6],
            {
                "VOUT": [0.0 + 0j, 30.0 + 0j, 0.1 + 0j],  # 9 W into 50 Ohm
                "I_DS.i": [0.5 + 0j, 0.01 + 0j, 0.0 + 0j],
            },
            block_name="HB1.HB",
            load_ohm=50.0,
            vds_v=10.0,  # Wrong hardcoded VDS -> 5 W DC vs 9 W RF -> 180%
            ids_signal="I_DS.i",
            vout_signal="VOUT",
            pin_dbm=15.0,
        )
        self.assertIn("warnings", comp["pa_metrics"])
        self.assertTrue(any("100%" in w for w in comp["pa_metrics"]["warnings"]))

    def test_optimizer_op_aliases_upfront_validation_and_pf_precision(self) -> None:
        # Operator aliases >=, <=, == and field aliases rload_ohm, vdc_signal, idc_signal
        objs = rf_optimizer._validate_objectives([
            {"id": "pout", "metric": "pout_w", "signal": "VOUT", "rload_ohm": 50.0, "op": "==", "target": 4.13},
            {"id": "pae", "metric": "pae_percent", "signal": "VOUT", "vdc_signal": "VDS", "idc_signal": "I_DS.i", "pin_dbm": 15.0, "op": ">=", "target": 92.5},
        ])
        self.assertEqual(objs[0]["op"], "eq")
        self.assertEqual(objs[1]["op"], "ge")
        self.assertEqual(objs[1]["vds_signal"], "VDS")
        self.assertEqual(objs[1]["ids_signal"], "I_DS.i")

        # Upfront validation must fail BEFORE simulation when pae_percent lacks vds/ids/pin
        with self.assertRaises(ValueError):
            rf_optimizer._validate_objectives([
                {"id": "bad_pae", "metric": "pae_percent", "signal": "VOUT", "op": ">=", "target": 90.0},
            ])

        # Significant-digit rounding must NOT truncate 51.317 pF (5.1317e-11) to 0.0
        self.assertGreater(rf_optimizer._sig_float(5.13169e-11, 10), 5.0e-11)
        self.assertAlmostEqual(rf_optimizer._sig_float(5.13169e-11, 10), 5.13169e-11, places=16)

    def test_strip_analysis_controllers_removes_atahb_companion(self) -> None:
        nl = (
            'Options ResourceUsage=yes\n'
            'R:R1 N1 0 R=50 Ohm\n'
            'HB:HB1 MaxOrder=11 Freq[1]=13.56 MHz Order[1]=11\n'
            'Tran:HB1_tran HB_Sol=1 SteadyState=1 \\\n'
            '  Freq[1]=13.56 MHz Order[1]=11\n'
            'Component:tahb_HB1 Module="ATAHB" Type="ModelExtractor" \\\n'
            '  Tran_Analysis="HB1_tran" HB_Analysis="HB1"\n'
        )
        cleaned = netlist_parser.strip_analysis_controllers(nl)
        self.assertIn("R:R1", cleaned)
        self.assertNotIn("HB:HB1", cleaned)
        self.assertNotIn("Tran:HB1_tran", cleaned)
        self.assertNotIn("tahb_HB1", cleaned)
        self.assertNotIn("ATAHB", cleaned)

    def test_route_orthogonal_avoids_existing_wire_segments_and_cleans_param_repr(self) -> None:
        # Existing wire along y=0 from x=0..10; routing from (2,0) to (8,0) must detour to avoid collinear overlap
        existing = [((0.0, 0.0), (10.0, 0.0))]
        verts = design_ops.route_orthogonal((2.0, 0.0), (8.0, 0.0), blocked_segments=existing)
        self.assertEqual(len(verts), 4)
        self.assertNotEqual(verts[1][1], 0.0)

        # Model default repr cleaner
        class FakeParamConst:
            def __str__(self) -> str:
                return '<ParamItemConst "NMOS"=form("y_n1")>'
        self.assertEqual(design_ops._model_default_value(FakeParamConst()), "y_n1")

    def test_unphysical_pae_above_100_percent_rejected_by_scorecard_and_cost(self) -> None:
        # Reproduce 399% PAE controlled HB data: summary flags physically_valid=False and warnings,
        # and BOTH scorecard/extract_objective_metric AND _evaluate_cost MUST refuse to mark PAE >= 90% as PASS!
        raw_block = {
            "block": "HB1.HB",
            "axis": "freq",
            "axis_values": [0.0, 13.56e6, 27.12e6],
            "signals": {
                # 20 W RF into 50 Ohm (V_peak = sqrt(2*50*20) = 44.72136 V), Pin = 15 dBm (0.03162 W),
                # Pdc = 10 V * 0.5 A = 5 W -> drain_eff = 400%, PAE = 399.37%
                "VOUT": [[0.0, 0.0], [44.72135955, 0.0], [0.1, 0.0]],
                "VDS": [[10.0, 0.0], [0.0, 0.0], [0.0, 0.0]],
                "I_DS.i": [[0.5, 0.0], [0.01, 0.0], [0.0, 0.0]],
            },
        }
        comp = rf_compress.compress_waveform_block(
            "freq",
            raw_block["axis_values"],
            {k: rf_compress.decode_complex_pairs(v) for k, v in raw_block["signals"].items()},
            block_name="HB1.HB",
            load_ohm=50.0,
            vds_signal="VDS",
            ids_signal="I_DS.i",
            vout_signal="VOUT",
            pin_dbm=15.0,
        )
        self.assertFalse(comp["pa_metrics"]["physically_valid"])
        self.assertGreater(comp["pa_metrics"]["pae_percent"], 390.0)
        self.assertIn("warnings", comp["pa_metrics"])

        specs = [
            {
                "id": "pae_spec",
                "metric": "pae_percent",
                "signal": "VOUT",
                "load_ohm": 50.0,
                "vds_signal": "VDS",
                "ids_signal": "I_DS.i",
                "pin_dbm": 15.0,
                "op": ">=",
                "target": 90.0,
            }
        ]
        with self.assertRaises(ValueError) as ctx:
            rf_optimizer.evaluate_specs_scorecard(specs, lambda _b, _a: raw_block)
        self.assertIn("Unphysical efficiency", str(ctx.exception))

        # Defense in depth: _evaluate_cost directly passed 399% PAE MUST NOT mark satisfied=True
        objs = rf_optimizer._validate_objectives(specs)
        total_cost, details, all_sat = rf_optimizer._evaluate_cost({"pae_spec": 399.37}, objs)
        self.assertFalse(all_sat)
        self.assertFalse(details["pae_spec"]["satisfied"])
        self.assertTrue(details["pae_spec"]["physical_violation"])
        self.assertGreater(total_cost, 1e6)

        # Even if huge Pin masks PAE below 100%, drain_eff > 100% must ALSO be rejected!
        specs_masked = [{**specs[0], "pin_dbm": 42.0}]  # Pin = 15.85 W -> PAE = (20-15.85)/5 = 83%, but drain_eff = 400%!
        with self.assertRaises(ValueError):
            rf_optimizer.evaluate_specs_scorecard(specs_masked, lambda _b, _a: raw_block)

        # Upfront target validation must reject target > 100%
        with self.assertRaises(ValueError):
            rf_optimizer._validate_objectives([{**specs[0], "target": 120.0}])

    def test_multidim_sweep_scorecard_never_passes_on_single_arbitrary_slice(self) -> None:
        # Reproduce 5-resistor RC ParamSweep (2k, 4k, 6k, 8k, 10k) with 1 kHz gain <= -30 dB:
        # 2k (-22.01 dB) and 4k (-28.01 dB) FAIL; 6k (-31.53 dB), 8k (-34.03 dB), 10k (-35.96 dB) PASS.
        c_val = 1.0e-6
        freqs = [10.0, 100.0, 1000.0, 10000.0]
        r_vals = [2000.0, 4000.0, 6000.0, 8000.0, 10000.0]

        def rc_pairs(r_ohm: float) -> list[list[float]]:
            out = []
            for f in freqs:
                h = 1.0 / (1.0 + 1j * 2.0 * math.pi * f * r_ohm * c_val)
                out.append([h.real, h.imag])
            return out

        # 1. Unfiltered flattened 2D raw_block WITHOUT extract_sweep_fn MUST be rejected for db20_at
        flat_xs: list[float] = []
        flat_vout: list[list[float]] = []
        for rv in r_vals:
            flat_xs.extend(freqs)
            flat_vout.extend(rc_pairs(rv))
        raw_2d = {
            "block": "SW_BRIDGE_CTRL.AC1.AC",
            "axis": "freq",
            "outer_axes": {"R_val": r_vals},
            "axis_values": flat_xs,
            "signals": {"VOUT": flat_vout},
        }
        specs_30 = [{"id": "gain_1k", "metric": "db20_at", "signal": "VOUT", "at_hz": 1000.0, "op": "<=", "target": -30.0}]
        with self.assertRaises(ValueError):
            rf_optimizer.evaluate_specs_scorecard(specs_30, lambda _b, _a: raw_2d)

        # Even without outer_axes metadata, duplicate freq coordinates MUST be rejected by _interp_at / guard
        raw_2d_no_meta = {**raw_2d, "outer_axes": {}}
        with self.assertRaises(ValueError):
            rf_optimizer.evaluate_specs_scorecard(specs_30, lambda _b, _a: raw_2d_no_meta)

        # 2. With extract_sweep_fn, evaluate_specs_scorecard scores EVERY slice -> all_pass=False (3/5 pass, 2/5 fail)
        sw_payload = {
            "block": "SW_BRIDGE_CTRL.AC1.AC",
            "axis": "freq",
            "sweep_axis": "R_val",
            "slices": [{"sweep_value": rv, "axis_values": freqs, "signals": {"VOUT": rc_pairs(rv)}} for rv in r_vals],
        }
        res_fail = rf_optimizer.evaluate_specs_scorecard(
            specs_30,
            lambda _b, _a: raw_2d,
            extract_sweep_fn=lambda _sw, _b, _a, _f=None: sw_payload,
        )
        self.assertFalse(res_fail["all_pass"])
        self.assertEqual(len(res_fail["scorecard"]), 5)
        self.assertEqual(res_fail["sweep_evaluation"][0]["passed_slices"], 3)
        self.assertEqual(res_fail["sweep_evaluation"][0]["failed_slices"], 2)
        statuses = {row[0]: row[3] for row in res_fail["scorecard"]}
        self.assertEqual(statuses["gain_1k[R_val=2000]"], "FAIL")
        self.assertEqual(statuses["gain_1k[R_val=4000]"], "FAIL")
        self.assertEqual(statuses["gain_1k[R_val=6000]"], "PASS")

        # 3. When ALL 5 slices genuinely pass (<= -20 dB), all_pass=True
        specs_20 = [{**specs_30[0], "target": -20.0}]
        res_pass = rf_optimizer.evaluate_specs_scorecard(
            specs_20,
            lambda _b, _a: raw_2d,
            extract_sweep_fn=lambda _sw, _b, _a, _f=None: sw_payload,
        )
        self.assertTrue(res_pass["all_pass"])
        self.assertEqual(res_pass["sweep_evaluation"][0]["passed_slices"], 5)

    def test_close_sweep_values_and_manual_id_collision_never_overwrite_fail(self) -> None:
        # Reproduce 1000.001 Ohm (FAIL) vs 1000.004 Ohm (PASS) previously collapsing to 'gain[R_val=1000]'!
        freqs = [100.0, 1000.0]
        sw_close = {
            "block": "SW_BRIDGE_CTRL.AC1.AC",
            "axis": "freq",
            "sweep_axis": "R_val",
            "slices": [
                {"sweep_value": 1000.001, "axis_values": freqs, "signals": {"VOUT": [[0.9, 0.0], [0.20, 0.0]]}},  # -13.98 dB FAIL
                {"sweep_value": 1000.004, "axis_values": freqs, "signals": {"VOUT": [[0.9, 0.0], [0.10, 0.0]]}},  # -20.00 dB PASS
            ],
        }
        raw_stub = {
            "block": "SW_BRIDGE_CTRL.AC1.AC",
            "axis": "freq",
            "outer_axes": {"R_val": [1000.001, 1000.004]},
            "axis_values": freqs * 2,
            "signals": {"VOUT": [[0.9, 0.0], [0.20, 0.0], [0.9, 0.0], [0.10, 0.0]]},
        }
        specs = [{"id": "gain", "metric": "db20_at", "signal": "VOUT", "at_hz": 1000.0, "op": "<=", "target": -18.0}]
        res = rf_optimizer.evaluate_specs_scorecard(
            specs,
            lambda _b, _a: raw_stub,
            extract_sweep_fn=lambda _sw, _b, _a, _f=None: sw_close,
        )
        self.assertFalse(res["all_pass"])
        self.assertEqual(res["scorecard"][0][0], "gain[R_val=1000.001]")
        self.assertEqual(res["scorecard"][0][3], "FAIL")
        self.assertEqual(res["scorecard"][1][0], "gain[R_val=1000.004]")
        self.assertEqual(res["scorecard"][1][3], "PASS")

        # Ultra-close values identical even at 12 sig digits + manual spec ID matching auto slice ID
        sw_ultra = {
            "block": "SW_BRIDGE_CTRL.AC1.AC",
            "axis": "freq",
            "sweep_axis": "R_val",
            "slices": [
                {"sweep_value": 1000.0, "axis_values": freqs, "signals": {"VOUT": [[0.9, 0.0], [0.20, 0.0]]}},  # FAIL
                {"sweep_value": 1000.0 + 1e-13, "axis_values": freqs, "signals": {"VOUT": [[0.9, 0.0], [0.10, 0.0]]}},  # PASS
            ],
        }
        filtered_1d = {
            "block": "SW_BRIDGE_CTRL.AC1.AC",
            "axis": "freq",
            "outer_axes": {},
            "axis_values": freqs,
            "signals": {"VOUT": [[0.9, 0.0], [0.10, 0.0]]},  # PASS
        }
        specs_collide = [
            {"id": "gain", "metric": "db20_at", "signal": "VOUT", "at_hz": 1000.0, "op": "<=", "target": -18.0},
            {"id": "gain[R_val=1000]", "metric": "db20_at", "signal": "VOUT", "at_hz": 1000.0, "op": "<=", "target": -18.0, "filters": {"R_val": 1000.0}},
        ]
        res2 = rf_optimizer.evaluate_specs_scorecard(
            specs_collide,
            lambda _b, _a, flt=None: filtered_1d if flt else raw_stub,
            extract_sweep_fn=lambda _sw, _b, _a, _f=None: sw_ultra,
        )
        self.assertFalse(res2["all_pass"])
        row_ids = [r[0] for r in res2["scorecard"]]
        self.assertEqual(len(row_ids), len(set(row_ids)))  # Every row ID strictly unique!
        self.assertEqual(res2["scorecard"][0][3], "FAIL")
        self.assertEqual(res2["scorecard"][1][3], "PASS")
        self.assertEqual(res2["scorecard"][2][0], "gain[R_val=1000]")
        self.assertEqual(res2["scorecard"][2][3], "PASS")

    def test_scale_invariant_eq_tolerance_and_small_target_consistency(self) -> None:
        # Reproduce exact bug: 1 mW measured (0.001 W / 0 dBm) vs 10 mW target (0.01 W / 10 dBm)
        # Both pout_w == 0.01 AND pout_dbm == 10 MUST fail!
        v_1mw = math.sqrt(2.0 * 50.0 * 0.001)  # 0.316227766 V peak into 50 Ohm -> 0.001 W (0 dBm)
        hb_1mw = {
            "block": "HB1.HB",
            "axis": "freq",
            "axis_values": [0.0, 13.56e6, 27.12e6],
            "signals": {"VOUT": [[0.0, 0.0], [v_1mw, 0.0], [1e-4, 0.0]]},
        }
        res_w = rf_optimizer.evaluate_specs_scorecard(
            [{"id": "p_w", "metric": "pout_w", "signal": "VOUT", "load_ohm": 50.0, "op": "==", "target": 0.01}],
            lambda _b, _a: hb_1mw,
        )
        res_dbm = rf_optimizer.evaluate_specs_scorecard(
            [{"id": "p_dbm", "metric": "pout_dbm", "signal": "VOUT", "load_ohm": 50.0, "op": "==", "target": 10.0}],
            lambda _b, _a: hb_1mw,
        )
        self.assertFalse(res_w["all_pass"])
        self.assertEqual(res_w["scorecard"][0][3], "FAIL")
        self.assertFalse(res_dbm["all_pass"])
        self.assertEqual(res_dbm["scorecard"][0][3], "FAIL")

        # 9.9 mW (-1% from 10 mW) MUST pass default +/-2% tolerance; 9.5 mW (-5%) MUST fail
        v_9p9mw = math.sqrt(2.0 * 50.0 * 0.0099)
        v_9p5mw = math.sqrt(2.0 * 50.0 * 0.0095)
        hb_9p9 = {**hb_1mw, "signals": {"VOUT": [[0.0, 0.0], [v_9p9mw, 0.0], [1e-4, 0.0]]}}
        hb_9p5 = {**hb_1mw, "signals": {"VOUT": [[0.0, 0.0], [v_9p5mw, 0.0], [1e-4, 0.0]]}}
        self.assertTrue(rf_optimizer.evaluate_specs_scorecard(
            [{"id": "p_w", "metric": "pout_w", "signal": "VOUT", "op": "==", "target": 0.01}],
            lambda _b, _a: hb_9p9,
        )["all_pass"])
        self.assertFalse(rf_optimizer.evaluate_specs_scorecard(
            [{"id": "p_w", "metric": "pout_w", "signal": "VOUT", "op": "==", "target": 0.01}],
            lambda _b, _a: hb_9p5,
        )["all_pass"])
        # Explicit tolerance=0.001 (1 mW) allows 9.5 mW for 10 mW target
        self.assertTrue(rf_optimizer.evaluate_specs_scorecard(
            [{"id": "p_w", "metric": "pout_w", "signal": "VOUT", "op": "==", "target": 0.01, "tolerance": 0.001}],
            lambda _b, _a: hb_9p5,
        )["all_pass"])

        # Tiny-target le/ge edge case: 0.0 W MUST NOT pass pout_w >= 1e-9 W!
        objs_nw = rf_optimizer._validate_objectives([
            {"id": "p_nw", "metric": "pout_w", "signal": "VOUT", "op": ">=", "target": 1e-9},
        ])
        _, det_nw, sat_nw = rf_optimizer._evaluate_cost({"p_nw": 0.0}, objs_nw)
        self.assertFalse(sat_nw)
        self.assertFalse(det_nw["p_nw"]["satisfied"])

        # Linear zero target: 15 mW MUST NOT pass pout_w == 0.0!
        objs_z = rf_optimizer._validate_objectives([
            {"id": "p_zero", "metric": "pout_w", "signal": "VOUT", "op": "==", "target": 0.0},
        ])
        _, det_z, sat_z = rf_optimizer._evaluate_cost({"p_zero": 0.015}, objs_z)
        self.assertFalse(sat_z)

        # Non-finite value in min/max MUST fail
        objs_min = rf_optimizer._validate_objectives([
            {"id": "m1", "metric": "ripple_db", "signal": "VOUT", "op": "min"},
        ])
        _, _, sat_nan = rf_optimizer._evaluate_cost({"m1": math.nan}, objs_min)
        self.assertFalse(sat_nan)

        # Upfront validation rejects norm <= 0, tolerance < 0, load_ohm <= 0, non-finite target
        for bad in (
            {"id": "b1", "metric": "pout_w", "signal": "VOUT", "op": "==", "target": 0.01, "norm": 0.0},
            {"id": "b2", "metric": "pout_w", "signal": "VOUT", "op": "==", "target": 0.01, "norm": -1.0},
            {"id": "b3", "metric": "pout_w", "signal": "VOUT", "op": "==", "target": 0.01, "tolerance": -0.001},
            {"id": "b4", "metric": "pout_w", "signal": "VOUT", "op": "==", "target": 0.01, "load_ohm": 0.0},
            {"id": "b5", "metric": "pout_w", "signal": "VOUT", "op": "==", "target": math.nan},
        ):
            with self.assertRaises(ValueError):
                rf_optimizer._validate_objectives([bad])


if __name__ == "__main__":
    unittest.main()
