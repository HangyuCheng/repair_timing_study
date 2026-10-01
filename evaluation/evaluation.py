from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .benchmark import load_benchmark
from ..JITBench_Construction.bug_reconstruction.java import GumTree, parse_patch_ast
from ..JITBench_Construction.collect.collection import project_map
from ..JITBench_Construction.validation.runner import compile_candidate
from ..io import read_json, read_jsonl, read_yaml, write_json, write_jsonl
from .prompts import parse_candidates


KS = (1, 5, 10)


def clean_patch(text: object) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    value = re.sub(r"^```(?:java)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    return "\n".join(line.rstrip() for line in value.strip().splitlines()).strip()


def exact_key(text: object) -> str:

    source = clean_patch(text)
    normalized: list[str] = []
    state = "code"
    i = 0
    while i < len(source):
        if state == "code":
            if source.startswith('"""', i):
                normalized.append('"""')
                state = "text-block"
                i += 3
                continue
            if source.startswith("//", i):
                normalized.append("//")
                state = "line-comment"
                i += 2
                continue
            if source.startswith("/*", i):
                normalized.append("/*")
                state = "block-comment"
                i += 2
                continue
            char = source[i]
            if char == '"':
                normalized.append(char)
                state = "string"
            elif char == "'":
                normalized.append(char)
                state = "character"
            elif not char.isspace():
                normalized.append(char)
            i += 1
            continue

        if state in {"string", "character"}:
            char = source[i]
            normalized.append(char)
            if char == "\\" and i + 1 < len(source):
                normalized.append(source[i + 1])
                i += 2
                continue
            if (state == "string" and char == '"') or (state == "character" and char == "'"):
                state = "code"
            i += 1
            continue

        if state == "text-block":
            if source.startswith('"""', i):
                normalized.append('"""')
                state = "code"
                i += 3
                continue
            char = source[i]
            normalized.append(char)
            if char == "\\" and i + 1 < len(source):
                normalized.append(source[i + 1])
                i += 2
            else:
                i += 1
            continue

        char = source[i]
        normalized.append(char)
        i += 1
        if state == "line-comment" and char == "\n":
            state = "code"
        elif state == "block-comment" and len(normalized) >= 2 and normalized[-2:] == ["*", "/"]:
            state = "code"
    return "".join(normalized)


def exact_match(candidate: str, reference: str) -> bool:
    return bool(exact_key(reference)) and exact_key(candidate) == exact_key(reference)


def ast_match(candidate: str, reference: str, gumtree: GumTree | None = None) -> bool:
    if exact_match(candidate, reference):
        return True
    if gumtree is not None:
        return gumtree.patch_equal(clean_patch(candidate), clean_patch(reference))
    left = parse_patch_ast(candidate)
    right = parse_patch_ast(reference)
    return left is not None and left == right


def _candidate_id(run: dict[str, Any], rank: int) -> str:
    fields = [run[key] for key in ("model", "strategy", "context", "state", "temperature", "bug_id")]
    return "__".join(_safe(value) for value in fields) + f"__rank-{rank}"


def _load_semantic_labels(path: Path | None) -> dict[str, bool]:
    if path is None or not path.exists():
        return {}
    labels = {}
    for row in read_jsonl(path):
        value = row.get("correct")
        if not isinstance(value, bool):
            raise ValueError(f"semantic label {row.get('candidate_id')}: correct must be boolean")
        labels[row["candidate_id"]] = value
    return labels


def _legacy_generation(row: dict[str, Any]) -> dict[str, Any]:
    run_directory = str(row.get("run_directory", ""))
    context_match = re.search(r"_(method|class)_", run_directory, flags=re.I)
    temperature: object = row.get("temperature", 0.0)
    try:
        temperature = float(temperature)
    except (TypeError, ValueError):
        pass
    return {
        "bug_id": str(row["bug_id"]),
        "model": row["model"],
        "strategy": row.get("strategy", row.get("prompt", "zero-shot")),
        "context": context_match.group(1).lower() if context_match else row.get("context", "method"),
        "state": str(row["state"]).lower(),
        "temperature": temperature,
        "candidate_budget": int(row.get("candidate_budget", 10)),
        "candidates": parse_candidates(str(row.get("content", "")), int(row.get("candidate_budget", 10))),
        "raw_response": row.get("content", ""),
    }


def _generation_runs(root: Path) -> list[dict[str, Any]]:
    if root.is_file():
        payload = read_json(root)
        if isinstance(payload, dict) and isinstance(payload.get("output"), list):
            return [_legacy_generation(row) for row in payload["output"]]
        if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
            return [payload]
        raise ValueError(f"unsupported generation artifact: {root}")
    files = [path for path in root.rglob("*.json") if not path.name.endswith("summary.json") and not path.name.endswith("error.json")]
    return [read_json(path) for path in files]


def _safe(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value))


