from __future__ import annotations

from datetime import timezone
from pathlib import Path
from typing import Any

from .drift import analyze_context_drift
from .context import Joern
from ..collect.collection import project_map
from ..collect.git import GitRepo, parse_zero_context_hunks
from ...io import read_json, read_jsonl, write_json, write_jsonl
from .java import GumTree, extract_regions
from ...schema import SchemaError, validate_decision, validate_record, validate_validation_result


def create_evidence_bundle(projects_path: Path, candidates_path: Path, out_dir: Path) -> dict[str, Any]:
    projects, tools = project_map(projects_path)
    gumtree = GumTree(str(tools.get("gumtree", "gumtree")))
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions = []
    evidence_index = []
    for candidate in read_jsonl(candidates_path):
        repo = GitRepo(Path(projects[candidate["project"]]["local_repo"]))
        fix_commit = candidate["fix_commit"]
        parent = candidate["parent_commit"]
        for source_index, source_item in enumerate(candidate["production_java_files"], 1):
            if isinstance(source_item, str):
                before_path = after_path = source_item
            else:
                before_path = source_item["before_path"]
                after_path = source_item["after_path"]
            item_id = f"{candidate['candidate_id']}-file-{source_index}"
            diff_text = repo.run("diff", "--unified=0", parent, fix_commit, "--", before_path, after_path)
            hunks = parse_zero_context_hunks(diff_text)
            blame_commits = []
            line_history_commits = []
            for hunk in hunks:
                if hunk["old_count"]:
                    end = hunk["old_start"] + hunk["old_count"] - 1
                    blame_commits.extend(repo.blame(parent, before_path, hunk["old_start"], end))
                    line_history_commits.extend(repo.line_history(parent, before_path, hunk["old_start"], end))
            blame_commits = list(dict.fromkeys(blame_commits))
            line_history_commits = list(dict.fromkeys(line_history_commits))
            before = repo.show_file(parent, before_path)
            after = repo.show_file(fix_commit, after_path)
            actions: list[dict[str, Any]] | None = None
            gumtree_error = ""
            try:
                actions = gumtree.actions(before, after)
            except Exception as exc:
                gumtree_error = str(exc)
            evidence = {
                "evidence_id": item_id,
                "candidate_id": candidate["candidate_id"],
                "project": candidate["project"],
                "fix_commit": fix_commit,
                "delay_commit": parent,
                "before_source_path": before_path,
                "after_source_path": after_path,
                "commit_message": repo.message(fix_commit),
                "diff": diff_text,
                "hunks": hunks,
                "blame_candidates": blame_commits,
                "log_L_candidates": line_history_commits,
                "developer_test_files": candidate.get("developer_test_files", []),
                "gumtree_action_count": len(actions) if actions is not None else None,
                "gumtree_actions": actions,
                "gumtree_error": gumtree_error,
            }
            evidence_path = out_dir / "evidence" / f"{item_id}.json"
            write_json(evidence_path, evidence)
            evidence_index.append({"evidence_id": item_id, "evidence": str(evidence_path)})
            decisions.append({
                "candidate_id": item_id,
                "accept": False,
                "bug_id": "",
                "project": candidate["project"],
                "fix_commit": fix_commit,
                "bug_inducing_commit": "",
                "method_name": "",
                "jit_source_path": before_path,
                "delay_source_path": before_path,
                "jit_buggy_statement": "",
                "delay_buggy_statement": "",
                "delay_reference_patch": "",
                "jit_reference_patch": "",
                "patch_construction": "direct_backport",
                "adaptation_notes": "",
                "root_cause": "",
                "triggering_conditions": "",
                "faulty_behavior": "",
                "expected_behavior": "",
                "real_production_bug": False,
                "defect_changes_isolated": False,
                "single_function_repair": False,
                "target_function_in_both_states": False,
                "same_defect_across_states": False,
                "ast_mapping_interpretable": False,
                "bug_inducing_commit_justification": "",
                "supporting_evidence": "",
                "evidence": str(evidence_path),
            })
    write_jsonl(out_dir / "decisions.jsonl", decisions)
    write_json(out_dir / "index.json", evidence_index)
    return {"evidence_items": len(decisions), "decisions": str(out_dir / "decisions.jsonl")}


def _production_java_path(value: object) -> bool:
    source_path = str(value).replace("\\", "/").lower()
    return source_path.endswith(".java") and "/src/test/" not in source_path and "/test/" not in source_path


