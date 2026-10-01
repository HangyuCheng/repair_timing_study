from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

from .JITBench_Construction.collect.collection import prepare_candidates
from .JITBench_Construction.collect.github import discover_projects, select_projects
from .JITBench_Construction.bug_reconstruction.reconstruction import (
    add_metadata,
    create_evidence_bundle,
    finalize,
    reconstruct,
    validate_decisions_file,
)
from .JITBench_Construction.validation.runner import run_validation
from .evaluation.evaluation import apply_semantic_labels, run_evaluation
from .evaluation.generation import generate_all


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _handler(function: Callable, **mapping: str) -> Callable[[argparse.Namespace], None]:
    def run(args: argparse.Namespace) -> None:
        kwargs = {target: getattr(args, source) for target, source in mapping.items()}
        _print(function(**kwargs))
    return run


def main() -> None:
    parser = argparse.ArgumentParser(prog="jitbench")
    top = parser.add_subparsers(dest="area", required=True)

    construction = top.add_parser("construction")
    csub = construction.add_subparsers(dest="command", required=True)

    crawl = csub.add_parser("crawl-projects")
    crawl.add_argument("--config", type=_path, required=True)
    crawl.add_argument("--out", type=_path, required=True)
    crawl.set_defaults(run=_handler(discover_projects, config_path="config", out_path="out"))

    select = csub.add_parser("select-projects")
    select.add_argument("--config", type=_path, required=True)
    select.add_argument("--candidates", type=_path, required=True)
    select.add_argument("--clone-root", type=_path, required=True)
    select.add_argument("--out", type=_path, required=True)
    select.set_defaults(run=_handler(
        select_projects,
        config_path="config",
        candidates_path="candidates",
        clone_root="clone_root",
        out_path="out",
    ))

    prepare = csub.add_parser("prepare")
    prepare.add_argument("--config", type=_path, required=True)
    prepare.add_argument("--out", type=_path, required=True)
    prepare.set_defaults(run=_handler(prepare_candidates, config_path="config", out_path="out"))

    evidence = csub.add_parser("evidence")
    evidence.add_argument("--projects", type=_path, required=True)
    evidence.add_argument("--candidates", type=_path, required=True)
    evidence.add_argument("--out", type=_path, required=True)
    evidence.set_defaults(run=_handler(create_evidence_bundle, projects_path="projects", candidates_path="candidates", out_dir="out"))

    decisions = csub.add_parser("validate-decisions")
    decisions.add_argument("--decisions", type=_path, required=True)
    decisions.set_defaults(run=_handler(validate_decisions_file, path="decisions"))

    reconstruction = csub.add_parser("reconstruct")
    reconstruction.add_argument("--projects", type=_path, required=True)
    reconstruction.add_argument("--decisions", type=_path, required=True)
    reconstruction.add_argument("--out", type=_path, required=True)
    reconstruction.set_defaults(run=_handler(reconstruct, projects_path="projects", decisions_path="decisions", out_path="out"))

    metadata = csub.add_parser("metadata")
    metadata.add_argument("--projects", type=_path, required=True)
    metadata.add_argument("--benchmark", type=_path, required=True)
    metadata.add_argument("--out", type=_path, required=True)
    metadata.set_defaults(run=_handler(add_metadata, projects_path="projects", benchmark_path="benchmark", out_path="out"))

    finish = csub.add_parser("finalize")
    finish.add_argument("--benchmark", type=_path, required=True)
    finish.add_argument("--validation", type=_path, required=True)
    finish.add_argument("--out", type=_path, required=True)
    finish.set_defaults(run=_handler(finalize, benchmark_path="benchmark", validation_path="validation", out_path="out"))

    validation = top.add_parser("validation")
    vsub = validation.add_subparsers(dest="command", required=True)
    validate = vsub.add_parser("run")
    validate.add_argument("--projects", type=_path, required=True)
    validate.add_argument("--benchmark", type=_path, required=True)
    validate.add_argument("--manifest", type=_path, required=True)
    validate.add_argument("--work-root", type=_path, required=True)
    validate.add_argument("--out", type=_path, required=True)
    validate.set_defaults(run=_handler(run_validation, projects_path="projects", benchmark_path="benchmark", manifest_path="manifest", work_root="work_root", out_path="out"))

    experiment = top.add_parser("experiment")
    esub = experiment.add_subparsers(dest="command", required=True)
    generate = esub.add_parser("generate")
    generate.add_argument("--benchmark", type=_path, required=True)
    generate.add_argument("--config", type=_path, required=True)
    generate.add_argument("--out", type=_path, required=True)
    generate.add_argument("--only-bug")
    generate.add_argument("--no-resume", action="store_true")
    generate.set_defaults(run=lambda args: _print(generate_all(args.benchmark, args.config, args.out, args.only_bug, not args.no_resume)))

    evaluation = top.add_parser("evaluation")
    evsub = evaluation.add_subparsers(dest="command", required=True)
    evaluate = evsub.add_parser("run")
    evaluate.add_argument("--benchmark", type=_path, required=True)
    evaluate.add_argument("--generations", type=_path, required=True)
    evaluate.add_argument("--validation", type=_path, required=True)
    evaluate.add_argument("--projects", type=_path, required=True)
    evaluate.add_argument("--out", type=_path, required=True)
    evaluate.add_argument("--semantic-labels", type=_path)
    evaluate.add_argument("--ast-engine", choices=("gumtree", "javalang"), default="gumtree")
    evaluate.set_defaults(run=lambda args: _print(run_evaluation(args.benchmark, args.generations, args.validation, args.projects, args.out, args.semantic_labels, args.ast_engine)))

    semantic = evsub.add_parser("apply-semantic-labels")
    semantic.add_argument("--benchmark", type=_path, required=True)
    semantic.add_argument("--judgments", type=_path, required=True)
    semantic.add_argument("--labels", type=_path, required=True)
    semantic.add_argument("--out", type=_path, required=True)
    semantic.set_defaults(run=_handler(apply_semantic_labels, benchmark_path="benchmark", judgments_path="judgments", labels_path="labels", out_dir="out"))

    args = parser.parse_args()
    args.run(args)
