from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import javalang
from javalang.ast import Node


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _layout_signature(text: str) -> tuple[str, list[int]]:
    value = normalize_newlines(text)
    signature: list[str] = []
    positions: list[int] = []
    state = "code"
    i = 0
    while i < len(value):
        if state == "code":
            if value.startswith('"""', i):
                for offset in range(3):
                    signature.append('"')
                    positions.append(i + offset)
                state = "text-block"
                i += 3
                continue
            if value.startswith("//", i):
                signature.extend(("/", "/"))
                positions.extend((i, i + 1))
                state = "line-comment"
                i += 2
                continue
            if value.startswith("/*", i):
                signature.extend(("/", "*"))
                positions.extend((i, i + 1))
                state = "block-comment"
                i += 2
                continue
            char = value[i]
            if char == '"':
                state = "string"
            elif char == "'":
                state = "character"
            if not char.isspace():
                signature.append(char)
                positions.append(i)
            i += 1
            continue
        char = value[i]
        signature.append(char)
        positions.append(i)
        if state in {"string", "character", "text-block"} and char == "\\" and i + 1 < len(value):
            signature.append(value[i + 1])
            positions.append(i + 1)
            i += 2
            continue
        if state == "string" and char == '"':
            state = "code"
        elif state == "character" and char == "'":
            state = "code"
        elif state == "text-block" and value.startswith('"""', i):
            for offset in (1, 2):
                signature.append('"')
                positions.append(i + offset)
            state = "code"
            i += 3
            continue
        elif state == "line-comment" and char == "\n":
            state = "code"
        elif state == "block-comment" and char == "/" and i > 0 and value[i - 1] == "*":
            state = "code"
        i += 1
    return "".join(signature), positions


def _layout_spans(source: str, target: str) -> list[tuple[int, int]]:
    source_key, positions = _layout_signature(source)
    target_key, _ = _layout_signature(target)
    if not target_key:
        return []
    spans = []
    start = source_key.find(target_key)
    while start >= 0:
        spans.append((positions[start], positions[start + len(target_key) - 1] + 1))
        start = source_key.find(target_key, start + 1)
    return spans


def _context_span(source: str, context: str | None) -> tuple[int, int] | None:
    if not context:
        return None
    exact_start = source.find(context)
    if exact_start >= 0 and source.find(context, exact_start + 1) < 0:
        return exact_start, exact_start + len(context)
    spans = _layout_spans(source, context)
    return spans[0] if len(spans) == 1 else None


def _within_context(spans: list[tuple[int, int]], context_span: tuple[int, int] | None) -> list[tuple[int, int]]:
    if context_span is None:
        return spans
    start, end = context_span
    return [span for span in spans if start <= span[0] and span[1] <= end]


def apply_replacement(source: str, buggy: str, patch: str, context: str | None = None) -> str:
    source = normalize_newlines(source)
    buggy = normalize_newlines(buggy)
    exact_count = source.count(buggy)
    if exact_count == 1:
        return source.replace(buggy, patch, 1)
    spans = _within_context(_layout_spans(source, buggy), _context_span(source, context))
    if len(spans) != 1:
        raise ValueError(f"expected one buggy-statement occurrence, found {len(spans)}")
    start, end = spans[0]
    return source[:start] + patch + source[end:]


def _indented_patch(patch: str, indent: str) -> str:
    value = textwrap.dedent(normalize_newlines(patch)).strip("\n")
    return "\n".join(indent + line if line else "" for line in value.splitlines())


def apply_insertion(source: str, patch: str, edit: dict[str, Any], context: str | None = None) -> str:
    source = normalize_newlines(source)
    context_span = _context_span(source, context)
    position = edit.get("position")
    if position == "before-class-close":
        start, end = context_span or (0, len(source))
        insertion = source.rfind("}", start, end)
        if insertion < 0:
            raise ValueError("class closing brace not found for insertion")
        line_start = source.rfind("\n", 0, insertion) + 1
        indent = re.match(r"[ \t]*", source[line_start:insertion]).group(0)
        value = _indented_patch(patch, indent + "    ")
        return source[:line_start] + value + "\n" + source[line_start:]
    anchor = str(edit.get("anchor", ""))
    spans = _within_context(_layout_spans(source, anchor), context_span)
    if len(spans) != 1:
        raise ValueError(f"expected one insertion anchor occurrence, found {len(spans)}")
    anchor_start, anchor_end = spans[0]
    line_start = source.rfind("\n", 0, anchor_start) + 1
    line_end = source.find("\n", anchor_end)
    if line_end < 0:
        line_end = len(source)
    base_indent = re.match(r"[ \t]*", source[line_start:anchor_start]).group(0)
    if position == "after":
        indent = base_indent + ("    " if anchor.rstrip().endswith("{") else "")
        value = _indented_patch(patch, indent)
        return source[:line_end] + "\n" + value + source[line_end:]
    if position == "before":
        value = _indented_patch(patch, base_indent)
        return source[:line_start] + value + "\n" + source[line_start:]
    raise ValueError(f"unsupported insertion position: {position}")


