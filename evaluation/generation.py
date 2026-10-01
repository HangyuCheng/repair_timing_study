from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .benchmark import load_benchmark
from ..io import read_yaml, write_json
from .prompts import SYSTEM_PROMPT, build_prompt, parse_candidates
from .providers import ModelProvider


def _slug(value: object) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in str(value)).strip("-")


def generate_all(
    benchmark_path: Path,
    config_path: Path,
    out_dir: Path,
    only_bug: str | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    benchmark = load_benchmark(benchmark_path)
    config = read_yaml(config_path)
    budget = int(config.get("candidate_budget", 10))
    records = [row for row in benchmark["records"] if only_bug is None or str(row["bug_id"]) == str(only_bug)]
    completed = skipped = failed = 0
    for model in config["models"]:
        provider = ModelProvider(model)
        for strategy in config["strategies"]:
            for context in config["contexts"]:
                for state in config["states"]:
                    for temperature in config["temperatures"]:
                        run_dir = out_dir / _slug(model["name"]) / strategy / context / state / f"temp-{temperature}"
                        for record in records:
                            output = run_dir / f"{record['bug_id']}.json"
                            if resume and output.exists():
                                skipped += 1
                                continue
                            user_prompt = build_prompt(record, state, context, strategy, budget)
                            started = time.time()
                            try:
                                response, raw = provider.generate(SYSTEM_PROMPT, user_prompt, float(temperature))
                                candidates = parse_candidates(response, budget)
                                payload = {
                                    "bug_id": str(record["bug_id"]),
                                    "project": record["project"],
                                    "model": model["name"],
                                    "model_id": model["model_id"],
                                    "strategy": strategy,
                                    "context": context,
                                    "state": state,
                                    "temperature": float(temperature),
                                    "candidate_budget": budget,
                                    "candidates": candidates,
                                    "system_prompt": SYSTEM_PROMPT,
                                    "user_prompt": user_prompt,
                                    "raw_response": response,
                                    "provider_response": raw,
                                    "elapsed_seconds": time.time() - started,
                                }
                                write_json(output, payload)
                                completed += 1
                            except Exception as exc:
                                write_json(output.with_suffix(".error.json"), {
                                    "bug_id": str(record["bug_id"]), "model": model["name"], "error": str(exc),
                                    "system_prompt": SYSTEM_PROMPT, "user_prompt": user_prompt,
                                })
                                failed += 1
    summary = {"completed": completed, "skipped": skipped, "failed": failed, "output": str(out_dir)}
    write_json(out_dir / "generation-summary.json", summary)
    return summary
