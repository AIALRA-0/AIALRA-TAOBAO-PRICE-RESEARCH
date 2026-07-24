#!/usr/bin/env python3
"""Shared deterministic runtime primitives for a graph-driven Skill."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


STATE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
NODE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EXECUTORS = {"script", "mcp", "browser-dom", "computer-use", "reasoning"}
SIDE_EFFECTS = {"none", "read", "write", "destructive"}
EXCLUDED_CORE_PARTS = {"__pycache__", ".DS_Store"}


class RuntimeErrorDetail(ValueError):
    """A user-facing deterministic runtime validation failure."""


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "VERSION").is_file() and (candidate / ".agents" / "skills").is_dir():
            return candidate
    raise RuntimeErrorDetail("Cannot locate the Skill repository root")


def find_skill_dir(root: Path) -> Path:
    candidates = sorted(
        path for path in (root / ".agents" / "skills").iterdir() if path.is_dir()
    )
    if len(candidates) != 1:
        raise RuntimeErrorDetail(
            f"Expected exactly one Skill in this repository, found {len(candidates)}"
        )
    return candidates[0]


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeErrorDetail(f"Required file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeErrorDetail(
            f"Invalid JSON-compatible YAML/JSON in {path}: line {exc.lineno}, column {exc.colno}"
        ) from exc


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_skill_path(skill_dir: Path, raw: Any, *, must_exist: bool = True) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeErrorDetail("Expected a non-empty relative path")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or ".." in pure.parts:
        raise RuntimeErrorDetail(f"Unsafe path outside Skill root: {raw}")
    resolved = (skill_dir / pure).resolve()
    try:
        resolved.relative_to(skill_dir.resolve())
    except ValueError as exc:
        raise RuntimeErrorDetail(f"Unsafe path outside Skill root: {raw}") from exc
    if must_exist and not resolved.is_file():
        raise RuntimeErrorDetail(f"Declared file does not exist: {raw}")
    return resolved


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate the stable JSON Schema subset used by this template."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    expected = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    if expected in type_map:
        valid = isinstance(value, type_map[expected])
        if expected in {"number", "integer"} and isinstance(value, bool):
            valid = False
        if not valid:
            return [f"{path}: expected {expected}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: value does not match const")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is not in enum")
    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list):
            errors.append(f"{path}: schema.required must be an array")
            required = []
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: required property is missing")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            errors.append(f"{path}: schema.properties must be an object")
            properties = {}
        for key, child in value.items():
            if key in properties:
                errors.extend(validate_schema(child, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: additional property is not allowed")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                errors.extend(validate_schema(child, item_schema, f"{path}[{index}]"))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength")
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], value):
                    errors.append(f"{path}: does not match pattern")
            except re.error:
                errors.append(f"{path}: schema pattern is invalid")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    return errors


def load_workflow(skill_dir: Path) -> dict[str, Any]:
    workflow = read_json(skill_dir / "workflow.yaml")
    if not isinstance(workflow, dict):
        raise RuntimeErrorDetail("workflow.yaml must contain an object")
    return workflow


def validate_workflow(
    workflow: dict[str, Any], skill_dir: Path, *, allow_draft: bool = False
) -> list[str]:
    errors: list[str] = []
    required_top = {"definition", "execution", "learning"}
    missing = sorted(required_top - set(workflow))
    if missing:
        errors.append(f"workflow missing top-level categories: {', '.join(missing)}")
    unclassified = sorted(set(workflow) - required_top)
    if unclassified:
        errors.append(
            "workflow contains unclassified top-level fields: " + ", ".join(unclassified)
        )

    definition = workflow.get("definition")
    if not isinstance(definition, dict):
        errors.append("workflow.definition must be an object")
        definition = {}
    required_definition = {"ir_version", "skill_name", "configured"}
    missing_definition = sorted(required_definition - set(definition))
    if missing_definition:
        errors.append(
            "workflow.definition missing fields: " + ", ".join(missing_definition)
        )
    extra_definition = sorted(set(definition) - required_definition)
    if extra_definition:
        errors.append(
            "workflow.definition contains unknown fields: " + ", ".join(extra_definition)
        )
    if definition.get("ir_version") != 2:
        errors.append("workflow.definition.ir_version must be 2")
    if definition.get("skill_name") != skill_dir.name:
        errors.append("workflow.definition.skill_name must match the Skill directory")
    if not isinstance(definition.get("configured"), bool):
        errors.append("workflow.definition.configured must be boolean")
    elif definition.get("configured") is False and not allow_draft:
        errors.append(
            "workflow is still a draft; set definition.configured=true only after domain design and tests"
        )

    execution = workflow.get("execution")
    if not isinstance(execution, dict):
        errors.append("workflow.execution must be an object")
        execution = {}
    required_execution = {"limits", "graph", "completion"}
    missing_execution = sorted(required_execution - set(execution))
    if missing_execution:
        errors.append(
            "workflow.execution missing categories: " + ", ".join(missing_execution)
        )
    extra_execution = sorted(set(execution) - required_execution)
    if extra_execution:
        errors.append(
            "workflow.execution contains unclassified fields: " + ", ".join(extra_execution)
        )

    limits = execution.get("limits")
    if not isinstance(limits, dict):
        errors.append("workflow.execution.limits must be an object")
        limits = {}
    required_limits = {"max_nodes", "total_timeout_seconds"}
    missing_limits = sorted(required_limits - set(limits))
    if missing_limits:
        errors.append(
            "workflow.execution.limits missing fields: " + ", ".join(missing_limits)
        )
    extra_limits = sorted(set(limits) - required_limits)
    if extra_limits:
        errors.append(
            "workflow.execution.limits contains unknown fields: " + ", ".join(extra_limits)
        )
    for key, lower, upper in (
        ("max_nodes", 1, 64),
        ("total_timeout_seconds", 1, 86400),
    ):
        value = limits.get(key)
        if not isinstance(value, int) or not lower <= value <= upper:
            errors.append(f"workflow.execution.limits.{key} must be {lower}-{upper}")

    learning = workflow.get("learning")
    if not isinstance(learning, dict):
        errors.append("workflow.learning must be an object")
        learning = {}
    required_learning = {"compaction"}
    missing_learning = sorted(required_learning - set(learning))
    if missing_learning:
        errors.append(
            "workflow.learning missing categories: " + ", ".join(missing_learning)
        )
    extra_learning = sorted(set(learning) - required_learning)
    if extra_learning:
        errors.append(
            "workflow.learning contains unclassified fields: " + ", ".join(extra_learning)
        )
    compaction = learning.get("compaction")
    if not isinstance(compaction, dict):
        errors.append("workflow.learning.compaction must be an object")
        compaction = {}
    required_compaction = {"compact_every", "active_rule_limit"}
    missing_compaction = sorted(required_compaction - set(compaction))
    if missing_compaction:
        errors.append(
            "workflow.learning.compaction missing fields: " + ", ".join(missing_compaction)
        )
    extra_compaction = sorted(set(compaction) - required_compaction)
    if extra_compaction:
        errors.append(
            "workflow.learning.compaction contains unknown fields: "
            + ", ".join(extra_compaction)
        )
    threshold = compaction.get("compact_every")
    active_limit = compaction.get("active_rule_limit")
    if not isinstance(threshold, int) or not 4 <= threshold <= 1000:
        errors.append("workflow.learning.compaction.compact_every must be 4-1000")
    if not isinstance(active_limit, int) or not 1 <= active_limit <= 500:
        errors.append("workflow.learning.compaction.active_rule_limit must be 1-500")
    if isinstance(threshold, int) and isinstance(active_limit, int) and active_limit > threshold // 2:
        errors.append("active_rule_limit must be at most half of compact_every")

    graph = execution.get("graph")
    if not isinstance(graph, dict):
        errors.append("workflow.execution.graph must be an object")
        graph = {}
    required_graph = {"entry_node", "nodes"}
    missing_graph = sorted(required_graph - set(graph))
    if missing_graph:
        errors.append(
            "workflow.execution.graph missing fields: " + ", ".join(missing_graph)
        )
    extra_graph = sorted(set(graph) - required_graph)
    if extra_graph:
        errors.append(
            "workflow.execution.graph contains unknown fields: " + ", ".join(extra_graph)
        )
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append("workflow.execution.graph.nodes must be a non-empty array")
        nodes = []
    if isinstance(limits.get("max_nodes"), int) and len(nodes) > limits["max_nodes"]:
        errors.append("workflow contains more nodes than execution.limits.max_nodes")
    node_map: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(nodes):
        label = f"execution.graph.nodes[{index}]"
        if not isinstance(node, dict):
            errors.append(f"{label} must be an object")
            continue
        required_node = {
            "id",
            "input_schema",
            "output_schema",
            "executor",
            "side_effect",
            "requires_confirmation",
            "timeout_seconds",
            "max_retries",
            "validator",
            "fallback",
            "on_success",
            "stop_conditions",
        }
        missing_node = sorted(required_node - set(node))
        if missing_node:
            errors.append(f"{label} missing fields: {', '.join(missing_node)}")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
            errors.append(f"{label}.id must use lowercase kebab-case")
            continue
        if node_id in node_map:
            errors.append(f"duplicate node id: {node_id}")
        node_map[node_id] = node
        executor = node.get("executor")
        if executor not in EXECUTORS:
            errors.append(f"{label}.executor must be one of {sorted(EXECUTORS)}")
        for schema_key in ("input_schema", "output_schema"):
            try:
                schema_path = safe_skill_path(skill_dir, node.get(schema_key))
                schema = read_json(schema_path)
                if not isinstance(schema, dict):
                    errors.append(f"{label}.{schema_key} must contain a JSON object")
            except RuntimeErrorDetail as exc:
                errors.append(f"{label}.{schema_key}: {exc}")
        if executor == "script":
            command = node.get("command")
            if not isinstance(command, list) or not command or not all(
                isinstance(token, str) and token for token in command
            ):
                errors.append(f"{label}.command must be a non-empty argv array")
            elif any("\n" in token or "\x00" in token for token in command):
                errors.append(f"{label}.command contains unsafe control characters")
        else:
            action = node.get("action")
            if not isinstance(action, dict):
                errors.append(f"{label}.action must be an object for non-script executors")
            else:
                action_name = action.get("name")
                if (
                    not isinstance(action_name, str)
                    or not action_name.strip()
                    or len(action_name) > 200
                    or "\n" in action_name
                ):
                    errors.append(f"{label}.action.name must be a non-empty one-line string")
                if not isinstance(action.get("arguments"), dict):
                    errors.append(f"{label}.action.arguments must be an object")
        side_effect = node.get("side_effect")
        if side_effect not in SIDE_EFFECTS:
            errors.append(f"{label}.side_effect is invalid")
        confirmation = node.get("requires_confirmation")
        if not isinstance(confirmation, bool):
            errors.append(f"{label}.requires_confirmation must be boolean")
        if side_effect in {"write", "destructive"} and confirmation is not True:
            errors.append(f"{label} write/destructive actions must require confirmation")
        timeout = node.get("timeout_seconds")
        if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            errors.append(f"{label}.timeout_seconds must be 1-3600")
        retries = node.get("max_retries")
        if not isinstance(retries, int) or not 0 <= retries <= 10:
            errors.append(f"{label}.max_retries must be 0-10")
        validator = node.get("validator")
        if validator is not None and (
            not isinstance(validator, list)
            or not validator
            or not all(isinstance(token, str) and token for token in validator)
        ):
            errors.append(f"{label}.validator must be null or an argv array")
        stop_conditions = node.get("stop_conditions")
        if (
            not isinstance(stop_conditions, list)
            or len(stop_conditions) > 16
            or not all(
                isinstance(condition, str) and condition.strip() and len(condition) <= 300
                for condition in stop_conditions
            )
        ):
            errors.append(
                f"{label}.stop_conditions must be an array of at most 16 non-empty strings"
            )

    entry = graph.get("entry_node")
    if entry not in node_map:
        errors.append("workflow.execution.graph.entry_node does not exist")
    for node_id, node in node_map.items():
        success = node.get("on_success")
        if not isinstance(success, str) or not success:
            errors.append(f"node {node_id}.on_success must name a node or __complete__")
        elif success != "__complete__" and success not in node_map:
            errors.append(f"node {node_id}.on_success points to missing node {success}")
        fallback = node.get("fallback")
        if fallback == "__complete__":
            errors.append(f"node {node_id}.fallback cannot complete without a valid node output")
        elif fallback is not None and (not isinstance(fallback, str) or fallback not in node_map):
            errors.append(f"node {node_id}.fallback points to missing node {fallback}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            errors.append(f"workflow cycle detected at node {node_id}; use bounded retries instead")
            return
        if node_id in visited or node_id not in node_map:
            return
        visiting.add(node_id)
        for edge in ("on_success", "fallback"):
            target = node_map[node_id].get(edge)
            if isinstance(target, str) and target != "__complete__":
                visit(target)
        visiting.remove(node_id)
        visited.add(node_id)

    if isinstance(entry, str):
        visit(entry)
    reachable = set(visited)
    unreachable = sorted(set(node_map) - reachable)
    if unreachable:
        errors.append(f"workflow contains unreachable nodes: {', '.join(unreachable)}")
    for node_id in sorted(node_map):
        visit(node_id)
    completion = execution.get("completion")
    if not isinstance(completion, dict):
        errors.append("workflow.execution.completion must be an object")
        completion = {}
    required_completion = {"output_schema", "validator"}
    missing_completion = sorted(required_completion - set(completion))
    if missing_completion:
        errors.append(
            "workflow.execution.completion missing fields: " + ", ".join(missing_completion)
        )
    extra_completion = sorted(set(completion) - required_completion)
    if extra_completion:
        errors.append(
            "workflow.execution.completion contains unknown fields: "
            + ", ".join(extra_completion)
        )
    try:
        safe_skill_path(skill_dir, completion.get("output_schema"))
    except RuntimeErrorDetail as exc:
        errors.append(f"workflow.execution.completion.output_schema: {exc}")
    final_validator = completion.get("validator")
    if final_validator is not None and (
        not isinstance(final_validator, list)
        or not final_validator
        or not all(isinstance(token, str) and token for token in final_validator)
    ):
        errors.append("workflow.execution.completion.validator must be null or an argv array")
    return errors


def core_files(root: Path, skill_dir: Path) -> list[Path]:
    files = [
        root / "AGENTS.md",
        root / "VERSION",
        root / "SECURITY.md",
        root / ".gitleaks.toml",
        root / ".pre-commit-config.yaml",
    ]
    for protected_root in (root / "scripts", root / ".github" / "workflows"):
        if protected_root.is_dir():
            files.extend(path for path in protected_root.rglob("*") if path.is_file() and not path.is_symlink())
    for path in skill_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative_parts = path.relative_to(skill_dir).parts
        if any(part in EXCLUDED_CORE_PARTS for part in relative_parts) or path.suffix == ".pyc":
            continue
        files.append(path)
    return sorted(path for path in set(files) if path.is_file())


def compute_core_manifest(root: Path, skill_dir: Path) -> dict[str, Any]:
    return {
        "format_version": 1,
        "skill_name": skill_dir.name,
        "skill_version": (root / "VERSION").read_text(encoding="utf-8").strip(),
        "files": {
            str(path.relative_to(root)): sha256_file(path) for path in core_files(root, skill_dir)
        },
    }


def verify_core_lock(root: Path, skill_dir: Path) -> list[str]:
    lock_path = root / ".core-lock.json"
    if not lock_path.is_file():
        return [".core-lock.json is missing; run freeze_core.py after tests"]
    expected = read_json(lock_path)
    actual = compute_core_manifest(root, skill_dir)
    if expected != actual:
        return ["stable core differs from .core-lock.json; review, test, bump VERSION, then freeze"]
    return []


def state_path(root: Path, state_id: str) -> Path:
    if not STATE_ID_RE.fullmatch(state_id):
        raise RuntimeErrorDetail("Invalid state id")
    return root / ".runtime" / "states" / f"{state_id}.json"


def load_state(root: Path, state_id: str) -> dict[str, Any]:
    state = read_json(state_path(root, state_id))
    if not isinstance(state, dict) or state.get("state_id") != state_id:
        raise RuntimeErrorDetail("State file is invalid")
    return state


def save_state(root: Path, state: dict[str, Any]) -> None:
    atomic_write_json(state_path(root, state["state_id"]), state)


def append_trace(state: dict[str, Any], event: str, **details: Any) -> None:
    state.setdefault("trace", []).append({"event": event, **details})


SECRET_PATTERNS = (
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile("-----BEGIN " + r"[A-Z0-9 ]*PRIVATE KEY-----"),
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
LONG_NUMBER_RE = re.compile(r"\b\d{8,}\b")


def sanitize_lesson(text: str) -> str:
    if not isinstance(text, str):
        raise RuntimeErrorDetail("Lesson must be a string")
    cleaned = " ".join(text.split())
    for pattern in SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted-secret]", cleaned)
    cleaned = EMAIL_RE.sub("[redacted-email]", cleaned)
    cleaned = URL_RE.sub("[redacted-url]", cleaned)
    cleaned = LONG_NUMBER_RE.sub("[redacted-number]", cleaned)
    if not 12 <= len(cleaned) <= 240:
        raise RuntimeErrorDetail("Sanitized lesson must be 12-240 characters")
    return cleaned


def normalized_lesson(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text.lower()).strip()


def load_active_rules(root: Path) -> list[dict[str, Any]]:
    path = root / "learning" / "active-rules.json"
    if not path.is_file():
        return []
    data = read_json(path)
    rules = data.get("rules", []) if isinstance(data, dict) else []
    return rules if isinstance(rules, list) else []
