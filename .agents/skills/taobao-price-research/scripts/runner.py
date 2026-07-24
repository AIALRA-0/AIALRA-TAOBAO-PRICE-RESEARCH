#!/usr/bin/env python3
"""Deterministic state-machine runner for one Skill repository."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from runtime_lib import (
    RuntimeErrorDetail,
    append_trace,
    atomic_write_json,
    find_repo_root,
    find_skill_dir,
    load_active_rules,
    load_state,
    load_workflow,
    read_json,
    safe_skill_path,
    save_state,
    validate_schema,
    validate_workflow,
    verify_core_lock,
)


COMPLETE = "__complete__"
FAILURE_KINDS = ("retryable", "fallback", "user-required", "policy-blocked", "fatal")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def node_map(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in workflow["execution"]["graph"]["nodes"]}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def ensure_runtime_ready(root: Path, skill_dir: Path, workflow: dict[str, Any]) -> None:
    errors = validate_workflow(workflow, skill_dir, allow_draft=False)
    errors.extend(verify_core_lock(root, skill_dir))
    if errors:
        raise RuntimeErrorDetail("Runtime is not ready: " + "; ".join(errors))


def schema_for(skill_dir: Path, relative_path: str) -> dict[str, Any]:
    schema = read_json(safe_skill_path(skill_dir, relative_path))
    if not isinstance(schema, dict):
        raise RuntimeErrorDetail(f"Schema must contain an object: {relative_path}")
    return schema


def directive(
    root: Path, state: dict[str, Any], workflow: dict[str, Any]
) -> dict[str, Any]:
    nodes = node_map(workflow)
    node = nodes[state["current_node"]]
    return {
        "state_id": state["state_id"],
        "status": state["status"],
        "node": {
            "id": node["id"],
            "executor": node["executor"],
            "action": node.get("action"),
            "side_effect": node["side_effect"],
            "requires_confirmation": node["requires_confirmation"],
            "timeout_seconds": node["timeout_seconds"],
            "max_retries": node["max_retries"],
            "output_schema": node["output_schema"],
            "stop_conditions": node.get("stop_conditions", []),
        },
        "input": state["current_input"],
        "failure_context": state.get("last_error"),
        "failure_submission": (
            {
                "allowed_kinds": list(FAILURE_KINDS),
                "command_argv": [
                    "python3",
                    "scripts/runner.py",
                    "fail",
                    "--state-id",
                    state["state_id"],
                    "--node-id",
                    node["id"],
                    "--kind",
                    "<kind>",
                    "--message",
                    "<brief reason>",
                ],
            }
            if node["executor"] != "script"
            else None
        ),
        "advisory_rules": load_active_rules(root),
        "priority": "Stable core and user instructions override advisory learning rules.",
        "next_command": (
            "advance"
            if state["status"] == "running"
            else "obtain explicit user confirmation, then approve"
            if state["status"] == "waiting-confirmation"
            else "execute only the declared action, then use submit or failure_submission"
            if state["status"] == "waiting-external"
            else "follow the returned state without bypassing the runner"
        ),
    }


def materialize_node_io(
    root: Path, state: dict[str, Any], node: dict[str, Any]
) -> tuple[Path, Path]:
    attempt = state.get("retries", {}).get(node["id"], 0)
    directory = root / ".runtime" / "runs" / state["state_id"] / "nodes" / node["id"] / f"attempt-{attempt}"
    directory.mkdir(parents=True, exist_ok=True)
    input_file = directory / "input.json"
    output_file = directory / "output.json"
    atomic_write_json(input_file, state["current_input"])
    if output_file.exists():
        output_file.unlink()
    return input_file, output_file


def expand_command(command: list[str], values: dict[str, str]) -> list[str]:
    expanded: list[str] = []
    for token in command:
        result = token
        for key, value in values.items():
            result = result.replace(f"${{{key}}}", value)
        if "${" in result:
            raise RuntimeErrorDetail(f"Unknown command placeholder: {result}")
        expanded.append(result)
    return expanded


def run_argv(
    argv: list[str], skill_dir: Path, timeout_seconds: int
) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            argv,
            cwd=skill_dir,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "")[-1000:], "command timed out"
    except OSError as exc:
        return 126, "", f"command could not start: {exc}"
    return result.returncode, result.stdout[-1000:], result.stderr[-1000:]


def run_validator(
    validator: list[str] | None,
    skill_dir: Path,
    input_file: Path,
    output_file: Path,
    timeout_seconds: int,
) -> str | None:
    if validator is None:
        return None
    argv = expand_command(
        validator,
        {
            "input_file": str(input_file),
            "output_file": str(output_file),
            "skill_dir": str(skill_dir),
        },
    )
    code, _, stderr = run_argv(argv, skill_dir, timeout_seconds)
    return None if code == 0 else f"validator failed with exit {code}: {stderr}"


def final_validation(
    state: dict[str, Any], workflow: dict[str, Any], skill_dir: Path, output_file: Path
) -> list[str]:
    output = state["current_input"]
    completion = workflow["execution"]["completion"]
    errors = validate_schema(output, schema_for(skill_dir, completion["output_schema"]))
    if errors:
        return errors
    validator = completion.get("validator")
    message = run_validator(
        validator,
        skill_dir,
        output_file,
        output_file,
        workflow["execution"]["limits"]["total_timeout_seconds"],
    )
    return [message] if message else []


def accept_output(
    root: Path,
    skill_dir: Path,
    workflow: dict[str, Any],
    state: dict[str, Any],
    node: dict[str, Any],
    output: Any,
    input_file: Path,
    output_file: Path,
) -> None:
    schema_errors = validate_schema(output, schema_for(skill_dir, node["output_schema"]))
    if schema_errors:
        raise RuntimeErrorDetail("Output schema validation failed: " + "; ".join(schema_errors))
    atomic_write_json(output_file, output)
    validator_error = run_validator(
        node.get("validator"), skill_dir, input_file, output_file, node["timeout_seconds"]
    )
    if validator_error:
        raise RuntimeErrorDetail(validator_error)
    state.setdefault("node_results", {})[node["id"]] = output
    state["current_input"] = output
    state["last_error"] = None
    state["steps_executed"] += 1
    append_trace(state, "node-completed", node_id=node["id"], at=now_iso())
    target = node.get("on_success")
    if target == COMPLETE:
        final_errors = final_validation(state, workflow, skill_dir, output_file)
        if final_errors:
            raise RuntimeErrorDetail("Final validation failed: " + "; ".join(final_errors))
        state["status"] = "completed"
        state["final_output"] = output
        state["completed_at"] = now_iso()
        append_trace(state, "workflow-completed", at=state["completed_at"])
    else:
        state["current_node"] = target
        state["status"] = "running"


def handle_failure(
    state: dict[str, Any], node: dict[str, Any], kind: str, message: str
) -> None:
    state["last_error"] = {"node_id": node["id"], "kind": kind, "message": message[:1000]}
    append_trace(state, "node-failed", node_id=node["id"], kind=kind, at=now_iso())
    retries = state.setdefault("retries", {}).get(node["id"], 0)
    if kind == "retryable" and retries < node["max_retries"]:
        state["retries"][node["id"]] = retries + 1
        state["status"] = "running"
        return
    fallback = node.get("fallback")
    if kind in {"retryable", "fallback", "policy-blocked"} and fallback:
        state["current_node"] = fallback
        state["status"] = "running"
        return
    if kind == "user-required":
        state["status"] = "waiting-user"
        return
    state["status"] = "failed"
    state["failed_at"] = now_iso()


def check_limits(state: dict[str, Any], workflow: dict[str, Any]) -> None:
    limits = workflow["execution"]["limits"]
    if state["steps_executed"] >= limits["max_nodes"]:
        raise RuntimeErrorDetail("Workflow exceeded execution.limits.max_nodes")
    started = dt.datetime.fromisoformat(state["started_at"])
    elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
    if elapsed > limits["total_timeout_seconds"]:
        raise RuntimeErrorDetail("Workflow exceeded execution.limits.total_timeout_seconds")


def start(args: argparse.Namespace) -> dict[str, Any]:
    root = find_repo_root()
    skill_dir = find_skill_dir(root)
    workflow = load_workflow(skill_dir)
    ensure_runtime_ready(root, skill_dir, workflow)
    initial = read_json(args.input.resolve())
    definition = workflow["definition"]
    graph = workflow["execution"]["graph"]
    entry = node_map(workflow)[graph["entry_node"]]
    errors = validate_schema(initial, schema_for(skill_dir, entry["input_schema"]))
    if errors:
        raise RuntimeErrorDetail("Initial input is invalid: " + "; ".join(errors))
    state = {
        "state_id": uuid.uuid4().hex,
        "skill_name": skill_dir.name,
        "workflow_ir_version": definition["ir_version"],
        "status": "running",
        "current_node": graph["entry_node"],
        "initial_input": initial,
        "current_input": initial,
        "node_results": {},
        "retries": {},
        "approved_nodes": [],
        "steps_executed": 0,
        "last_error": None,
        "started_at": now_iso(),
        "trace": [],
    }
    append_trace(state, "workflow-started", node_id=state["current_node"], at=state["started_at"])
    save_state(root, state)
    return directive(root, state, workflow)


def advance(args: argparse.Namespace) -> dict[str, Any]:
    root = find_repo_root()
    skill_dir = find_skill_dir(root)
    workflow = load_workflow(skill_dir)
    ensure_runtime_ready(root, skill_dir, workflow)
    state = load_state(root, args.state_id)
    nodes = node_map(workflow)
    while state["status"] == "running":
        check_limits(state, workflow)
        node = nodes[state["current_node"]]
        input_errors = validate_schema(
            state["current_input"], schema_for(skill_dir, node["input_schema"])
        )
        if input_errors:
            handle_failure(
                state,
                node,
                "fatal",
                "Current node input is invalid: " + "; ".join(input_errors),
            )
            save_state(root, state)
            return {
                "state_id": state["state_id"],
                "status": state["status"],
                "error": state["last_error"],
            }
        if node["requires_confirmation"] and node["id"] not in state["approved_nodes"]:
            state["status"] = "waiting-confirmation"
            state["waiting_since"] = now_iso()
            save_state(root, state)
            return directive(root, state, workflow)
        if node["executor"] != "script":
            state["status"] = "waiting-external"
            state["waiting_since"] = now_iso()
            save_state(root, state)
            return directive(root, state, workflow)
        input_file, output_file = materialize_node_io(root, state, node)
        argv = expand_command(
            node["command"],
            {
                "input_file": str(input_file),
                "output_file": str(output_file),
                "skill_dir": str(skill_dir),
            },
        )
        code, stdout, stderr = run_argv(argv, skill_dir, node["timeout_seconds"])
        if code != 0 or not output_file.is_file():
            message = f"script exit={code}; stdout={stdout}; stderr={stderr}"
            handle_failure(state, node, "retryable", message)
            save_state(root, state)
            if state["status"] != "running":
                return {"state_id": state["state_id"], "status": state["status"], "error": state["last_error"]}
            continue
        try:
            output = read_json(output_file)
            accept_output(root, skill_dir, workflow, state, node, output, input_file, output_file)
        except RuntimeErrorDetail as exc:
            handle_failure(state, node, "retryable", str(exc))
        save_state(root, state)
    if state["status"] == "completed":
        return {
            "state_id": state["state_id"],
            "status": "completed",
            "final_output": state["final_output"],
            "learning_required": True,
        }
    return {"state_id": state["state_id"], "status": state["status"], "error": state.get("last_error")}


def submit(args: argparse.Namespace) -> dict[str, Any]:
    root = find_repo_root()
    skill_dir = find_skill_dir(root)
    workflow = load_workflow(skill_dir)
    ensure_runtime_ready(root, skill_dir, workflow)
    state = load_state(root, args.state_id)
    check_limits(state, workflow)
    if state["status"] != "waiting-external" or state["current_node"] != args.node_id:
        raise RuntimeErrorDetail("State is not waiting for output from this node")
    node = node_map(workflow)[args.node_id]
    waiting_since = dt.datetime.fromisoformat(state.get("waiting_since", state["started_at"]))
    external_elapsed = (dt.datetime.now(dt.timezone.utc) - waiting_since).total_seconds()
    if external_elapsed > node["timeout_seconds"]:
        handle_failure(state, node, "retryable", "external node exceeded timeout_seconds")
        save_state(root, state)
        return directive(root, state, workflow) if state["status"] == "running" else {
            "state_id": state["state_id"],
            "status": state["status"],
            "error": state.get("last_error"),
        }
    input_file, output_file = materialize_node_io(root, state, node)
    try:
        output = read_json(args.output.resolve())
        accept_output(root, skill_dir, workflow, state, node, output, input_file, output_file)
    except RuntimeErrorDetail as exc:
        handle_failure(state, node, "retryable", str(exc))
    save_state(root, state)
    if state["status"] == "completed":
        return {
            "state_id": state["state_id"],
            "status": "completed",
            "final_output": state["final_output"],
            "learning_required": True,
        }
    if state["status"] == "running":
        return directive(root, state, workflow)
    return {"state_id": state["state_id"], "status": state["status"], "error": state.get("last_error")}


def fail(args: argparse.Namespace) -> dict[str, Any]:
    root = find_repo_root()
    skill_dir = find_skill_dir(root)
    workflow = load_workflow(skill_dir)
    ensure_runtime_ready(root, skill_dir, workflow)
    state = load_state(root, args.state_id)
    check_limits(state, workflow)
    if state["status"] != "waiting-external" or state["current_node"] != args.node_id:
        raise RuntimeErrorDetail("State is not waiting on this external node")
    node = node_map(workflow)[args.node_id]
    handle_failure(state, node, args.kind, args.message)
    save_state(root, state)
    return directive(root, state, workflow) if state["status"] == "running" else {
        "state_id": state["state_id"],
        "status": state["status"],
        "error": state.get("last_error"),
    }


def approve(args: argparse.Namespace) -> dict[str, Any]:
    root = find_repo_root()
    skill_dir = find_skill_dir(root)
    workflow = load_workflow(skill_dir)
    ensure_runtime_ready(root, skill_dir, workflow)
    state = load_state(root, args.state_id)
    check_limits(state, workflow)
    if state["status"] != "waiting-confirmation" or state["current_node"] != args.node_id:
        raise RuntimeErrorDetail("State is not waiting for confirmation on this node")
    if args.node_id not in state["approved_nodes"]:
        state["approved_nodes"].append(args.node_id)
    state["status"] = "running"
    append_trace(state, "node-approved", node_id=args.node_id, at=now_iso())
    save_state(root, state)
    return directive(root, state, workflow)


def resume(args: argparse.Namespace) -> dict[str, Any]:
    root = find_repo_root()
    skill_dir = find_skill_dir(root)
    workflow = load_workflow(skill_dir)
    ensure_runtime_ready(root, skill_dir, workflow)
    state = load_state(root, args.state_id)
    check_limits(state, workflow)
    if state["status"] != "waiting-user":
        raise RuntimeErrorDetail("State is not waiting for the user")
    state["status"] = "running"
    append_trace(state, "user-resumed", at=now_iso())
    save_state(root, state)
    return directive(root, state, workflow)


def status(args: argparse.Namespace) -> dict[str, Any]:
    root = find_repo_root()
    state = load_state(root, args.state_id)
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--input", required=True, type=Path)
    start_parser.set_defaults(handler=start)
    advance_parser = subparsers.add_parser("advance")
    advance_parser.add_argument("--state-id", required=True)
    advance_parser.set_defaults(handler=advance)
    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("--state-id", required=True)
    submit_parser.add_argument("--node-id", required=True)
    submit_parser.add_argument("--output", required=True, type=Path)
    submit_parser.set_defaults(handler=submit)
    fail_parser = subparsers.add_parser("fail")
    fail_parser.add_argument("--state-id", required=True)
    fail_parser.add_argument("--node-id", required=True)
    fail_parser.add_argument(
        "--kind", required=True, choices=FAILURE_KINDS
    )
    fail_parser.add_argument("--message", required=True)
    fail_parser.set_defaults(handler=fail)
    approve_parser = subparsers.add_parser("approve")
    approve_parser.add_argument("--state-id", required=True)
    approve_parser.add_argument("--node-id", required=True)
    approve_parser.set_defaults(handler=approve)
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--state-id", required=True)
    resume_parser.set_defaults(handler=resume)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--state-id", required=True)
    status_parser.set_defaults(handler=status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        started = time.monotonic()
        payload = args.handler(args)
        payload["runner_elapsed_ms"] = round((time.monotonic() - started) * 1000)
        emit(payload)
        return 0 if payload.get("status") not in {"failed"} else 1
    except RuntimeErrorDetail as exc:
        emit({"status": "error", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