def apply_state_patch(source: str, state: dict[str, Any], patch: str) -> str:
    edit = state.get("edit", {"kind": "replace"})
    if edit.get("kind", "replace") == "insert":
        context = state.get("buggy_method") if edit.get("position") != "before-class-close" else state.get("buggy_class")
        return apply_insertion(source, patch, edit, context)
    return apply_replacement(source, state["buggy_statement"], patch, state.get("buggy_method"))


def render_insertion_marker(context: str, edit: dict[str, Any]) -> str:
    return apply_insertion(context, "/* INSERT PATCH HERE */", edit, context)


def _line_offset(source: str, line: int, column: int) -> int:
    lines = source.splitlines(keepends=True)
    return sum(len(value) for value in lines[: line - 1]) + column - 1


def _matching_brace(source: str, opening: int) -> int:
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    i = opening
    while i < len(source):
        char = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif char == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced Java braces")


def _declaration_start(source: str, node: Node, fallback: int) -> int:
    positions = [getattr(node, "position", None)]
    positions.extend(getattr(annotation, "position", None) for annotation in getattr(node, "annotations", []) or [])
    lines = [position.line for position in positions if position]
    line = min(lines) if lines else source.count("\n", 0, fallback) + 1
    start = _line_offset(source, line, 1)


    while start > 0:
        previous_end = start - 1
        previous_start = source.rfind("\n", 0, previous_end) + 1
        previous = source[previous_start:previous_end].strip()
        if not previous:
            break
        if previous.startswith("//"):
            start = previous_start
            continue
        if previous.endswith("*/"):
            block_start = source.rfind("/*", 0, previous_end)
            if block_start < 0:
                break
            block_line_start = source.rfind("\n", 0, block_start) + 1
            if source[block_line_start:block_start].strip():
                break
            start = block_line_start
            continue
        break
    return start


def _lexical_declaration_start(source: str, declaration_line_start: int) -> int:
    start = declaration_line_start
    while start > 0:
        previous_end = start - 1
        previous_start = source.rfind("\n", 0, previous_end) + 1
        previous = source[previous_start:previous_end].strip()
        if not previous:
            break
        if previous.startswith("//") or previous.startswith("@"):
            start = previous_start
            continue
        if previous.endswith("*/"):
            block_start = source.rfind("/*", 0, previous_end)
            if block_start < 0:
                break
            block_line_start = source.rfind("\n", 0, block_start) + 1
            if source[block_line_start:block_start].strip():
                break
            start = block_line_start
            continue
        if previous.endswith(")"):
            cursor = previous_start
            annotation_start = -1
            while cursor > 0:
                candidate_end = cursor - 1
                candidate_start = source.rfind("\n", 0, candidate_end) + 1
                candidate = source[candidate_start:candidate_end].strip()
                if not candidate:
                    break
                if candidate.startswith("@"):
                    annotation_start = candidate_start
                    break
                cursor = candidate_start
            if annotation_start >= 0:
                annotation = source[annotation_start:previous_end]
                if annotation.count("(") == annotation.count(")"):
                    start = annotation_start
                    continue
        break
    return start


@dataclass(frozen=True)
class JavaRegions:
    method: str
    class_source: str


def extract_regions(source: str, statement: str, method_name: str | None = None) -> JavaRegions:
    source = normalize_newlines(source)
    target = source.find(statement)
    if target < 0:
        raise ValueError("buggy statement not found in source")
    try:
        tree = javalang.parse.parse(source)
    except Exception:
        return _extract_regions_lexically(source, statement, method_name)
    methods = []
    classes = []
    for _path, node in tree:
        if not getattr(node, "position", None):
            continue
        start = _line_offset(source, node.position.line, node.position.column)
        if isinstance(node, (javalang.tree.MethodDeclaration, javalang.tree.ConstructorDeclaration)):
            if method_name and getattr(node, "name", None) != method_name:
                continue
            methods.append((start, node))
        if isinstance(node, (javalang.tree.ClassDeclaration, javalang.tree.EnumDeclaration, javalang.tree.InterfaceDeclaration)):
            classes.append((start, node))

    def enclosing(candidates: list[tuple[int, Node]]) -> str:
        matches = []
        for start, node in candidates:
            opening = source.find("{", start)
            if opening < 0:
                continue
            end = _matching_brace(source, opening) + 1
            if start <= target < end:
                declaration_start = _declaration_start(source, node, start)
                matches.append((end - declaration_start, source[declaration_start:end]))
        if not matches:
            raise ValueError("no enclosing Java region contains the buggy statement")
        return min(matches, key=lambda item: item[0])[1]

    method_source = enclosing(methods)
    enclosing(classes)
    return JavaRegions(method=method_source, class_source=source)


