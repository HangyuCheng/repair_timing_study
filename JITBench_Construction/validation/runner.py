from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from ..bug_reconstruction.java import apply_state_patch
from ..collect.collection import project_map
from ..collect.git import GitRepo
from ...evaluation.benchmark import load_benchmark
from ...io import read_json, read_yaml, write_json
from .environment import (
    archive_snapshot,
    build_process_environment,
    copy_triggering_tests,
    prepare_state_workspaces,
    validate_environment_spec,
    validate_workspace_layout,
)


def _run_command(command: list[str], cwd: Path, env: dict[str, str], timeout: int, log_path: Path) -> dict[str, Any]:
    timed_out = False
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        output = proc.stdout or ""
        returncode: int | None = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        returncode = None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    return {
        "command": command,
        "returncode": returncode,
        "passed": returncode == 0 and not timed_out,
        "timed_out": timed_out,
        "skipped": False,
        "log": str(log_path),
    }


def _skipped_command() -> dict[str, Any]:
    return {"passed": False, "skipped": True, "timed_out": False, "returncode": None}


def _trigger_failure_contract(spec: dict[str, Any], state: str) -> dict[str, Any]:
    contracts = spec.get("expected_trigger_failures")
    if not isinstance(contracts, dict) or not isinstance(contracts.get(state), dict):
        raise ValueError(f"missing expected_trigger_failures.{state}")
    contract = contracts[state]
    if not str(contract.get("failure_reason", "")).strip():
        raise ValueError(f"expected_trigger_failures.{state}.failure_reason is required")
    exit_codes = contract.get("expected_exit_codes")
    if not isinstance(exit_codes, list) or not exit_codes or any(not isinstance(value, int) or value == 0 for value in exit_codes):
        raise ValueError(f"expected_trigger_failures.{state}.expected_exit_codes must be a nonempty list of nonzero integers")
    patterns = contract.get("required_patterns")
    if not isinstance(patterns, list) or not patterns or any(not str(value).strip() for value in patterns):
        raise ValueError(f"expected_trigger_failures.{state}.required_patterns must be a nonempty list")
    if contract.get("match", "all") not in {"all", "any"}:
        raise ValueError(f"expected_trigger_failures.{state}.match must be all or any")
    for key in ("required_patterns", "forbidden_patterns"):
        for pattern in contract.get(key, []):
            try:
                re.compile(str(pattern))
            except re.error as exc:
                raise ValueError(f"expected_trigger_failures.{state}.{key} contains invalid regex {pattern!r}: {exc}") from exc
    return contract


