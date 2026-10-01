from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Iterable

from ..collect.git import GitRepo


COMMAND_KEYS = ("compile_command", "test_compile_command", "trigger_command", "regression_command")


def archive_snapshot(repo: GitRepo, commit: str, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"workspace already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = destination.parent / f"{destination.name}.zip"
    proc = subprocess.run(
        ["git", "-C", str(repo.path), "archive", "--format=zip", f"--output={archive}", commit],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode:
        raise RuntimeError(f"git archive failed: {proc.stderr.strip()}")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(destination)
    archive.unlink()


def _within(root: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def copy_triggering_tests(spec: dict[str, Any], state: str, workspace: Path, manifest_dir: Path) -> None:
    tests = spec.get("triggering_tests", [])
    if not tests:
        raise ValueError("a nonempty triggering_tests list is required")
    for index, test in enumerate(tests, 1):
        source_value = test.get(f"{state}_source", test.get("source"))
        target_value = test.get(f"{state}_target")
        if not source_value or not target_value:
            raise ValueError(f"triggering_tests[{index}] requires a source and {state}_target")
        source = Path(source_value)
        if not source.is_absolute():
            source = manifest_dir / source
        if not source.is_file():
            raise FileNotFoundError(f"triggering test source does not exist: {source}")
        target = workspace / str(target_value)
        if not _within(workspace, target):
            raise ValueError(f"triggering test target escapes the workspace: {target_value}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _validate_command(spec: dict[str, Any], key: str) -> None:
    command = spec.get(key)
    if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part.strip() for part in command):
        raise ValueError(f"{key} must be a nonempty list of command arguments")


def _java_executable(jdk_home: Path) -> Path | None:
    for name in ("java", "java.exe"):
        candidate = jdk_home / "bin" / name
        if candidate.is_file():
            return candidate
    return None


def validate_environment_spec(
    spec: dict[str, Any],
    manifest_dir: Path,
    jdk_home: str | None,
    states: Iterable[str] = ("jit", "delay"),
) -> None:
    module = spec.get("module")
    if not isinstance(module, str) or not module.strip():
        raise ValueError("validation module is required")
    module_path = Path(module)
    if module_path.is_absolute() or ".." in module_path.parts:
        raise ValueError("validation module must be a workspace-relative path")
    for key in COMMAND_KEYS:
        _validate_command(spec, key)
    if not jdk_home:
        raise ValueError("jdk_home is required for a reproducible validation environment")
    resolved_jdk = Path(jdk_home).expanduser().resolve()
    if _java_executable(resolved_jdk) is None:
        raise FileNotFoundError(f"JDK java executable does not exist under: {resolved_jdk}")
    tests = spec.get("triggering_tests")
    if not isinstance(tests, list) or not tests:
        raise ValueError("a nonempty triggering_tests list is required")
    for state in states:
        for index, test in enumerate(tests, 1):
            source_value = test.get(f"{state}_source", test.get("source"))
            target_value = test.get(f"{state}_target")
            if not source_value or not target_value:
                raise ValueError(f"triggering_tests[{index}] requires a source and {state}_target")
            source = Path(source_value)
            if not source.is_absolute():
                source = manifest_dir / source
            if not source.is_file():
                raise FileNotFoundError(f"triggering test source does not exist: {source}")
            target = Path(str(target_value))
            if target.is_absolute() or ".." in target.parts:
                raise ValueError(f"triggering_tests[{index}].{state}_target must stay inside the workspace")


def build_process_environment(jdk_home: str) -> dict[str, str]:
    resolved_jdk = Path(jdk_home).expanduser().resolve()
    env = dict(os.environ)
    env["JAVA_HOME"] = str(resolved_jdk)
    env["PATH"] = str(resolved_jdk / "bin") + os.pathsep + env.get("PATH", "")
    return env


def validate_workspace_layout(workspace: Path, spec: dict[str, Any]) -> None:
    module = workspace / str(spec["module"])
    if not module.exists():
        raise FileNotFoundError(f"configured validation module does not exist in snapshot: {spec['module']}")
    for key in COMMAND_KEYS:
        executable = spec[key][0]
        if executable.startswith(".") or "/" in executable or "\\" in executable:
            command_path = workspace / executable
            if not command_path.exists():
                raise FileNotFoundError(f"{key} executable does not exist in snapshot: {executable}")


def prepare_state_workspaces(
    repo: GitRepo,
    state: dict[str, Any],
    spec: dict[str, Any],
    state_name: str,
    manifest_dir: Path,
    base: Path,
) -> tuple[Path, Path]:
    before_dir = base / "before"
    after_dir = base / "after"
    archive_snapshot(repo, state["commit"], before_dir)
    shutil.copytree(before_dir, after_dir)
    copy_triggering_tests(spec, state_name, before_dir, manifest_dir)
    copy_triggering_tests(spec, state_name, after_dir, manifest_dir)
    validate_workspace_layout(before_dir, spec)
    validate_workspace_layout(after_dir, spec)
    return before_dir, after_dir
