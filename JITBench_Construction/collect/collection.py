from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .git import GitRepo
from ...io import read_yaml, write_json, write_jsonl


FIX_KEYWORDS = ("fix", "solve", "bug", "mistake", "defect", "issue", "error")
FIX_SUBJECT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:fix(?:e[ds]?|ing)?|solve[ds]?|resolve[ds]?|bugs?|mistakes?|defects?|issues?|errors?)(?![A-Za-z0-9])",
    flags=re.IGNORECASE,
)


def _slug(value: object) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in str(value)).strip("-")


def _is_bug_fix_subject(subject: str) -> bool:
    return bool(FIX_SUBJECT_PATTERN.search(subject))


def project_map(config_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    config = read_yaml(config_path)
    projects = {row["name"]: row for row in config.get("projects", [])}
    if not projects:
        raise ValueError("projects configuration is empty")
    return projects, config.get("tools", {})


def _production_java(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    return lowered.endswith(".java") and "/src/test/" not in lowered and "/test/" not in lowered


def _test_java(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    return lowered.endswith(".java") and ("/src/test/" in lowered or "/test/" in lowered)


def prepare_candidates(config_path: Path, out_path: Path) -> dict[str, Any]:
    projects, _tools = project_map(config_path)
    rows: list[dict[str, Any]] = []
    selection_results = []
    for name, spec in projects.items():
        repo = GitRepo(Path(spec["local_repo"]))
        configured_branch = spec.get("main_branch", "auto")
        branch = repo.default_branch() if configured_branch == "auto" else configured_branch
        eligibility = {
            "primary_language_java": str(spec.get("primary_language", "")).lower() == "java",
            "github_stars_over_3000": int(spec.get("github_stars", 0)) > 3000,
            "history_over_two_years": repo.history_span_days(branch) > 730,
            "commits_over_5000": repo.total_commits(branch) > 5000,
            "non_tutorial": not bool(spec.get("tutorial_or_homework", False)),
        }
        selection_results.append({"project": name, **eligibility})
        if not all(eligibility.values()):
            continue
        since = spec.get("history_start", "2015-01-01T00:00:00Z")
        until = spec.get("history_end", "2025-12-01T00:00:00Z")
        for commit in repo.mine_fix_commits(branch, since, until, FIX_KEYWORDS):
            if not _is_bug_fix_subject(commit["subject"]):
                continue
            changed_pairs = repo.changed_file_pairs(commit["fix_commit"])
            java_files = [
                pair for pair in changed_pairs
                if not pair["status"].startswith(("A", "D"))
                and _production_java(pair["before_path"])
                and _production_java(pair["after_path"])
            ]
            if not java_files:
                continue
            candidate_id = f"{_slug(name)}-{commit['fix_commit'][:12]}"
            rows.append({
                "candidate_id": candidate_id,
                "project": name,
                **commit,
                "parent_commit": repo.parent(commit["fix_commit"]),
                "production_java_files": java_files,
                "developer_test_files": [
                    pair["after_path"] for pair in changed_pairs
                    if not pair["status"].startswith("D") and _test_java(pair["after_path"])
                ],
            })
    write_jsonl(out_path, rows)
    report = {"candidate_count": len(rows), "projects": selection_results, "output": str(out_path)}
    write_json(out_path.with_suffix(".summary.json"), report)
    return report
