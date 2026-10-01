from __future__ import annotations

import difflib
from collections import Counter
from typing import Any

from .java import GumTree, normalize_newlines


TARGET_METHOD_MARKER = "/* JITBENCH_TARGET_METHOD_EXCLUDED */"


def _remove_target_method(class_source: str, method_source: str) -> str:
    class_value = normalize_newlines(class_source)
    method_value = normalize_newlines(method_source)
    if class_value.count(method_value) != 1:
        raise ValueError("target method is not uniquely embedded in its enclosing class")
    return class_value.replace(method_value, TARGET_METHOD_MARKER, 1)


def _changed_lines(before: str, after: str) -> dict[str, int]:
    added = deleted = 0
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            deleted += 1
    return {"added": added, "deleted": deleted, "total": added + deleted}


def _unified_diff(before: str, after: str, before_name: str, after_name: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=before_name,
            tofile=after_name,
            lineterm="",
        )
    )


def _action_kind(action: Any) -> str:
    if not isinstance(action, dict):
        return "unknown"
    value = action.get("action") or action.get("name") or action.get("type") or "unknown"
    return str(value).strip().lower().replace(" ", "-")


def _action_summary(actions: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(_action_kind(action) for action in actions).items()))


def analyze_context_drift(jit: dict[str, Any], delay: dict[str, Any], gumtree: GumTree) -> dict[str, Any]:
    jit_method = normalize_newlines(str(jit["buggy_method"]))
    delay_method = normalize_newlines(str(delay["buggy_method"]))
    jit_class = normalize_newlines(str(jit["buggy_class"]))
    delay_class = normalize_newlines(str(delay["buggy_class"]))

    jit_class_context = _remove_target_method(jit_class, jit_method)
    delay_class_context = _remove_target_method(delay_class, delay_method)

    method_text_changed = jit_method != delay_method
    class_text_changed = jit_class_context != delay_class_context
    method_actions = gumtree.actions(
        f"class __JITBenchMethodContext {{\n{jit_method}\n}}",
        f"class __JITBenchMethodContext {{\n{delay_method}\n}}",
    )
    class_actions = gumtree.actions(jit_class_context, delay_class_context)
    method_ast_changed = bool(method_actions)
    class_ast_changed = bool(class_actions)

    return {
        "definition": {
            "method": "complete target method",
            "class": "complete Java compilation unit including package and import declarations with the target method replaced by a stable marker",
        },
        "method_drift": method_text_changed or method_ast_changed,
        "class_drift": class_text_changed or class_ast_changed,
        "method_text_changed": method_text_changed,
        "class_text_changed": class_text_changed,
        "method_ast_changed": method_ast_changed,
        "class_ast_changed": class_ast_changed,
        "method_changed_lines": _changed_lines(jit_method, delay_method),
        "class_changed_lines": _changed_lines(jit_class_context, delay_class_context),
        "method_text_diff": _unified_diff(jit_method, delay_method, "jit-method", "delay-method"),
        "class_text_diff": _unified_diff(jit_class_context, delay_class_context, "jit-class", "delay-class"),
        "method_gumtree_actions": len(method_actions),
        "class_gumtree_actions": len(class_actions),
        "method_gumtree_action_types": _action_summary(method_actions),
        "class_gumtree_action_types": _action_summary(class_actions),
        "method_gumtree_action_details": method_actions,
        "class_gumtree_action_details": class_actions,
    }
