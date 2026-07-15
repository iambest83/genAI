"""Tests for the mainframe-modernization MCP tools (Iteration 1.6).

Runs two ways:
  - `pytest` (if installed) collects the test_* functions.
  - `python tests/test_mcp_tools.py` runs them standalone (no pytest needed),
    which is how they're exercised in the local .venv that ships only fastmcp.

Run from the repo root so both `mcp_server` and `agent` import cleanly:
    mcp_server/.venv/bin/python mcp_server/tests/test_mcp_tools.py
"""
import json
import os
import sys
import itertools

# Allow running from anywhere: put the repo root (parent of mcp_server) on path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from mcp_server import server  # noqa: E402
from mcp_server import _helpers as h  # noqa: E402


def _parse(out: str) -> dict:
    """Pull the JSON payload out of a structured_response string."""
    return json.loads(out.split("```json")[1].split("```")[0])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_stable_hash_is_order_independent():
    assert h.stable_hash({"x": 1, "y": [2, 3]}) == h.stable_hash({"y": [2, 3], "x": 1})


def test_confidence_bands():
    assert h.computation_result(value=1, factors=[], inputs_echo={"a": 1},
                                missing_inputs=[], data_version="v")["confidence"] == "high"
    assert h.computation_result(value=1, factors=[], inputs_echo={"a": 1, "b": 2},
                                missing_inputs=["c"], data_version="v")["confidence"] == "medium"
    assert h.computation_result(value=1, factors=[], inputs_echo={"a": 1},
                                missing_inputs=["b", "c"], data_version="v")["confidence"] == "low"


def test_inputs_source_detection():
    measured = {"a": {"value": 1, "source": "measured"}}
    mixed = {"a": {"value": 1, "source": "measured"}, "b": {"value": 2, "source": "estimated"}}
    assert h.computation_result(value=1, factors=[], inputs_echo=measured,
                                missing_inputs=[], data_version="v")["inputs_source"] == "measured"
    assert h.computation_result(value=1, factors=[], inputs_echo=mixed,
                                missing_inputs=[], data_version="v")["inputs_source"] == "mixed"


def test_source_tokens_from_workload():
    wl = {"num_cobol_programs": 100, "has_cics": True, "has_db2": True,
          "num_vsam_files": 10, "databases": ["IMS-DB"]}
    toks = h.source_tokens_from_workload(wl)
    assert {"COBOL", "CICS", "DB2", "VSAM"} <= set(toks)


# ---------------------------------------------------------------------------
# estimate_complexity
# ---------------------------------------------------------------------------

def test_estimate_complexity_deterministic_and_hashed():
    a = _parse(server.estimate_complexity(num_cobol_programs=1500, num_jcl_jobs=600,
                                          has_cics=True, has_db2=True))
    b = _parse(server.estimate_complexity(num_cobol_programs=1500, num_jcl_jobs=600,
                                          has_cics=True, has_db2=True))
    assert a["value"] == b["value"]
    assert a["breakdown_hash"] == b["breakdown_hash"]
    assert a["confidence"] == "high"


def test_estimate_complexity_profile_seam_matches_explicit():
    explicit = _parse(server.estimate_complexity(num_cobol_programs=1500, num_jcl_jobs=600,
                                                 has_cics=True, has_db2=True))
    prof = {"workload": {"num_cobol_programs": 1500, "num_jcl_jobs": 600,
                         "has_cics": True, "has_db2": True}}
    seeded = _parse(server.estimate_complexity(profile=prof))
    assert seeded["value"] == explicit["value"]


def test_estimate_complexity_explicit_overrides_profile():
    prof = {"workload": {"num_cobol_programs": 1500, "num_jcl_jobs": 600}}
    p = _parse(server.estimate_complexity(num_cobol_programs=10, profile=prof))
    assert p["inputs_echo"]["num_cobol_programs"] == 10


