from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitRepo:
    path: Path

    def run(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and proc.returncode:
            raise GitError(f"git {' '.join(args)} failed in {self.path}: {proc.stderr.strip()}")
        return proc.stdout

    def resolve(self, revision: str) -> str:
        return self.run("rev-parse", revision).strip()

    def default_branch(self) -> str:
        symbolic = self.run("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False).strip()
        if symbolic.startswith("origin/"):
            return symbolic.removeprefix("origin/")
        current = self.run("branch", "--show-current", check=False).strip()
        if current:
            return current
        for candidate in ("main", "master"):
            if self.run("rev-parse", "--verify", candidate, check=False).strip():
                return candidate
        raise GitError(f"cannot determine default branch in {self.path}")

    def parent(self, revision: str) -> str:
        return self.resolve(f"{revision}^")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(self.path), "merge-base", "--is-ancestor", ancestor, descendant],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode not in (0, 1):
            raise GitError(
                f"git merge-base --is-ancestor {ancestor} {descendant} failed in "
                f"{self.path}: {proc.stderr.strip()}"
            )
        return proc.returncode == 0

    def show_file(self, revision: str, path: str) -> str:
        return self.run("show", f"{revision}:{path}")

    def timestamp(self, revision: str) -> datetime:
        raw = self.run("show", "-s", "--format=%cI", revision).strip()
        return datetime.fromisoformat(raw).astimezone(timezone.utc)

    def message(self, revision: str) -> str:
        return self.run("show", "-s", "--format=%B", revision).strip()

    def changed_files(self, revision: str) -> list[str]:
        return [line for line in self.run("diff-tree", "--no-commit-id", "--name-only", "-r", revision).splitlines() if line]

    def changed_file_pairs(self, revision: str) -> list[dict[str, str]]:
        out = self.run("diff-tree", "--no-commit-id", "--name-status", "-r", "-M", revision)
        pairs = []
        for line in out.splitlines():
            parts = line.split("\t")
            status = parts[0]
            if status.startswith(("R", "C")) and len(parts) == 3:
                pairs.append({"status": status, "before_path": parts[1], "after_path": parts[2]})
            elif len(parts) == 2:
                pairs.append({"status": status, "before_path": parts[1], "after_path": parts[1]})
        return pairs

    def diff(self, before: str, after: str, path: str | None = None, context: int = 3) -> str:
        args = ["diff", f"--unified={context}", before, after]
        if path:
            args.extend(["--", path])
        return self.run(*args)

    def commit_count(self, start_exclusive: str, end_inclusive: str) -> int:
        return int(self.run("rev-list", "--count", f"{start_exclusive}..{end_inclusive}").strip() or 0)

    def file_commits(self, start_exclusive: str, end_inclusive: str, path: str) -> list[str]:
        out = self.run("log", "--follow", "--format=%H", f"{start_exclusive}..{end_inclusive}", "--", path)
        return list(dict.fromkeys(line.strip() for line in out.splitlines() if line.strip()))

    def history_span_days(self, branch: str) -> float:
        dates = self.run("log", branch, "--format=%cI").splitlines()
        if not dates:
            return 0.0
        newest = datetime.fromisoformat(dates[0])
        oldest = datetime.fromisoformat(dates[-1])
        return (newest - oldest).total_seconds() / 86400

    def total_commits(self, branch: str) -> int:
        return int(self.run("rev-list", "--count", branch).strip() or 0)

    def mine_fix_commits(
        self,
        branch: str,
        since: str,
        until: str,
        keywords: Iterable[str],
    ) -> list[dict[str, str]]:
        pattern = "|".join(re.escape(word) for word in keywords)
        fmt = "%H%x1f%cI%x1f%s"
        out = self.run(
            "log", branch, "--no-merges", f"--since={since}", f"--until={until}", "--regexp-ignore-case",
            f"--grep=({pattern})", "--extended-regexp", f"--format={fmt}",
        )
        rows = []
        for line in out.splitlines():
            parts = line.split("\x1f", 2)
            if len(parts) == 3:
                rows.append({"fix_commit": parts[0], "commit_time": parts[1], "subject": parts[2]})
        return rows

    def blame(self, revision: str, path: str, start: int, end: int) -> list[str]:
        out = self.run("blame", "-w", "--porcelain", f"-L{start},{end}", revision, "--", path)
        hashes = []
        for line in out.splitlines():
            if re.fullmatch(r"[0-9a-f]{40} \d+ \d+(?: \d+)?", line):
                hashes.append(line.split()[0])
        return list(dict.fromkeys(hashes))

    def line_history(self, revision: str, path: str, start: int, end: int) -> list[str]:
        out = self.run("log", "--format=%H", f"-L{start},{end}:{path}", revision)
        return list(dict.fromkeys(line for line in out.splitlines() if re.fullmatch(r"[0-9a-f]{40}", line)))


def parse_zero_context_hunks(diff_text: str) -> list[dict[str, int]]:
    hunks = []
    pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)
    for match in pattern.finditer(diff_text):
        hunks.append({
            "old_start": int(match.group(1)),
            "old_count": int(match.group(2) or 1),
            "new_start": int(match.group(3)),
            "new_count": int(match.group(4) or 1),
        })
    return hunks
