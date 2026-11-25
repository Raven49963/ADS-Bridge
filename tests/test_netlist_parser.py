"""Unit tests for ADS netlist semantic parser, topology analyzer, ERC checker, and RC reviewer."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from netlist_parser import NetlistError, check_netlist, inspect_netlist, review_rc


class TestNetlistParserAndReview(unittest.TestCase):
    def test_standard_ads_lowpass_netlist(self) -> None:
        netlist = """
        ; Keysight ADS generated netlist
        Options ResourceUsage=yes UseNutmegFormat=no TopDesignName="lib:rc:schematic"
        V_Source:SRC1 N1 0 Type="V_AC" Vac=polar(1,0) V
        R:R1 N1 OUT R=3.0 kOhm Noise=yes
        C:C1 OUT 0 C=1.0 uF
        AC:AC1 Start=1 Hz Stop=1 MHz Dec=5
        """
        result = review_rc(netlist)
        self.assertEqual(result["input_net"], "N1")
        self.assertEqual(result["output_net"], "OUT")
        self.assertAlmostEqual(result["resistance_ohm"], 3000.0, places=9)
        self.assertAlmostEqual(result["capacitance_f"], 1e-6, places=15)
        expected_fc = 1.0 / (2.0 * math.pi * 3000.0 * 1e-6)
        self.assertAlmostEqual(result["cutoff_hz"], expected_fc, places=9)
        parsed = json.loads(json.dumps(result))
        self.assertEqual(
            set(parsed.keys()),
            {"input_net", "output_net", "resistance_ohm", "capacitance_f", "cutoff_hz"},
        )

    def test_renamed_instances_nets_and_pin_orders(self) -> None:
        netlist = """
        AC:AC_SWEEP Start=1 Hz Stop=10 MHz
        C:CAP_LOAD 0 V_LPF C=220 pF
        R:R_SERIES V_LPF VIN_SIG R=4.7kOhm
        V_Source:V_IN 0 VIN_SIG Type="V_AC" Vac=polar(2,45) V
        """
        result = review_rc(netlist)
        self.assertEqual(result["input_net"], "VIN_SIG")
        self.assertEqual(result["output_net"], "V_LPF")
        self.assertAlmostEqual(result["resistance_ohm"], 4700.0, places=9)
        self.assertAlmostEqual(result["capacitance_f"], 220e-12, places=20)

    def test_rejects_highpass_and_bad_rc_variants_in_strict_review(self) -> None:
        highpass = """
        V_Source:SRC1 N1 0 Type="V_AC" Vac=polar(1,0) V
        C:C1 N1 OUT C=1.0 uF
        R:R1 OUT 0 R=3.0 kOhm
        AC:AC1 CalcNoise=no
        """
        with self.assertRaises(NetlistError):
            review_rc(highpass)

        expr_rc = """
        V_Source:SRC1 N1 0 Type="V_AC" Vac=polar(1,0) V
        R:R1 N1 OUT R=R_var
        C:C1 OUT 0 C=1.0 uF
        AC:AC1 CalcNoise=no
        """
        with self.assertRaises(NetlistError):
            review_rc(expr_rc)

    def test_inspect_real_ads_netlist_with_vars_subcircuits_and_insights(self) -> None:
        netlist = """
        ; Top Design: "rf_lib:rc_var:schematic"
        ; Netlisted using Hierarchy Policy: "Standard"
        Options ResourceUsage=yes TopDesignName="rf_lib:rc_var:schematic"

        define pad_cell ( p_in p_out )
        parameters Rpad=10 Ohm
        R:RP1 p_in p_out R=Rpad
        end pad_cell

        R0=3 kOhm
        R_eff=2*R0
        C_load=1 uF

        V_Source:SRC1 N_IN 0 Type="V_AC" Vac=polar(1,0) V
        R:R1 N_IN VOUT R=R_eff Noise=yes
        C:C1 VOUT 0 C=C_load
        AC:AC1 SweepVar="freq" SweepPlan="AC1_stim" OutputPlan="AC1_out"
        SweepPlan: AC1_stim Start=1 Hz Stop=1 MHz Dec=5
        OutputPlan: AC1_out Type="Output" UseEquationNestLevel=yes
        """
        ast_data = inspect_netlist(netlist)
        self.assertEqual(ast_data["metadata"]["top_design"], "rf_lib:rc_var:schematic")
        self.assertAlmostEqual(ast_data["variables"]["R_eff"]["si_value"], 6000.0, places=6)
        self.assertEqual(len(ast_data["subcircuits"]), 1)
        self.assertEqual(ast_data["subcircuits"][0]["name"], "pad_cell")
        self.assertGreaterEqual(len(ast_data["analyses"]), 1)
        sweep_plans = [a for a in ast_data["analyses"] if a["type"] == "SweepPlan"]
        self.assertAlmostEqual(sweep_plans[0]["start_si"], 1.0)
        self.assertAlmostEqual(sweep_plans[0]["stop_si"], 1e6)
        self.assertEqual(ast_data["circuit_insights"][0]["archetype"], "rc_lowpass_1st_order")
        self.assertAlmostEqual(
            ast_data["circuit_insights"][0]["cutoff_hz"],
            1.0 / (2.0 * math.pi * 6000.0 * 1e-6),
            places=6,
        )
        self.assertIn("rc_lowpass_1st_order", ast_data["ai_summary"])

    def test_inspect_two_port_sparam_netlist(self) -> None:
        netlist = """
        Options ResourceUsage=yes
        Component Module="LinearCollapse" Type="ModelExtractor" NetworkRepresentation=2
        Port:Term1 PORT1 0 Num=1 Z=50 Ohm
        Port:Term2 PORT2 0 Num=2 Z=50 Ohm
        R:R1 PORT1 PORT2 R=50 Ohm
        S_Param:SP1 SweepVar="freq" SweepPlan="SP1_stim"
        SweepPlan: SP1_stim Start=100 MHz Stop=1 GHz Step=100 MHz
        """
        ast_data = inspect_netlist(netlist)
        insight = ast_data["circuit_insights"][0]
        self.assertEqual(insight["archetype"], "two_port_series_resistor")
        self.assertAlmostEqual(insight["s11_expected"], 1.0 / 3.0, places=9)
        self.assertAlmostEqual(insight["s21_expected"], 2.0 / 3.0, places=9)

    def test_erc_checker_catches_shorted_floating_and_missing_includes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad_tb = """
            #include "missing_models.ckt"
            R:R_SHORT N1 N1 R=50 Ohm
            R:R_FLOAT N1 DANGLING R=0 Ohm
            C:C_UNDEF N1 0 C=UNKNOWN_CAP
            """
            report = check_netlist(bad_tb, mode="tb", search_dirs=[tmp])
            self.assertFalse(report["ok"])
            codes = {d["code"] for d in report["diagnostics"]}
            self.assertIn("missing_include", codes)
            self.assertIn("shorted_instance", codes)
            self.assertIn("non_positive_parameter", codes)
            self.assertIn("unresolved_parameter", codes)
            self.assertIn("floating_net", codes)
            self.assertIn("missing_analysis", codes)

    def test_erc_checker_dut_vs_tb_modes(self) -> None:
        dut_netlist = """
        define lna_core ( rf_in rf_out vdd gnd )
        R:R_BIAS vdd rf_out R=1 kOhm
        end lna_core
        """
        dut_report = check_netlist(dut_netlist, mode="dut")
        self.assertTrue(dut_report["ok"])
        tb_report = check_netlist(dut_netlist, mode="tb")
        self.assertFalse(tb_report["ok"])

    def test_testbench_requires_an_actual_solver_analysis(self) -> None:
        netlist = """
        V_Source:V IN 0 Type="V_AC" Vac=polar(1,0) V
        R:R1 IN 0 R=50 Ohm
        SweepPlan: P Start=1 Hz Stop=10 Hz Step=1 Hz
        OutputPlan: O Type="Output"
        ParamSweep: SW SweepVar="r" SimInstanceName[1]="AC1"
        """
        report = check_netlist(netlist, mode="tb")
        self.assertFalse(report["ok"])
        self.assertIn("missing_analysis", {d["code"] for d in report["diagnostics"]})
        self.assertIn("SweepPlan", {a["type"] for a in report["parsed"]["analyses"]})
        self.assertTrue(check_netlist(netlist + '\nAC:AC1 SweepVar="freq" SweepPlan="P"\n', mode="tb")["ok"])

    def test_dut_references_resolve_in_local_and_caller_scopes(self) -> None:
        netlist = """
        Rroot=4 Ohm
        define outer ( a b )
        parameters Rcaller
        inner:U a b Rport=Rroot
        end outer
        define inner ( a b )
        parameters Rport
        Rlocal=Rcaller+Rport
        R:R1 a b R=Rlocal
        end inner
        define unrelated ( a b )
        R:R1 a b R=1 Ohm
        end unrelated
        """
        self.assertTrue(check_netlist(netlist, mode="dut")["ok"])

        for replacement in ["NO_SUCH_VAR", "Rcaller"]:
            report = check_netlist(netlist.replace("R=1 Ohm", f"R={replacement}"), mode="dut")
            self.assertFalse(report["ok"])
            diagnostic = next(d for d in report["diagnostics"] if d["code"] == "unresolved_parameter")
            self.assertEqual(diagnostic["target"], "unrelated.R1.R")
            self.assertIn(replacement, diagnostic["message"])
            self.assertGreater(diagnostic["line"], 0)

        broken_assignment = netlist.replace("Rcaller+Rport", "TYPO+Rport")
        report = check_netlist(broken_assignment, mode="dut")
        self.assertEqual([d["code"] for d in report["diagnostics"]], ["unresolved_variable"])

    def test_parameters_statement_with_unit_defaults_and_bare_names(self) -> None:
        netlist = """
        R0=5 kOhm
        define my_cell ( a b )
        parameters R1=10 Ohm R2 C1=1.5 pF Scale=2*R0 kOhm BareTail
        R:Ra a b R=R1+R2
        C:Ca b 0 C=C1
        end my_cell
        """
        parsed = inspect_netlist(netlist)
        params = parsed["subcircuits"][0]["parameters"]
        self.assertEqual(
            params,
            {"R1": "10 Ohm", "R2": "", "C1": "1.5 pF", "Scale": "2*R0 kOhm", "BareTail": ""},
        )
        report = check_netlist(netlist, mode="dut")
        self.assertTrue(report["ok"], str(report["diagnostics"]))

    def test_erc_checks_top_level_subcircuit_calls_and_subcircuit_internals(self) -> None:
        bad_top_call = """
        define pad_cell ( p1 p2 )
        parameters Rpad=50 Ohm
        R:R1 p1 p2 R=Rpad
        end pad_cell
        Port:P1 in 0 Num=1 Z=50 Ohm
        pad_cell:X1 in 0 Rpad=NO_SUCH_VAR
        S_Param:SP1 Start=1 GHz Stop=2 GHz Step=1 GHz
        """
        top_report = check_netlist(bad_top_call, mode="tb")
        self.assertFalse(top_report["ok"])
        self.assertTrue(
            any(d["code"] == "unresolved_parameter" and d["target"] == "X1.Rpad" for d in top_report["diagnostics"])
        )

        bad_sub_body = """
        define dut_bad ( p1 p2 )
        R:R1 p1 p1 R=-50 Ohm
        R:R1 p1 p2 R=0 Ohm
        end dut_bad
        """
        sub_report = check_netlist(bad_sub_body, mode="dut")
        self.assertFalse(sub_report["ok"])
        sub_codes = {d["code"] for d in sub_report["diagnostics"]}
        self.assertIn("duplicate_instance", sub_codes)
        self.assertIn("shorted_instance", sub_codes)
        self.assertIn("non_positive_parameter", sub_codes)

    def test_erc_consistent_include_handling_and_active_caller_intersection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inc_file = Path(tmp) / "params.ckt"
            inc_file.write_text("EXT_R=75 Ohm\n", encoding="utf-8")
            with_resolved_inc = """
            #include "params.ckt"
            Port:P1 in 0 Num=1 Z=50 Ohm
            R:R1 in 0 R=EXT_R
            S_Param:SP1 Start=1 GHz Stop=2 GHz Step=1 GHz
            """
            resolved_rep = check_netlist(with_resolved_inc, mode="tb", search_dirs=[tmp])
            self.assertTrue(resolved_rep["ok"])
            self.assertEqual(resolved_rep["warning_count"], 0)

        # Without search_dirs, both top-level and subcircuit external #include symbols downgrade consistently to warning
        unexpanded_top = """
        #include "external_defs.ckt"
        Port:P1 in 0 Num=1 Z=50 Ohm
        R:R1 in 0 R=EXT_R
        S_Param:SP1 Start=1 GHz Stop=2 GHz Step=1 GHz
        """
        unexpanded_rep = check_netlist(unexpanded_top, mode="tb")
        self.assertTrue(unexpanded_rep["ok"])
        self.assertEqual([(d["severity"], d["code"]) for d in unexpanded_rep["diagnostics"]],
                         [("warning", "unresolved_parameter")])

        # Uncalled or partial caller must not mask missing symbol in an active call path
        partial_caller_tb = """
        define caller_with_var ( a )
        Rsh=50 Ohm
        leaf:X1 a
        end caller_with_var

        define caller_without_var ( a )
        leaf:X1 a
        end caller_without_var

        define leaf ( p )
        R:R1 p 0 R=Rsh
        end leaf

        Port:P1 in 0 Num=1 Z=50 Ohm
        caller_without_var:Xtop in
        S_Param:SP1 Start=1 GHz Stop=2 GHz Step=1 GHz
        """
        partial_rep = check_netlist(partial_caller_tb, mode="tb")
        self.assertFalse(partial_rep["ok"])
        self.assertTrue(
            any(d["code"] == "unresolved_parameter" and d["target"] == "leaf.R1.R" for d in partial_rep["diagnostics"])
        )

    def test_two_port_sparam_maps_ports_by_num_and_rejects_duplicate_num(self) -> None:
        swapped = """
        Port:P_OUT out_net 0 Num=2 Z=50 Ohm
        Port:P_IN in_net 0 Num=1 Z=50 Ohm
        R:R1 in_net out_net R=50 Ohm
        S_Param:SP1 Start=1 GHz Stop=2 GHz Step=1 GHz
        """
        parsed_swapped = inspect_netlist(swapped)
        insight = parsed_swapped["circuit_insights"][0]
        self.assertEqual(insight["port1_net"], "in_net")
        self.assertEqual(insight["port2_net"], "out_net")

        dup_num = """
        Port:P1 in_net 0 Num=1 Z=50 Ohm
        Port:P2 out_net 0 Num=1 Z=50 Ohm
        R:R1 in_net out_net R=50 Ohm
        S_Param:SP1 Start=1 GHz Stop=2 GHz Step=1 GHz
        """
        parsed_dup = inspect_netlist(dup_num)
        self.assertEqual(parsed_dup["circuit_insights"], [])
        dup_report = check_netlist(dup_num, mode="tb")
        self.assertFalse(dup_report["ok"])
        self.assertIn("duplicate_port_number", {d["code"] for d in dup_report["diagnostics"]})


    def test_sdd_requires_a_nonempty_equation_for_each_port(self) -> None:
        valid = "SDD:AMP A 0 B 0 C 0 I[1,0]=_v1/130 F[2,0]=_v2-_v1 I[3,0]=0\n"
        self.assertFalse(any(d["code"] == "invalid_sdd_equations" for d in check_netlist(valid, mode="dut")["diagnostics"]))
        for expression in ("", "I[4,0]=0"):
            faulty = valid.replace("I[3,0]=0", expression)
            result = check_netlist(faulty, mode="dut")
            diagnostic = next(d for d in result["diagnostics"] if d["code"] == "invalid_sdd_equations")
            self.assertFalse(result["ok"])
            self.assertEqual(diagnostic["target"], "AMP")
            self.assertIn("port(s) [3]", diagnostic["message"])
        nested = "define amp ( A B C )\n" + valid.replace(" F[2,0]=_v2-_v1", "") + "end amp\n"
        result = check_netlist(nested, mode="dut")
        self.assertTrue(any(d["code"] == "invalid_sdd_equations" and d["target"] == "amp.AMP" for d in result["diagnostics"]))


if __name__ == "__main__":
    unittest.main()
