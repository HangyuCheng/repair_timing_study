from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .git import GitRepo
from ...io import read_jsonl, read_yaml, write_json, write_jsonl, write_yaml


DEFAULT_EXCLUSION_TERMS = (
    "assignment", "coursework", "demo", "demonstration", "exercise", "homework",
    "learning", "sample", "student", "teaching", "tutorial",
)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _project_identifier(full_name: str) -> str:
    return full_name.lower().replace("/", "__")


def _non_industrial_terms(repository: dict[str, Any], terms: tuple[str, ...]) -> list[str]:
    fields = [repository.get("name", ""), repository.get("description", "")]
    fields.extend(repository.get("topics") or [])
    text = " ".join(str(value).lower() for value in fields if value)
    return sorted({
        term for term in terms
        if re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text)
    })


def remote_eligibility(repository: dict[str, Any], config: dict[str, Any], collected_at: datetime) -> dict[str, Any]:
    min_stars = int(config.get("min_stars_exclusive", 3000))
    min_history_days = int(config.get("min_history_days_exclusive", 730))
    terms = tuple(str(value).lower() for value in config.get("exclude_terms", DEFAULT_EXCLUSION_TERMS))
    full_name = str(repository.get("full_name", ""))
    manual_exclusions = config.get("manual_exclusions", {}) or {}
    matched_terms = _non_industrial_terms(repository, terms)
    created_at = str(repository.get("created_at", ""))
    history_days = (collected_at - _parse_time(created_at)).total_seconds() / 86400 if created_at else -1
    checks = {
        "public": not bool(repository.get("private", False)),
        "not_fork": not bool(repository.get("fork", False)),
        "not_archived": not bool(repository.get("archived", False)),
        "not_disabled": not bool(repository.get("disabled", False)),
        "primary_language_java": str(repository.get("language", "")).lower() == "java",
        "github_stars_over_threshold": int(repository.get("stargazers_count", 0)) > min_stars,
        "remote_age_over_two_years": history_days > min_history_days,
        "non_tutorial_name_description_topics": not matched_terms,
        "not_manually_excluded": full_name not in manual_exclusions,
    }
    return {
        "eligible": all(checks.values()),
        "checks": checks,
        "remote_history_days": history_days,
        "matched_exclusion_terms": matched_terms,
        "manual_exclusion_reason": str(manual_exclusions.get(full_name, "")),
    }


