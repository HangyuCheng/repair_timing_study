from __future__ import annotations

from typing import Any


class SchemaError(ValueError):
    pass


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    value = mapping.get(key)
    if value is None or value == "" or value == []:
        raise SchemaError(f"{where}: missing {key}")
    return value


def validate_decision(row: dict[str, Any]) -> None:
    where = f"decision {row.get('candidate_id', '<unknown>')}"
    if row.get("accept") is not True:
        return
    for key in (
        "bug_id", "project", "fix_commit", "bug_inducing_commit", "method_name",
        "jit_source_path", "delay_source_path", "jit_buggy_statement",
        "delay_buggy_statement", "delay_reference_patch", "root_cause",
        "triggering_conditions", "faulty_behavior", "expected_behavior",
        "patch_construction", "bug_inducing_commit_justification",
        "supporting_evidence", "evidence",
    ):
        _require(row, key, where)
    if row["patch_construction"] not in {"direct_backport", "adapted"}:
        raise SchemaError(f"{where}: patch_construction must be direct_backport or adapted")
    if row["patch_construction"] == "adapted":
        _require(row, "jit_reference_patch", where)
        _require(row, "adaptation_notes", where)
    for key in (
        "real_production_bug", "defect_changes_isolated", "single_function_repair",
        "target_function_in_both_states", "same_defect_across_states",
        "ast_mapping_interpretable",
    ):
        if row.get(key) is not True:
            raise SchemaError(f"{where}: {key} must be true")


def validate_record(record: dict[str, Any], require_metadata: bool = True) -> None:
    where = f"bug {record.get('bug_id', '<unknown>')}"
    for key in ("bug_id", "project", "provenance", "target", "states"):
        _require(record, key, where)
    states = record["states"]
    for state in ("jit", "delay"):
        value = _require(states, state, where)
        for key in ("commit", "source_path", "buggy_statement", "reference_patch", "buggy_method", "buggy_class"):
            _require(value, key, f"{where}/{state}")
        if value["buggy_method"].count(value["buggy_statement"]) != 1:
            raise SchemaError(f"{where}/{state}: buggy statement must occur exactly once in method")
        if value["buggy_class"].count(value["buggy_method"]) != 1:
            raise SchemaError(f"{where}/{state}: target method must occur exactly once in enclosing class")
    if states["jit"]["commit"] == states["delay"]["commit"]:
        raise SchemaError(f"{where}: JIT and delay commits must differ")
    provenance = record["provenance"]
    for key in ("bug_inducing_commit", "delay_commit", "fix_commit", "fix_message", "fix_url"):
        _require(provenance, key, where)
    if provenance["bug_inducing_commit"] != states["jit"]["commit"]:
        raise SchemaError(f"{where}: JIT commit must be the bug-inducing commit")
    if provenance["delay_commit"] != states["delay"]["commit"]:
        raise SchemaError(f"{where}: delayed state must be the parent of the fix commit")
    if require_metadata:
        evolution = _require(record, "evolution", where)
        for key in ("evolutionary_days", "repository_commit_count", "target_file_commit_count", "method_drift", "class_drift"):
            if key not in evolution:
                raise SchemaError(f"{where}: missing evolution.{key}")
        for key in ("method_gumtree_actions", "class_gumtree_actions"):
            if evolution.get(key) is None:
                raise SchemaError(f"{where}: missing evolution.{key}")
        if evolution.get("gumtree_error"):
            raise SchemaError(f"{where}: GumTree failed: {evolution['gumtree_error']}")
        definition = _require(evolution, "definition", where)
        if definition.get("method") != "complete target method":
            raise SchemaError(f"{where}: invalid method-level context-drift definition")
        if "complete Java compilation unit" not in str(definition.get("class", "")):
            raise SchemaError(f"{where}: invalid class-level context-drift definition")
        for level in ("method", "class"):
            text_changed = evolution.get(f"{level}_text_changed")
            ast_changed = evolution.get(f"{level}_ast_changed")
            drift = evolution.get(f"{level}_drift")
            if not isinstance(text_changed, bool) or not isinstance(ast_changed, bool):
                raise SchemaError(f"{where}: {level} text/AST drift flags must be boolean")
            if drift != (text_changed or ast_changed):
                raise SchemaError(f"{where}: inconsistent {level}_drift flag")
            details = evolution.get(f"{level}_gumtree_action_details")
            if not isinstance(details, list) or len(details) != evolution[f"{level}_gumtree_actions"]:
                raise SchemaError(f"{where}: inconsistent {level} GumTree action evidence")
            changed_lines = evolution.get(f"{level}_changed_lines")
            if not isinstance(changed_lines, dict) or changed_lines.get("total") != changed_lines.get("added", 0) + changed_lines.get("deleted", 0):
                raise SchemaError(f"{where}: inconsistent {level} changed-line evidence")
            text_diff = evolution.get(f"{level}_text_diff")
            if not isinstance(text_diff, str) or bool(text_diff) != text_changed:
                raise SchemaError(f"{where}: inconsistent {level} textual-diff evidence")
        joern_status = evolution.get("joern_status")
        if joern_status not in {"success", "unavailable", "error"}:
            raise SchemaError(f"{where}: invalid Joern status")
        for key in ("joern_jit_features", "joern_delay_features", "joern_context_delta"):
            if not isinstance(evolution.get(key), dict):
                raise SchemaError(f"{where}: evolution.{key} must be an object")
        if not isinstance(evolution.get("joern_error", ""), str):
            raise SchemaError(f"{where}: evolution.joern_error must be text")
        if joern_status == "success":
            for key in ("method_data_flow", "method_control_flow", "class_data_flow", "class_control_flow"):
                if key not in evolution["joern_jit_features"] or key not in evolution["joern_delay_features"]:
                    raise SchemaError(f"{where}: successful Joern analysis is missing {key}")
            if evolution.get("joern_error"):
                raise SchemaError(f"{where}: successful Joern analysis contains an error")
        else:
            if evolution.get("joern_context_delta"):
                raise SchemaError(f"{where}: failed or unavailable Joern analysis cannot contain a delta")
            if joern_status == "error" and not evolution.get("joern_error"):
                raise SchemaError(f"{where}: failed Joern analysis must record its error")
        if evolution["evolutionary_days"] < 0 or evolution["repository_commit_count"] < 0 or evolution["target_file_commit_count"] < 0:
            raise SchemaError(f"{where}: evolution metrics must be nonnegative")
        if not evolution["method_drift"] and not evolution["class_drift"]:
            raise SchemaError(f"{where}: both repair contexts are identical")