def verify_trigger_failure(trigger_result: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Validate a trigger failure against its state-specific contract."""
    log_path = trigger_result.get("log")
    log_text = Path(log_path).read_text(encoding="utf-8", errors="replace") if log_path and Path(log_path).exists() else ""
    expected_exit_codes = [int(value) for value in contract["expected_exit_codes"]]
    required = {str(pattern): bool(re.search(str(pattern), log_text, flags=re.MULTILINE)) for pattern in contract["required_patterns"]}
    forbidden = {str(pattern): bool(re.search(str(pattern), log_text, flags=re.MULTILINE)) for pattern in contract.get("forbidden_patterns", [])}
    match_mode = contract.get("match", "all")
    required_ok = all(required.values()) if match_mode == "all" else any(required.values())
    rejection_reasons = []
    if trigger_result.get("skipped"):
        rejection_reasons.append("trigger command was skipped")
    if trigger_result.get("timed_out"):
        rejection_reasons.append("trigger command timed out")
    if trigger_result.get("passed"):
        rejection_reasons.append("triggering test unexpectedly passed")
    if trigger_result.get("returncode") not in expected_exit_codes:
        rejection_reasons.append(f"unexpected exit code: {trigger_result.get('returncode')}")
    if not required_ok:
        rejection_reasons.append(f"required log patterns did not satisfy match={match_mode}")
    if any(forbidden.values()):
        rejection_reasons.append("forbidden log pattern matched")
    return {
        "verified": not rejection_reasons,
        "failure_reason": str(contract["failure_reason"]),
        "expected_exit_codes": expected_exit_codes,
        "actual_exit_code": trigger_result.get("returncode"),
        "required_pattern_matches": required,
        "forbidden_pattern_matches": forbidden,
        "rejection_reasons": rejection_reasons,
    }


def _execute_checks(
    workspace: Path,
    spec: dict[str, Any],
    env: dict[str, str],
    timeout: int,
    log_dir: Path,
    expected_trigger_failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    compile_result = _run_command(spec["compile_command"], workspace, env, timeout, log_dir / "compile.log")
    test_compile_result = _run_command(spec["test_compile_command"], workspace, env, timeout, log_dir / "test-compile.log") if compile_result["passed"] else _skipped_command()
    compilation_passed = compile_result["passed"] and test_compile_result["passed"]
    trigger_result = _run_command(spec["trigger_command"], workspace, env, timeout, log_dir / "trigger.log") if compilation_passed else _skipped_command()
    regression_result = _run_command(spec["regression_command"], workspace, env, timeout, log_dir / "regression.log") if compilation_passed else _skipped_command()
    failure_evidence = verify_trigger_failure(trigger_result, expected_trigger_failure) if expected_trigger_failure is not None else None
    return {
        "compile_passed": compilation_passed,
        "production_compile_passed": compile_result["passed"],
        "test_compile_passed": test_compile_result["passed"],
        "triggering_tests_passed": trigger_result["passed"],
        "trigger_failure_verified": failure_evidence["verified"] if failure_evidence is not None else None,
        "trigger_failure_evidence": failure_evidence,
        "regression_tests_passed": regression_result["passed"],
        "commands": {"compile": compile_result, "test_compile": test_compile_result, "trigger": trigger_result, "regression": regression_result},
    }


def _patch_workspace(workspace: Path, state: dict[str, Any], patch: str) -> None:
    source_path = workspace / state["source_path"]
    source = source_path.read_text(encoding="utf-8")
    source_path.write_text(apply_state_patch(source, state, patch), encoding="utf-8")


def run_validation(
    projects_path: Path,
    benchmark_path: Path,
    manifest_path: Path,
    work_root: Path,
    out_path: Path,
) -> dict[str, Any]:
    projects, _tools = project_map(projects_path)
    benchmark = load_benchmark(benchmark_path)
    manifest = read_yaml(manifest_path)
    defaults = manifest.get("defaults", {})
    bug_specs = {str(key): value for key, value in manifest.get("bugs", {}).items()}
    results = []
    for record in benchmark["records"]:
        bug_id = str(record["bug_id"])
        if bug_id not in bug_specs:
            raise ValueError(f"bug {bug_id}: missing validation manifest entry")
        spec = bug_specs[bug_id]
        repo = GitRepo(Path(projects[record["project"]]["local_repo"]))
        project_spec = manifest.get("projects", {}).get(record["project"], {})
        jdk_home = spec.get("jdk_home") or project_spec.get("jdk_home")
        validate_environment_spec(spec, manifest_path.parent, jdk_home)
        env = build_process_environment(str(jdk_home))
        timeout = int(spec.get("timeout_seconds", defaults.get("timeout_seconds", 1800)))
        state_results = {}
        for state_name in ("jit", "delay"):
            state = record["states"][state_name]
            failure_contract = _trigger_failure_contract(spec, state_name)
            base = work_root / f"bug-{bug_id}" / state_name
            before_dir, after_dir = prepare_state_workspaces(repo, state, spec, state_name, manifest_path.parent, base)
            _patch_workspace(after_dir, state, state["reference_patch"])
            state_results[state_name] = {
                "commit": state["commit"],
                "before_patch": _execute_checks(before_dir, spec, env, timeout, base / "logs-before", failure_contract),
                "after_patch": _execute_checks(after_dir, spec, env, timeout, base / "logs-after"),
            }
        results.append({
            "bug_id": bug_id,
            "project": record["project"],
            "environment": {"module": spec["module"], "jdk_home": str(Path(str(jdk_home)).resolve()), "timeout_seconds": timeout},
            "states": state_results,
        })
        write_json(out_path, {"records": results})
    payload = {"records": results}
    write_json(out_path, payload)
    return {"records": len(results), "output": str(out_path)}


def compile_candidate(
    repo_path: Path,
    state: dict[str, Any],
    candidate: str,
    spec: dict[str, Any],
    workspace: Path,
    manifest_dir: Path,
    jdk_home: str | None,
    timeout: int,
    state_name: str,
) -> dict[str, Any]:

    repo = GitRepo(repo_path)
    validate_environment_spec(spec, manifest_dir, jdk_home, states=(state_name,))
    archive_snapshot(repo, state["commit"], workspace)
    copy_triggering_tests(spec, state_name, workspace, manifest_dir)
    validate_workspace_layout(workspace, spec)
    _patch_workspace(workspace, state, candidate)
    env = build_process_environment(str(jdk_home))
    production = _run_command(spec["compile_command"], workspace, env, timeout, workspace.parent / f"{workspace.name}.compile.log")
    tests = _run_command(spec["test_compile_command"], workspace, env, timeout, workspace.parent / f"{workspace.name}.test-compile.log") if production["passed"] else _skipped_command()
    compilable = production["passed"] and tests["passed"]
    regression = _run_command(spec["regression_command"], workspace, env, timeout, workspace.parent / f"{workspace.name}.regression.log") if compilable else _skipped_command()
    return {
        "passed": compilable and regression["passed"],
        "compilable": compilable,
        "regression_tests_passed": regression["passed"],
        "production_compile": production,
        "test_compile": tests,
        "regression": regression,
        "log": production.get("log", ""),
    }