def _load_decision_evidence(row: dict[str, Any], decisions_path: Path) -> dict[str, Any]:
    value = Path(str(row["evidence"]))
    candidates = [value] if value.is_absolute() else [decisions_path.parent / value, value]
    evidence_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if evidence_path is None:
        raise SchemaError(f"decision {row['candidate_id']}: evidence file does not exist: {value}")
    evidence = read_json(evidence_path)
    if not isinstance(evidence, dict):
        raise SchemaError(f"decision {row['candidate_id']}: evidence must be a JSON object")
    expected = {
        "evidence_id": row["candidate_id"],
        "project": row["project"],
        "fix_commit": row["fix_commit"],
    }
    for key, expected_value in expected.items():
        if str(evidence.get(key, "")) != str(expected_value):
            raise SchemaError(f"decision {row['candidate_id']}: evidence {key} does not match the decision")
    if not _production_java_path(evidence.get("before_source_path")) or not _production_java_path(evidence.get("after_source_path")):
        raise SchemaError(f"decision {row['candidate_id']}: evidence is not a Java production-code change")
    if evidence.get("gumtree_error"):
        raise SchemaError(f"decision {row['candidate_id']}: GumTree evidence failed: {evidence['gumtree_error']}")
    if not isinstance(evidence.get("gumtree_actions"), list) or int(evidence.get("gumtree_action_count") or 0) <= 0:
        raise SchemaError(f"decision {row['candidate_id']}: nonempty GumTree edit evidence is required")
    if not evidence.get("hunks"):
        raise SchemaError(f"decision {row['candidate_id']}: textual diff hunks are required")
    return evidence


def validate_decisions_file(path: Path) -> dict[str, Any]:
    rows = read_jsonl(path)
    errors = []
    accepted = 0
    bug_ids = set()
    candidate_ids = set()
    for row in rows:
        try:
            candidate_id = str(row.get("candidate_id", ""))
            if not candidate_id:
                raise SchemaError("decision is missing candidate_id")
            if candidate_id in candidate_ids:
                raise SchemaError(f"duplicate candidate_id: {candidate_id}")
            candidate_ids.add(candidate_id)
            validate_decision(row)
            if row.get("accept") is True:
                _load_decision_evidence(row, path)
                accepted += 1
                bug_id = str(row["bug_id"])
                if bug_id in bug_ids:
                    raise SchemaError(f"duplicate accepted bug_id: {bug_id}")
                bug_ids.add(bug_id)
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise SchemaError("decision validation failed:\n" + "\n".join(errors))
    return {"rows": len(rows), "accepted": accepted}


def reconstruct(projects_path: Path, decisions_path: Path, out_path: Path) -> dict[str, Any]:
    projects, _tools = project_map(projects_path)
    validate_decisions_file(decisions_path)
    records = []
    for decision in read_jsonl(decisions_path):
        if decision.get("accept") is not True:
            continue
        repo = GitRepo(Path(projects[decision["project"]]["local_repo"]))
        fix_commit = repo.resolve(decision["fix_commit"])
        delay_commit = repo.parent(fix_commit)
        jit_commit = repo.resolve(decision["bug_inducing_commit"])
        if jit_commit == delay_commit or not repo.is_ancestor(jit_commit, delay_commit):
            raise SchemaError(
                f"decision {decision['candidate_id']}: bug-inducing commit must be a strict "
                "ancestor of the delayed repair state"
            )
        jit_source = repo.show_file(jit_commit, decision["jit_source_path"])
        delay_source = repo.show_file(delay_commit, decision["delay_source_path"])
        jit_regions = extract_regions(jit_source, decision["jit_buggy_statement"], decision["method_name"])
        delay_regions = extract_regions(delay_source, decision["delay_buggy_statement"], decision["method_name"])
        jit_patch = decision.get("jit_reference_patch") or decision["delay_reference_patch"]
        base_url = projects[decision["project"]].get("repo_url", "").removesuffix(".git")
        record = {
            "bug_id": str(decision["bug_id"]),
            "project": decision["project"],
            "provenance": {
                "bug_inducing_commit": jit_commit,
                "delay_commit": delay_commit,
                "fix_commit": fix_commit,
                "fix_message": repo.message(fix_commit),
                "fix_url": f"{base_url}/commit/{fix_commit}" if base_url else fix_commit,
            },
            "target": {"language": "Java", "method_name": decision["method_name"], "scope": "single-function"},
            "repair_intent": {
                "root_cause": decision["root_cause"],
                "triggering_conditions": decision["triggering_conditions"],
                "faulty_behavior": decision["faulty_behavior"],
                "expected_behavior": decision["expected_behavior"],
            },
            "states": {
                "jit": {
                    "commit": jit_commit,
                    "source_path": decision["jit_source_path"],
                    "buggy_statement": decision["jit_buggy_statement"],
                    "reference_patch": jit_patch,
                    "patch_construction": decision["patch_construction"],
                    "adaptation_notes": decision.get("adaptation_notes", ""),
                    "buggy_method": jit_regions.method,
                    "buggy_class": jit_regions.class_source,
                },
                "delay": {
                    "commit": delay_commit,
                    "source_path": decision["delay_source_path"],
                    "buggy_statement": decision["delay_buggy_statement"],
                    "reference_patch": decision["delay_reference_patch"],
                    "patch_construction": "developer_fix",
                    "adaptation_notes": "",
                    "buggy_method": delay_regions.method,
                    "buggy_class": delay_regions.class_source,
                },
            },
        }
        validate_record(record, require_metadata=False)
        records.append(record)
    payload = {
        "schema_version": "1.0",
        "description": "Paired timing-aware Java repair tasks",
        "records": sorted(records, key=lambda item: int(item["bug_id"])),
    }
    write_json(out_path, payload)
    return {"records": len(records), "output": str(out_path)}