def validate_experiment_record(record: dict[str, Any]) -> None:
    where = f"bug {record.get('bug_id', '<unknown>')}"
    if not record.get("bug_id") or not record.get("project"):
        raise SchemaError(f"{where}: experiment record requires bug_id and project")
    states = record.get("states")
    if not isinstance(states, dict):
        raise SchemaError(f"{where}: experiment record requires states")
    for state_name in ("jit", "delay"):
        state = states.get(state_name)
        if not isinstance(state, dict):
            raise SchemaError(f"{where}: missing {state_name} state")
        for key in ("commit", "source_path", "reference_patch", "buggy_method", "buggy_class"):
            if state.get(key) in (None, ""):
                raise SchemaError(f"{where}/{state_name}: missing {key}")
        edit = state.get("edit", {"kind": "replace"})
        kind = edit.get("kind", "replace")
        if kind == "replace" and not state.get("buggy_statement"):
            raise SchemaError(f"{where}/{state_name}: replacement task requires buggy_statement")
        if kind == "insert" and edit.get("position") not in {"before", "after", "before-class-close"}:
            raise SchemaError(f"{where}/{state_name}: invalid insertion position")
        if kind == "insert" and edit.get("position") != "before-class-close" and not edit.get("anchor"):
            raise SchemaError(f"{where}/{state_name}: insertion task requires an anchor")
        if kind not in {"replace", "insert"}:
            raise SchemaError(f"{where}/{state_name}: unsupported edit kind {kind}")


def validate_validation_result(result: dict[str, Any], bug_id: str) -> None:
    where = f"validation bug {bug_id}"
    for state in ("jit", "delay"):
        item = _require(result.get("states", {}), state, where)
        before = _require(item, "before_patch", f"{where}/{state}")
        after = _require(item, "after_patch", f"{where}/{state}")
        expected = {
            "compile_passed": True,
            "triggering_tests_passed": False,
            "regression_tests_passed": True,
        }
        for key, value in expected.items():
            if before.get(key) is not value:
                raise SchemaError(f"{where}/{state}: before_patch.{key} must be {value}")
        if before.get("trigger_failure_verified") is not True:
            raise SchemaError(f"{where}/{state}: before_patch trigger failure must match the documented target-defect contract")
        evidence = before.get("trigger_failure_evidence")
        if not isinstance(evidence, dict) or evidence.get("verified") is not True or not evidence.get("failure_reason"):
            raise SchemaError(f"{where}/{state}: missing verified trigger-failure evidence")
        before_trigger = before.get("commands", {}).get("trigger", {})
        if before_trigger.get("skipped") or before_trigger.get("timed_out"):
            raise SchemaError(f"{where}/{state}: triggering test cannot be skipped or timed out")
        for key in expected:
            if after.get(key) is not True:
                raise SchemaError(f"{where}/{state}: after_patch.{key} must be true")
        for command_name, command in after.get("commands", {}).items():
            if command.get("skipped") or command.get("timed_out"):
                raise SchemaError(f"{where}/{state}: after_patch command {command_name} cannot be skipped or timed out")