def run_evaluation(
    benchmark_path: Path,
    generations_dir: Path,
    validation_path: Path,
    projects_path: Path,
    out_dir: Path,
    semantic_labels_path: Path | None = None,
    ast_engine: str = "gumtree",
) -> dict[str, Any]:
    benchmark = load_benchmark(benchmark_path)
    records = {str(row["bug_id"]): row for row in benchmark["records"]}
    validation = read_yaml(validation_path)
    bug_specs = {str(key): value for key, value in validation.get("bugs", {}).items()}
    defaults = validation.get("defaults", {})
    projects, tools = project_map(projects_path)
    gumtree = GumTree(str(tools.get("gumtree", "gumtree"))) if ast_engine == "gumtree" else None
    if gumtree is not None and not gumtree.available():
        raise RuntimeError("AST evaluation requires GumTree, but it is unavailable")
    semantic = _load_semantic_labels(semantic_labels_path)
    judgments = []
    queue = []
    for run in _generation_runs(generations_dir):
        bug_id = str(run.get("bug_id", ""))
        if bug_id not in records or bug_id not in bug_specs:
            continue
        record = records[bug_id]
        state_name = run["state"]
        state = record["states"][state_name]
        spec = bug_specs[bug_id]
        project_validation = validation.get("projects", {}).get(record["project"], {})
        jdk_home = spec.get("jdk_home") or project_validation.get("jdk_home")
        timeout = int(spec.get("timeout_seconds", defaults.get("timeout_seconds", 1800)))
        for rank, candidate in enumerate(run.get("candidates", [])[:10], 1):
            candidate_id = _candidate_id(run, rank)

            workspace = out_dir / "candidate-validation-work" / _safe(run["model"]) / run["strategy"] / run["context"] / state_name / f"temp-{run['temperature']}" / f"bug-{bug_id}" / f"{state_name}-{rank}"
            validation_cache = workspace.parent / f"{workspace.name}.validation-result.json"
            if validation_cache.exists():
                validation_result = read_json(validation_cache)
            else:
                validation_result = compile_candidate(
                    Path(projects[record["project"]]["local_repo"]), state, candidate, spec, workspace,
                    validation_path.parent, str(jdk_home) if jdk_home else None, timeout, state_name,
                )
                write_json(validation_cache, validation_result)
            compilable = bool(validation_result["compilable"])
            regression_passed = bool(validation_result["regression_tests_passed"])
            validation_passed = compilable and regression_passed
            exact = validation_passed and exact_match(candidate, state["reference_patch"])
            structural = exact or (validation_passed and ast_match(candidate, state["reference_patch"], gumtree))
            semantic_correct = structural or (validation_passed and semantic.get(candidate_id, False))
            row = {
                "candidate_id": candidate_id,
                "bug_id": bug_id,
                "project": record["project"],
                "model": run["model"],
                "strategy": run["strategy"],
                "context": run["context"],
                "state": state_name,
                "temperature": run["temperature"],
                "rank": rank,
                "candidate": candidate,
                "reference_patch": state["reference_patch"],
                "compilable": compilable,
                "regression_tests_passed": regression_passed,
                "validation_passed": validation_passed,
                "exact": exact,
                "ast": structural,
                "semantic": semantic_correct,
                "semantic_status": (
                    "compilation-failed" if not compilable else
                    "regression-failed" if not regression_passed else
                    "automatic" if structural else
                    "external-label" if candidate_id in semantic else "pending"
                ),
                "compile_log": validation_result.get("log", ""),
                "regression_log": validation_result["regression"].get("log", ""),
                "regression_result": validation_result["regression"],
            }
            judgments.append(row)
            if validation_passed and not structural and candidate_id not in semantic:
                queue.append({
                    **row,
                    "repair_intent": record.get("repair_intent", {}),
                    "buggy_method": state["buggy_method"],
                    "buggy_class": state["buggy_class"],
                })
    write_jsonl(out_dir / "candidate-judgments.jsonl", judgments)
    write_jsonl(out_dir / "semantic-candidates.jsonl", queue)
    tables = aggregate_fix_at_k(judgments, len(records), require_complete_semantic=not queue)
    write_json(out_dir / "fix-at-k.json", tables)
    summary = {
        "benchmark_size": len(records),
        "candidate_judgments": len(judgments),
        "semantic_pending": len(queue),
        "semantic_complete": not queue,
        "ast_engine": ast_engine,
        "outputs": {"judgments": str(out_dir / "candidate-judgments.jsonl"), "semantic_candidates": str(out_dir / "semantic-candidates.jsonl"), "metrics": str(out_dir / "fix-at-k.json")},
    }
    write_json(out_dir / "evaluation-summary.json", summary)
    return summary