def add_metadata(projects_path: Path, benchmark_path: Path, out_path: Path) -> dict[str, Any]:
    projects, tools = project_map(projects_path)
    payload = read_json(benchmark_path)
    gumtree = GumTree(str(tools.get("gumtree", "gumtree")))
    if not gumtree.available():
        raise RuntimeError("GumTree is required for context-drift extraction")
    joern = Joern(str(tools.get("joern_parse", "joern-parse")), str(tools.get("joern_export", "joern-export")))
    joern_required = bool(tools.get("joern_required", False))
    if joern_required and not joern.available():
        raise RuntimeError("Joern is required by configuration but its parser or exporter is unavailable")
    excluded = []
    kept = []
    for record in payload["records"]:
        repo = GitRepo(Path(projects[record["project"]]["local_repo"]))
        jit = record["states"]["jit"]
        delay = record["states"]["delay"]
        try:
            drift = analyze_context_drift(jit, delay, gumtree)
        except Exception as exc:
            raise RuntimeError(f"bug {record['bug_id']}: context-drift extraction failed: {exc}") from exc
        if not drift["method_drift"] and not drift["class_drift"]:
            excluded.append({"bug_id": record["bug_id"], "reason": "identical method and class contexts"})
            continue
        start = repo.timestamp(jit["commit"])
        end = repo.timestamp(delay["commit"])
        if end < start:
            raise SchemaError(f"bug {record['bug_id']}: delayed state predates the JIT state")
        joern_status = "unavailable"
        joern_error = ""
        joern_jit: dict[str, list[str]] = {}
        joern_delay: dict[str, list[str]] = {}
        if joern.available():
            try:
                joern_jit = joern.features(jit["buggy_class"], record["target"]["method_name"], jit["buggy_method"])
                joern_delay = joern.features(delay["buggy_class"], record["target"]["method_name"], delay["buggy_method"])
                joern_status = "success"
            except Exception as exc:
                joern_status = "error"
                joern_error = str(exc)
                if joern_required:
                    raise RuntimeError(f"bug {record['bug_id']}: Joern analysis failed: {exc}") from exc
        target_file_commits = set()
        for source_path in {jit["source_path"], delay["source_path"]}:
            target_file_commits.update(repo.file_commits(jit["commit"], delay["commit"], source_path))
        record["evolution"] = {
            "jit_timestamp": start.astimezone(timezone.utc).isoformat(),
            "delay_timestamp": end.astimezone(timezone.utc).isoformat(),
            "evolutionary_days": (end - start).total_seconds() / 86400,
            "repository_commit_count": repo.commit_count(jit["commit"], delay["commit"]),
            "target_file_commit_count": len(target_file_commits),
            **drift,
            "gumtree_error": "",
            "joern_status": joern_status,
            "joern_jit_features": joern_jit,
            "joern_delay_features": joern_delay,
            "joern_context_delta": Joern.delta(joern_jit, joern_delay) if joern_status == "success" else {},
            "joern_error": joern_error,
        }
        validate_record(record)
        kept.append(record)
    payload["records"] = kept
    payload["metadata_exclusions"] = excluded
    write_json(out_path, payload)
    return {"records": len(kept), "excluded": len(excluded), "output": str(out_path)}


def finalize(benchmark_path: Path, validation_path: Path, out_path: Path) -> dict[str, Any]:
    payload = read_json(benchmark_path)
    validation = read_json(validation_path)
    by_bug = {str(row["bug_id"]): row for row in validation.get("records", [])}
    final_records = []
    errors = []
    for record in payload["records"]:
        bug_id = str(record["bug_id"])
        try:
            validate_record(record)
            result = by_bug.get(bug_id)
            if result is None:
                raise SchemaError(f"bug {bug_id}: missing executable validation")
            validate_validation_result(result, bug_id)
            record["validation"] = result
            final_records.append(record)
        except Exception as exc:
            errors.append(str(exc))
    project_count = len({record["project"] for record in final_records})
    drift = {
        "method": sum(bool(record["evolution"]["method_drift"]) for record in final_records),
        "class": sum(bool(record["evolution"]["class_drift"]) for record in final_records),
        "both": sum(bool(record["evolution"]["method_drift"] and record["evolution"]["class_drift"]) for record in final_records),
    }
    if errors:
        raise SchemaError("finalization rejected records:\n" + "\n".join(errors))
    payload["records"] = final_records
    payload["metadata"] = {
        "benchmark": "JITBench",
        "record_count": len(final_records),
        "paired_states": ["jit", "delay"],
        "validation": "paired executable environments",
        "project_count": project_count,
        "context_drift_counts": drift,
    }
    write_json(out_path, payload)
    return {"records": len(final_records), "output": str(out_path)}


