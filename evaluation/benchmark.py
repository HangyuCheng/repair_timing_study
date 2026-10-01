from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import javalang

from ..io import read_json
from ..schema import SchemaError, validate_experiment_record


_JAVA_KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class",
    "const", "continue", "default", "do", "double", "else", "enum", "extends", "final",
    "finally", "float", "for", "goto", "if", "implements", "import", "instanceof", "int",
    "interface", "long", "native", "new", "package", "private", "protected", "public",
    "return", "short", "static", "strictfp", "super", "switch", "synchronized", "this",
    "throw", "throws", "transient", "try", "void", "volatile", "while", "true", "false", "null",
}


def _yes(value: object) -> bool:
    return str(value).strip().lower() in {"yes", "true", "1"}


def _identifiers(source: str) -> set[str]:
    return {
        value for value in re.findall(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b", source)
        if value not in _JAVA_KEYWORDS
    }


def _is_statement(snippet: str) -> bool:
    try:
        javalang.parse.parse(f"class __Patch {{ void __m() {{\n{snippet}\n}} }}")
        return True
    except Exception:
        return False


def _infer_insertion_edit(context: str, reference_patch: str) -> dict[str, Any]:
    if not _is_statement(reference_patch):
        return {"kind": "insert", "position": "before-class-close"}
    identifiers = _identifiers(reference_patch)
    candidates = []
    for line_no, line in enumerate(context.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        overlap = len(identifiers & _identifiers(stripped))
        if overlap:
            opens_block = stripped.endswith("{")
            candidates.append((overlap, opens_block, -line_no, stripped))
    if not candidates:
        raise SchemaError("legacy insertion task has no inferable insertion anchor")
    _overlap, opens_block, _line_no, anchor = max(candidates)
    return {"kind": "insert", "position": "after" if opens_block else "before", "anchor": anchor}


def _legacy_state(row: dict[str, Any], state: str) -> dict[str, Any]:
    prefix = "JIT" if state == "jit" else "delay"
    method = str(row[f"{prefix}_buggy_method"])
    class_source = str(row[f"{prefix}_buggy_class"])
    reference_patch = str(row["JIT_patch" if state == "jit" else "Delay_patch"])
    buggy_statement = str(row.get("buggy_statement", ""))
    sentinel_insertion = buggy_statement.strip() == "null" and method.count(buggy_statement) != 1
    edit = _infer_insertion_edit(method if _is_statement(reference_patch) else class_source, reference_patch) if sentinel_insertion else {"kind": "replace"}
    return {
        "commit": str(row["buggy_commit"]) if state == "jit" else f"{row['fixed_commit']}^",
        "source_path": str(row["src_path"]),
        "buggy_statement": "" if sentinel_insertion else buggy_statement,
        "reference_patch": reference_patch,
        "buggy_method": method,
        "buggy_class": class_source,
        "edit": edit,
    }


def _normalize_legacy_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "bug_id": str(row["bug_id"]),
        "project": str(row["project_name"]),
        "provenance": {
            "bug_inducing_commit": str(row["buggy_commit"]),
            "delay_commit": f"{row['fixed_commit']}^",
            "fix_commit": str(row["fixed_commit"]),
            "fix_message": str(row.get("fixed_commit_description", "")),
            "fix_url": str(row.get("url", "")),
        },
        "target": {"language": "Java", "scope": "single-function"},
        "states": {
            "jit": _legacy_state(row, "jit"),
            "delay": _legacy_state(row, "delay"),
        },
        "evolution": {
            "method_drift": _yes(row.get("method_context_change")),
            "class_drift": _yes(row.get("class_context_change")),
            "legacy_time_range": str(row.get("evolutionary_time", "")),
        },
        "schema_origin": "legacy-clean-subset",
    }


def normalize_benchmark(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        result: dict[str, Any] = {"records": payload}
    elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
        result = deepcopy(payload)
    else:
        raise SchemaError("benchmark must be a record list or an object containing records")
    normalized = []
    legacy_count = 0
    for row in result["records"]:
        if not isinstance(row, dict):
            raise SchemaError("benchmark records must be objects")
        if "states" in row:
            normalized.append(row)
        elif "project_name" in row and "JIT_buggy_method" in row and "delay_buggy_method" in row:
            normalized.append(_normalize_legacy_record(row))
            legacy_count += 1
        else:
            raise SchemaError(f"bug {row.get('bug_id', '<unknown>')}: unsupported benchmark schema")
    result["records"] = normalized
    if legacy_count:
        result.setdefault("metadata", {})["normalized_legacy_records"] = legacy_count
    return result


def load_benchmark(path: Path) -> dict[str, Any]:
    payload = normalize_benchmark(read_json(path))
    for record in payload["records"]:
        validate_experiment_record(record)
    return payload