@dataclass(frozen=True)
class GitHubClient:
    api_url: str = "https://api.github.com"
    token: str = ""
    timeout_seconds: int = 30

    def get_json(self, path: str, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        query = urllib.parse.urlencode(params)
        url = f"{self.api_url.rstrip('/')}/{path.lstrip('/')}?{query}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "here, enter your agent name!",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return payload, dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            remaining = exc.headers.get("X-RateLimit-Remaining", "unknown")
            reset = exc.headers.get("X-RateLimit-Reset", "unknown")
            raise RuntimeError(
                f"GitHub API request failed ({exc.code}); rate-limit remaining={remaining}, "
                f"reset={reset}: {body}"
            ) from exc

    def search_repositories(self, query: str, max_candidates: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        per_page = 100
        rows: list[dict[str, Any]] = []
        total_count = 0
        incomplete = False
        page = 1
        while True:
            payload, _headers = self.get_json(
                "/search/repositories",
                {"q": query, "sort": "stars", "order": "desc", "per_page": per_page, "page": page},
            )
            total_count = int(payload.get("total_count", 0))
            incomplete = incomplete or bool(payload.get("incomplete_results", False))
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise RuntimeError("GitHub repository search returned an invalid items field")
            rows.extend(item for item in items if isinstance(item, dict))
            if max_candidates is not None and len(rows) >= max_candidates:
                rows = rows[:max_candidates]
                break
            if not items or len(items) < per_page or len(rows) >= min(total_count, 1000):
                break
            page += 1
        if incomplete:
            raise RuntimeError("GitHub repository search reported incomplete results; retry the crawl")
        if total_count > 1000 and max_candidates is None:
            raise RuntimeError(
                "GitHub Search API exposes at most 1000 results for one query; partition the configured "
                "query or set an explicit max_candidates cap instead of silently truncating the crawl"
            )
        return rows, {"query": query, "reported_total": total_count, "retrieved": len(rows)}


def discover_projects(config_path: Path, out_path: Path) -> dict[str, Any]:
    root = read_yaml(config_path)
    config = root.get("project_collection", {})
    token = str(config.get("token", ""))
    token_env = str(config.get("token_env", "GITHUB_TOKEN"))
    if not token and token_env:
        token = os.environ.get(token_env, "")
    min_stars = int(config.get("min_stars_exclusive", 3000))
    query = str(config.get("query") or f"language:Java stars:>{min_stars} fork:false archived:false is:public")
    max_candidates_value = config.get("max_candidates")
    max_candidates = int(max_candidates_value) if max_candidates_value is not None else None
    collected_at = datetime.now(timezone.utc)
    client = GitHubClient(
        api_url=str(config.get("api_url", "https://api.github.com")),
        token=token,
        timeout_seconds=int(config.get("api_timeout_seconds", 30)),
    )
    repositories, search = client.search_repositories(query, max_candidates)
    rows = []
    for repository in repositories:
        eligibility = remote_eligibility(repository, config, collected_at)
        rows.append({
            "project_id": _project_identifier(str(repository["full_name"])),
            "github_full_name": repository["full_name"],
            "clone_url": repository["clone_url"],
            "html_url": repository["html_url"],
            "default_branch": repository.get("default_branch", ""),
            "primary_language": repository.get("language"),
            "github_stars": int(repository.get("stargazers_count", 0)),
            "created_at": repository.get("created_at"),
            "pushed_at": repository.get("pushed_at"),
            "topics": repository.get("topics") or [],
            "description": repository.get("description") or "",
            "collected_at": collected_at.isoformat(),
            "remote_eligibility": eligibility,
        })
    write_jsonl(out_path, rows)
    summary = {
        **search,
        "eligible_after_remote_filter": sum(bool(row["remote_eligibility"]["eligible"]) for row in rows),
        "output": str(out_path),
        "collected_at": collected_at.isoformat(),
    }
    write_json(out_path.with_suffix(".summary.json"), summary)
    return summary


def _clone_repository(url: str, destination: Path, timeout_seconds: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", "--origin", "origin", url, str(destination)],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )
    if proc.returncode:
        raise RuntimeError(f"git clone failed for {url}: {(proc.stderr or proc.stdout).strip()}")


def _remote_matches_project(remote_url: str, full_name: str) -> bool:
    value = remote_url.strip().lower().removesuffix(".git").replace("\\", "/")
    project = full_name.strip().lower().strip("/")
    return value.endswith("/" + project) or value.endswith(":" + project)


def select_projects(config_path: Path, candidates_path: Path, clone_root: Path, out_path: Path) -> dict[str, Any]:
    root = read_yaml(config_path)
    config = root.get("project_collection", {})
    min_history_days = int(config.get("min_history_days_exclusive", 730))
    min_commits = int(config.get("min_commits_exclusive", 5000))
    clone_timeout = int(config.get("clone_timeout_seconds", 7200))
    max_selected_value = config.get("max_selected_projects")
    max_selected = int(max_selected_value) if max_selected_value is not None else None
    history_start = str(config.get("history_start", "2015-01-01T00:00:00Z"))
    history_end = str(config.get("history_end", "2025-12-01T00:00:00Z"))
    clone_root = clone_root.resolve()
    clone_root.mkdir(parents=True, exist_ok=True)
    projects = []
    selection_results = []
    candidates = sorted(read_jsonl(candidates_path), key=lambda row: (-int(row.get("github_stars", 0)), row["github_full_name"]))
    for candidate in candidates:
        result: dict[str, Any] = {"project_id": candidate["project_id"], "github_full_name": candidate["github_full_name"]}
        if not candidate.get("remote_eligibility", {}).get("eligible"):
            result.update({"selected": False, "reason": "failed GitHub metadata filter"})
            selection_results.append(result)
            continue
        if max_selected is not None and len(projects) >= max_selected:
            result.update({"selected": False, "reason": "configured project limit reached"})
            selection_results.append(result)
            continue
        destination = (clone_root / str(candidate["project_id"])).resolve()
        try:
            destination.relative_to(clone_root)
        except ValueError as exc:
            raise ValueError(f"unsafe clone destination for {candidate['project_id']}") from exc
        if destination.exists():
            if not (destination / ".git").is_dir():
                raise FileExistsError(f"clone destination exists but is not a Git repository: {destination}")
        else:
            _clone_repository(str(candidate["clone_url"]), destination, clone_timeout)
        repo = GitRepo(destination)
        origin_url = repo.run("remote", "get-url", "origin").strip()
        if not _remote_matches_project(origin_url, str(candidate["github_full_name"])):
            raise RuntimeError(
                f"existing clone origin does not match {candidate['github_full_name']}: {origin_url}"
            )
        if bool(config.get("refresh_existing_clones", True)):
            repo.run("fetch", "--prune", "origin")
        branch = repo.default_branch()
        remote_branch = f"origin/{branch}"
        branch_revision = remote_branch if repo.run("rev-parse", "--verify", remote_branch, check=False).strip() else branch
        exact_history_days = repo.history_span_days(branch_revision)
        exact_commits = repo.total_commits(branch_revision)
        local_checks = {
            "history_over_two_years": exact_history_days > min_history_days,
            "commits_over_5000": exact_commits > min_commits,
        }
        selected = all(local_checks.values())
        result.update({
            "selected": selected,
            "local_checks": local_checks,
            "history_days": exact_history_days,
            "commit_count": exact_commits,
            "main_branch": branch_revision,
            "local_repo": str(destination),
        })
        selection_results.append(result)
        if not selected:
            continue
        projects.append({
            "name": candidate["project_id"],
            "github_full_name": candidate["github_full_name"],
            "repo_url": candidate["clone_url"],
            "local_repo": str(destination),
            "primary_language": "Java",
            "github_stars": candidate["github_stars"],
            "tutorial_or_homework": False,
            "main_branch": branch_revision,
            "history_start": history_start,
            "history_end": history_end,
            "selection_evidence": {
                "github_collected_at": candidate["collected_at"],
                "history_days": exact_history_days,
                "commit_count": exact_commits,
            },
        })
    payload = {"tools": root.get("tools", {}), "projects": projects}
    write_yaml(out_path, payload)
    report = {"candidate_count": len(candidates), "selected_count": len(projects), "projects": selection_results, "output": str(out_path)}
    write_json(out_path.with_suffix(".summary.json"), report)
    return report