def aggregate_fix_at_k(judgments: list[dict[str, Any]], denominator: int, require_complete_semantic: bool) -> dict[str, Any]:
    run_fields = ("model", "strategy", "context", "state", "temperature")
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in judgments:
        grouped[tuple(row[field] for field in run_fields)].append(row)
    rows = []
    for key, values in sorted(grouped.items(), key=lambda item: tuple(str(value) for value in item[0])):
        result = dict(zip(run_fields, key))
        by_bug: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for value in values:
            by_bug[str(value["bug_id"])].append(value)
        for metric in ("exact", "ast", "semantic"):
            if metric == "semantic" and not require_complete_semantic:
                result["Semantic-Fix"] = {"status": "pending external semantic labels"}
                continue
            name = {"exact": "Exact-Fix", "ast": "AST-Fix", "semantic": "Semantic-Fix"}[metric]
            for k in KS:
                count = sum(any(row[metric] for row in bug_rows if int(row["rank"]) <= k) for bug_rows in by_bug.values())
                result[f"{name}@{k}"] = {"count": count, "rate": count / denominator if denominator else 0.0, "denominator": denominator}
        rows.append(result)
    return {"rows": rows, "denominator": denominator, "ks": list(KS)}


def apply_semantic_labels(
    benchmark_path: Path,
    judgments_path: Path,
    labels_path: Path,
    out_dir: Path,
) -> dict[str, Any]:
    benchmark = load_benchmark(benchmark_path)
    judgments = read_jsonl(judgments_path)
    labels = _load_semantic_labels(labels_path)
    missing = []
    for row in judgments:
        if "regression_tests_passed" not in row:
            raise ValueError("candidate judgments lack regression results; rerun evaluation before applying semantic labels")
        if not row["compilable"] or not row["regression_tests_passed"]:
            row["semantic"] = False
            row["semantic_status"] = "regression-failed" if row["compilable"] else "compilation-failed"
            continue
        if row["compilable"] and not row["ast"]:
            candidate_id = row["candidate_id"]
            if candidate_id not in labels:
                missing.append(candidate_id)
                continue
            row["semantic"] = labels[candidate_id]
            row["semantic_status"] = "external-label"
    if missing:
        raise ValueError(f"semantic labels are incomplete for {len(missing)} candidates")
    write_jsonl(out_dir / "candidate-judgments.final.jsonl", judgments)
    metrics = aggregate_fix_at_k(judgments, len(benchmark["records"]), require_complete_semantic=True)
    write_json(out_dir / "fix-at-k.final.json", metrics)
    summary = {
        "benchmark_size": len(benchmark["records"]),
        "candidate_judgments": len(judgments),
        "semantic_complete": True,
        "judgments": str(out_dir / "candidate-judgments.final.jsonl"),
        "metrics": str(out_dir / "fix-at-k.final.json"),
    }
    write_json(out_dir / "semantic-label-summary.json", summary)
    return summary
