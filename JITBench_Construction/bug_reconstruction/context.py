from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


FLOW_KEYS = ("method_data_flow", "method_control_flow", "class_data_flow", "class_control_flow")
NODE_KINDS = {
    "CALL", "CONTROL_STRUCTURE", "IDENTIFIER", "LITERAL", "LOCAL",
    "METHOD_PARAMETER_IN", "METHOD_RETURN", "MEMBER", "TYPE_DECL",
}
DATA_FLOW_EDGES = {"DDG", "REACHING_DEF", "DATA_DEPENDENCE", "DATA_FLOW"}
CONTROL_FLOW_EDGES = {"CFG", "CDG", "CONTROL_DEPENDENCE", "DOMINATE", "POST_DOMINATE"}


class Joern:
    def __init__(self, parser: str, exporter: str) -> None:
        self.parser = parser
        self.exporter = exporter

    @staticmethod
    def _available(executable: str) -> bool:
        return bool(shutil.which(executable) or Path(executable).exists())

    def available(self) -> bool:
        return self._available(self.parser) and self._available(self.exporter)

    @staticmethod
    def _run(command: list[str], timeout: int = 900) -> None:
        proc = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if proc.returncode:
            output = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(f"Joern command failed: {output}")

    @staticmethod
    def _properties(value: str) -> dict[str, str]:
        properties = {
            key.upper(): raw.replace(r'\"', '"').replace(r"\n", "\n")
            for key, raw in re.findall(r'(\w+)\s*=\s*"((?:\\.|[^"\\])*)"', value)
        }
        if "LABEL" not in properties:
            html_label = re.search(r"\blabel\s*=\s*<(.+)>", value, flags=re.IGNORECASE)
            if html_label:
                properties["LABEL"] = re.sub(r"<[^>]+>", " ", html_label.group(1))
        return properties

    @classmethod
    def _read_graphs(cls, export_dir: Path) -> tuple[dict[str, dict[str, str]], list[tuple[str, str, str]]]:
        nodes: dict[str, dict[str, str]] = {}
        edges: list[tuple[str, str, str]] = []
        dot_files = sorted(export_dir.rglob("*.dot"))
        if not dot_files:
            raise RuntimeError("Joern export produced no DOT files")
        for dot_path in dot_files:
            prefix = str(dot_path.relative_to(export_dir)).replace("\\", "/")
            for line in dot_path.read_text(encoding="utf-8", errors="replace").splitlines():
                edge_match = re.match(r'^\s*"?([^"\s]+)"?\s*->\s*"?([^"\s]+)"?\s*\[(.*)\]\s*;?$', line)
                if edge_match:
                    properties = cls._properties(edge_match.group(3))
                    kind = (properties.get("LABEL") or properties.get("TYPE") or "UNKNOWN").upper()
                    edges.append((f"{prefix}:{edge_match.group(1)}", f"{prefix}:{edge_match.group(2)}", kind))
                    continue
                node_match = re.match(r'^\s*"?([^"\s]+)"?\s*\[(.*)\]\s*;?$', line)
                if node_match:
                    properties = cls._properties(node_match.group(2))
                    if properties:
                        nodes[f"{prefix}:{node_match.group(1)}"] = properties
        return nodes, edges

    @staticmethod
    def _line(properties: dict[str, str], key: str, default: int = 0) -> int:
        try:
            return int(properties.get(key, "") or default)
        except ValueError:
            return default

    @staticmethod
    def _node_kind(properties: dict[str, str]) -> str:
        explicit = properties.get("NODE_LABEL") or properties.get("TYPE") or ""
        if explicit:
            return explicit.upper()
        label = properties.get("LABEL", "").upper()
        for kind in sorted(NODE_KINDS | {"METHOD"}, key=len, reverse=True):
            if re.search(rf"(?:^|[^A-Z0-9_]){re.escape(kind)}(?:$|[^A-Z0-9_])", label):
                return kind
        return label

    @staticmethod
    def _method_name(properties: dict[str, str]) -> str:
        if properties.get("NAME"):
            return properties["NAME"]
        label = properties.get("LABEL", "")
        match = re.search(r"(?:^|[(,\s])METHOD\s*[,|:]\s*([^,)<>]+)", label, flags=re.IGNORECASE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _node_value(properties: dict[str, str]) -> str:
        return properties.get("CODE") or properties.get("NAME") or properties.get("TYPE_FULL_NAME") or ""

    def features(self, source: str, method_name: str, method_source: str | None = None) -> dict[str, list[str]]:
        if not self.available():
            raise RuntimeError("Joern parser or exporter is unavailable")
        with tempfile.TemporaryDirectory(prefix="jitbench-joern-") as temp:
            root = Path(temp)
            source_dir = root / "source"
            source_dir.mkdir()
            (source_dir / "Context.java").write_text(source, encoding="utf-8")
            cpg = root / "cpg.bin"
            export_dir = root / "export"
            self._run([self.parser, str(source_dir), "--language", "javasrc", "--output", str(cpg)])
            self._run([self.exporter, "--repr", "all", "--format", "dot", "--out", str(export_dir), str(cpg)])
            nodes, edges = self._read_graphs(export_dir)

            methods = [
                (node_id, properties) for node_id, properties in nodes.items()
                if self._node_kind(properties) == "METHOD"
                and self._method_name(properties) == method_name
                and properties.get("IS_EXTERNAL", "false").lower() == "false"
            ]
            if not methods:
                raise RuntimeError(f"Joern did not find method {method_name}")
            if method_source and source.count(method_source) == 1:
                target_line = source.count("\n", 0, source.index(method_source)) + 1
                scoped = [
                    item for item in methods
                    if self._line(item[1], "LINE_NUMBER") <= target_line
                    <= self._line(item[1], "LINE_NUMBER_END", self._line(item[1], "LINE_NUMBER"))
                ]
                if scoped:
                    methods = scoped
            if len(methods) != 1:
                raise RuntimeError(f"Joern method mapping is ambiguous for {method_name}")

            method = methods[0][1]
            start = self._line(method, "LINE_NUMBER")
            end = self._line(method, "LINE_NUMBER_END", start)
            features: dict[str, list[str]] = {key: [] for key in FLOW_KEYS}
            method_nodes = set()
            for node_id, properties in nodes.items():
                kind = self._node_kind(properties)
                if kind not in NODE_KINDS:
                    continue
                value = self._node_value(properties)
                if not value or value == "<empty>":
                    continue
                features.setdefault(f"class_{kind.lower()}", []).append(value)
                line = self._line(properties, "LINE_NUMBER")
                if start and start <= line <= end:
                    method_nodes.add(node_id)
                    features.setdefault(f"method_{kind.lower()}", []).append(value)

            for source_id, target_id, edge_kind in edges:
                source_value = self._node_value(nodes.get(source_id, {})) or source_id.rsplit(":", 1)[-1]
                target_value = self._node_value(nodes.get(target_id, {})) or target_id.rsplit(":", 1)[-1]
                edge_value = f"{edge_kind}:{source_value}->{target_value}"
                if edge_kind in DATA_FLOW_EDGES:
                    features["class_data_flow"].append(edge_value)
                    if source_id in method_nodes and target_id in method_nodes:
                        features["method_data_flow"].append(edge_value)
                if edge_kind in CONTROL_FLOW_EDGES:
                    features["class_control_flow"].append(edge_value)
                    if source_id in method_nodes and target_id in method_nodes:
                        features["method_control_flow"].append(edge_value)
            return {key: sorted(values) for key, values in sorted(features.items())}

    @staticmethod
    def delta(before: dict[str, list[str]], after: dict[str, list[str]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for kind in sorted(set(before) | set(after)):
            left = Counter(before.get(kind, []))
            right = Counter(after.get(kind, []))
            added = list((right - left).elements())
            removed = list((left - right).elements())
            result[kind] = {"added": added, "removed": removed, "changed": len(added) + len(removed)}
        result["total_changed"] = sum(value["changed"] for value in result.values() if isinstance(value, dict))
        return result