def test_estimate_complexity_missing_inputs_lower_confidence():
    p = _parse(server.estimate_complexity(has_cics=True))
    assert "num_cobol_programs" in p["missing_inputs"]
    assert "num_jcl_jobs" in p["missing_inputs"]
    assert p["confidence"] in ("low", "medium")


def test_estimate_complexity_has_provenance_footer():
    out = server.estimate_complexity(num_cobol_programs=100, num_jcl_jobs=50)
    assert "Last reviewed" in out


# ---------------------------------------------------------------------------
# compare_partner_tools
# ---------------------------------------------------------------------------

def test_partner_empty_input_is_low_confidence_not_perfect():
    p = _parse(server.compare_partner_tools())
    assert p["low_confidence"] is True
    scores = [r["score"] for r in p["ranked"]]
    assert not all(s == 1.0 for s in scores), "empty input must not give every vendor a perfect score"


def test_partner_coverage_ranks_and_explains():
    p = _parse(server.compare_partner_tools(source_systems=["COBOL", "CICS", "JCL", "VSAM"],
                                            pattern="rehost"))
    assert not p["low_confidence"]
    assert "source_system_coverage" in p["ranked"][0]["criterion_scores"]


def test_partner_profile_seeds_sources():
    prof = {"workload": {"num_cobol_programs": 1500, "has_cics": True, "num_vsam_files": 200}}
    p = _parse(server.compare_partner_tools(profile=prof))
    assert p["filters"]["source_systems_origin"] == "profile"
    assert not p["low_confidence"]


def test_partner_deterministic():
    a = _parse(server.compare_partner_tools(source_systems=["COBOL", "CICS"]))
    b = _parse(server.compare_partner_tools(source_systems=["COBOL", "CICS"]))
    assert [r["tool_key"] for r in a["ranked"]] == [r["tool_key"] for r in b["ranked"]]


def test_partner_weight_profiles_differ():
    cost = _parse(server.compare_partner_tools(source_systems=["COBOL", "CICS", "JCL", "VSAM"],
                                               weight_profile="cost_sensitive"))
    reg = _parse(server.compare_partner_tools(source_systems=["COBOL", "CICS", "JCL", "VSAM"],
                                              weight_profile="regulator_first"))
    assert cost["weight_profile"] == "cost_sensitive"
    assert reg["weight_profile"] == "regulator_first"


# ---------------------------------------------------------------------------
# compare_services
# ---------------------------------------------------------------------------

def test_services_vsam_ksds_top_is_dynamodb():
    p = _parse(server.compare_services(source="vsam_ksds"))
    assert p["candidates"][0]["service"] == "DynamoDB"
    assert "fit" in p["candidates"][0]["criterion_scores"]


def test_services_no_magic_fit_rank_in_source():
    import inspect
    assert "fit_rank = {" not in inspect.getsource(server.compare_services)


def test_services_profile_zero_downtime_sets_latency_need():
    p = _parse(server.compare_services(source="db2_zos",
                                       profile={"constraints": {"downtime_tolerance": "zero"}}))
    assert p["low_latency_need"] is True


def test_services_candidate_filter():
    p = _parse(server.compare_services(source="vsam_ksds", candidates=["DynamoDB", "Keyspaces"]))
    assert {c["service"] for c in p["candidates"]} == {"DynamoDB", "Keyspaces"}


# ---------------------------------------------------------------------------
# get_fsi_compliance_check
# ---------------------------------------------------------------------------

def test_compliance_lists_all_seven_regulations():
    p = _parse(server.get_fsi_compliance_check())
    assert set(p["available_regulations"]) == {
        "SOX", "PCI_DSS", "FDIC", "OCC", "GLBA", "FFIEC", "FINRA_17a-4"}


def test_compliance_has_disclaimer():
    out = server.get_fsi_compliance_check(regulation="SOX")
    assert "not legal" in out.lower()


def test_compliance_controls_are_typed():
    p = _parse(server.get_fsi_compliance_check(regulation="SOX"))
    c0 = p["details"]["controls"][0]
    for k in ("control_id", "regulation_clause", "intent", "satisfied_by", "evidence_required"):
        assert k in c0