def _extract_regions_lexically(source: str, statement: str, method_name: str | None) -> JavaRegions:
    target = source.find(statement)
    if target < 0:
        raise ValueError("buggy statement not found in source")
    if not method_name:
        raise ValueError("method_name is required when the Java parser cannot parse the historical source")

    method_pattern = re.compile(rf"\b{re.escape(method_name)}\s*\([^;{{}}]*\)\s*(?:throws\s+[^{{]+)?\{{", re.S)
    class_pattern = re.compile(r"\b(?:class|interface|enum|record)\s+[A-Za-z_$][\w$]*[^;{}]*\{", re.S)

    def find_region(pattern: re.Pattern[str], label: str) -> str:
        matches = []
        for match in pattern.finditer(source, 0, target + 1):
            opening = source.find("{", match.start(), match.end())
            if opening < 0:
                continue
            end = _matching_brace(source, opening) + 1
            if match.start() <= target < end:
                line_start = source.rfind("\n", 0, match.start()) + 1
                declaration_start = _lexical_declaration_start(source, line_start)
                matches.append((end - declaration_start, source[declaration_start:end]))
        if not matches:
            raise ValueError(f"no enclosing Java {label} found by lexical fallback")
        return min(matches, key=lambda item: item[0])[1]

    method_source = find_region(method_pattern, "method")
    find_region(class_pattern, "class")
    return JavaRegions(method=method_source, class_source=source)


def canonical_ast(value: object) -> str:
    if value is None:
        return "None"
    if isinstance(value, (str, int, float, bool)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_ast(item) for item in value) + "]"
    if not isinstance(value, Node):
        return repr(value)
    fields = [f"{name}={canonical_ast(getattr(value, name))}" for name in value.attrs if name != "position"]
    return f"{type(value).__name__}(" + ",".join(fields) + ")"


def parse_patch_ast(snippet: str) -> str | None:
    snippet = snippet.strip()
    variants = [snippet, snippet + ";"] if not snippet.endswith((";", "}")) else [snippet]
    for variant in variants:
        try:
            tree = javalang.parse.parse(f"class __Patch {{ void __m() {{\n{variant}\n}} }}")
            return canonical_ast(tree.types[0].methods[0].body)
        except Exception:
            pass
        try:
            expression = variant[:-1] if variant.endswith(";") else variant
            return canonical_ast(javalang.parse.parse_expression(expression))
        except Exception:
            pass
    return None


class GumTree:
    def __init__(self, executable: str = "gumtree") -> None:
        self.executable = executable

    def available(self) -> bool:
        return bool(shutil.which(self.executable) or Path(self.executable).exists())

    def actions(self, before: str, after: str) -> list[dict]:
        if not self.available():
            raise RuntimeError(f"GumTree executable not found: {self.executable}")
        with tempfile.TemporaryDirectory(prefix="jitbench-gumtree-") as temp:
            left = Path(temp) / "Before.java"
            right = Path(temp) / "After.java"
            left.write_text(before, encoding="utf-8")
            right.write_text(after, encoding="utf-8")
            commands = [
                [self.executable, "textdiff", "-f", "JSON", str(left), str(right)],
                [self.executable, "textdiff", "-f", "json", str(left), str(right)],
            ]
            last_error = ""
            for command in commands:
                proc = subprocess.run(command, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if proc.returncode == 0:
                    payload = json.loads(proc.stdout)
                    return payload.get("actions", payload if isinstance(payload, list) else [])
                last_error = proc.stderr.strip()
            raise RuntimeError(f"GumTree diff failed: {last_error}")

    def patch_equal(self, candidate: str, reference: str) -> bool:
        wrapper = lambda body: f"class __Patch {{ void __m() {{\n{body}\n}} }}\n"
        return len(self.actions(wrapper(candidate), wrapper(reference))) == 0
