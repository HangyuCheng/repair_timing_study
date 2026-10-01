from __future__ import annotations

import json
import re
from typing import Any

from ..JITBench_Construction.bug_reconstruction.java import render_insertion_marker


SYSTEM_PROMPT = "You are a professional Java developer specializing in software development and maintenance. Please repair the bug in the provided Java method using the supplied context. "


def _build_context_prompt(record: dict[str, Any], state: str, context: str, strategy: str, budget: int) -> str:
    state_data = record["states"][state]
    if strategy not in {"zero-shot", "cot"}:
        raise ValueError("strategy must be zero-shot or cot")
    code = state_data["buggy_method"] if context == "method" else state_data["buggy_class"]
    edit = state_data.get("edit", {"kind": "replace"})
    if edit.get("kind") == "insert":
        marker_context = state_data["buggy_method"] if edit.get("position") != "before-class-close" else state_data["buggy_class"]
        code = render_insertion_marker(marker_context, edit)
        task = "Replace the insertion marker with the missing Java code"
        buggy = "/* INSERT PATCH HERE */"
    else:
        task = "Repair the buggy statement in the Java code below"
        buggy = state_data["buggy_statement"]
    if context == "class":
        method_code = state_data["buggy_method"]
        class_code = state_data["buggy_class"]
        if edit.get("kind") == "insert":
            if edit.get("position") == "before-class-close":
                class_code = code
            else:
                method_code = code
        context_text = f"""Buggy method:
```java
{method_code}
```

Class context:
```java
{class_code}
```"""
    else:
        context_text = f"""Method context:
```java
{code}
```"""
    prompt = f"""{task}. Return no more than {budget} candidate replacement patches as JSON in the form {{"patches": ["patch 1", "patch 2"]}}. Do not response anything else except the candidate patches.

Buggy statement:
```java
{buggy}
```

{context_text}
"""
    if strategy == "cot":
        prompt = prompt.rstrip() + "\n\nLet's think step by step.\n"
    return prompt


def build_method_prompt(record: dict[str, Any], state: str, strategy: str, budget: int) -> str:
    return _build_context_prompt(record, state, "method", strategy, budget)


def build_class_prompt(record: dict[str, Any], state: str, strategy: str, budget: int) -> str:
    return _build_context_prompt(record, state, "class", strategy, budget)


def build_prompt(record: dict[str, Any], state: str, context: str, strategy: str, budget: int) -> str:
    if context == "method":
        return build_method_prompt(record, state, strategy, budget)
    if context == "class":
        return build_class_prompt(record, state, strategy, budget)
    raise ValueError("context must be method or class")


def build_messages(record: dict[str, Any], state: str, context: str, strategy: str, budget: int) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(record, state, context, strategy, budget)},
    ]


def _strip_fence(text: str) -> str:
    value = text.strip()
    value = re.sub(r"^```(?:json|java)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _legacy_fenced_candidates(response: str) -> list[str]:
    fence_line = r"(?m)^\s*(?:[-*]\s*)?```(?:java)?\s*$"
    if len(re.findall(fence_line, response, flags=re.I)) < 2:
        return []
    values = []
    for part in re.split(fence_line, response, flags=re.I):
        value = part.strip()
        value = re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", value)
        if value:
            values.append(value)
    return values


def _plain_candidates(response: str, budget: int) -> list[str]:
    groups = [value.strip() for value in re.split(r"\n\s*\n", response) if value.strip()]
    if 1 < len(groups) <= budget:
        return groups
    numbered = []
    for line in response.splitlines():
        match = re.match(r"^\s*(?:\d+[.)]|[-*])\s*(.+?)\s*$", line)
        if match:
            numbered.append(match.group(1).strip("`"))
    if numbered:
        return numbered
    lines = [line.strip() for line in response.splitlines() if line.strip()]
    if len(lines) <= budget:
        return lines
    candidates = []
    current = []
    depth = 0
    for line in lines:
        current.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0 and (line.endswith(";") or line.endswith("}")):
            candidates.append("\n".join(current))
            current = []
    if current:
        candidates.append("\n".join(current))
    return candidates if 1 < len(candidates) <= budget else lines


def parse_candidates(response: str, budget: int = 10) -> list[str]:
    response = response.replace("\r\n", "\n").replace("\r", "\n").strip()
    candidates: list[str] = []

    def add(value: object) -> None:
        patch = _strip_fence(str(value)).strip()
        if patch:
            candidates.append(patch)

    json_candidates = [response, _strip_fence(response)]
    match = re.search(r"\{[\s\S]*\}", response)
    if match:
        json_candidates.append(match.group(0))
    for value in json_candidates:
        try:
            payload = json.loads(value)
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("patches"), list):
            for patch in payload["patches"]:
                add(patch)
            return candidates[:budget]
        if isinstance(payload, list):
            for patch in payload:
                add(patch)
            return candidates[:budget]

    fenced = _legacy_fenced_candidates(response)
    if not fenced:
        fenced = re.findall(r"```(?:java)?\s*([\s\S]*?)```", response, flags=re.I)
    for patch in fenced:
        add(patch)
    if not candidates:
        for patch in _plain_candidates(response, budget):
            add(patch)
    return candidates[:budget]