def test_compliance_finra_worm_control():
    p = _parse(server.get_fsi_compliance_check(regulation="FINRA_17a-4"))
    worm = [c for c in p["details"]["controls"] if "WORM" in c["control_id"]]
    assert worm and "Object Lock" in str(worm[0]["satisfied_by"])


# ---------------------------------------------------------------------------
# analyze_phase_gaps
# ---------------------------------------------------------------------------

def test_phase_gaps_empty_is_discovery():
    p = _parse(server.analyze_phase_gaps(profile={"workload": {}, "constraints": {}, "decisions_made": []}))
    assert p["current_phase"] == "discovery"
    assert p["gap_questions"][0]["signal"] == "workload_known"


def test_phase_gaps_target_phase_union():
    p = _parse(server.analyze_phase_gaps(
        profile={"workload": {}, "constraints": {}, "decisions_made": []},
        target_phase="proposal"))
    sigs = [g["signal"] for g in p["gap_questions"]]
    assert "workload_known" in sigs and "has_pattern" in sigs


def test_phase_gaps_parity_with_derive_phase():
    """The whole point of the data-driven gate logic: it must reproduce
    CustomerProfile.derive_phase() exactly. Fuzz a broad cross-product."""
    from agent.customer_profile import CustomerProfile

    wl_opts = [{}, {"num_cobol_programs": 100}, {"has_cics": True}, {"has_db2": True},
               {"has_ims": True}, {"languages": ["COBOL"]}, {"databases": ["DB2"]}, {"num_jcl_jobs": 5}]
    cn_opts = [{}, {"regulations": ["SOX"]}, {"target_date": "2027"},
               {"budget_band": "x"}, {"downtime_tolerance": "zero"}]
    dec_opts = [[], [("pattern", "p")], [("partner_tool", "x")], [("target_service", "t")],
                [("pattern", "p"), ("partner_tool", "x")],
                [("pattern", "p"), ("target_service", "t")],
                [("pattern", "p"), ("partner_tool", "x"), ("target_service", "t")],
                [("a", "1"), ("b", "2"), ("c", "3")],
                [("pattern", "p"), ("a", "1"), ("b", "2")]]

    mismatches = []
    for wl, cn, dec in itertools.product(wl_opts, cn_opts, dec_opts):
        p = CustomerProfile(sa_id="s", customer_id="c")
        for k, v in wl.items():
            setattr(p.workload, k, v)
        for k, v in cn.items():
            setattr(p.constraints, k, v)
        for cat, val in dec:
            p.add_decision(cat, val, "", 1)
        expected = p.derive_phase()
        got = _parse(server.analyze_phase_gaps(profile=p.to_dict()))["current_phase"]
        if expected != got:
            mismatches.append((expected, got, wl, cn, dec))
    assert not mismatches, f"phase parity broke: {mismatches[:5]}"


# ---------------------------------------------------------------------------
# Tool-surface invariants
# ---------------------------------------------------------------------------

def test_tool_count_is_ten():
    import inspect
    n = len([name for name, obj in inspect.getmembers(server, inspect.isfunction)
             if getattr(obj, "__module__", "") == server.__name__
             and not name.startswith("_")
             and name in {
                 "lookup_cobol_pattern", "lookup_jcl_reference", "map_mainframe_to_aws",
                 "get_migration_pattern", "estimate_complexity", "get_fsi_compliance_check",
                 "compare_partner_tools", "compare_services", "analyze_phase_gaps",
                 "list_taxonomy",
             }])
    assert n == 10, f"expected 10 tools, found {n}"


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

def _run_all():
    fns = [(name, obj) for name, obj in sorted(globals().items())
           if name.startswith("test_") and callable(obj)]
    passed = 0
    failed = []
    for name, fn in fns:
        try:
            fn()
            passed += 1
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed.append((name, e))
            print(f"FAIL {name}: {e}")
    print(f"\n{passed}/{len(fns)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(_run_all())
