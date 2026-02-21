#!/usr/bin/env python3
"""
OpenAPI Spec Comparison Tool

Compares the FastAPI-generated OpenAPI spec against the reference spec in realworld/specs/api/openapi.yml.
Provides output digestible for humans and CI/automation.

Usage:
    python compare_openapi.py [options]

Options:
    --server-url URL     Connect to existing server (default: auto-start)
    --reference PATH     Reference spec path (default: realworld/specs/api/openapi.yml)
    --format {text,json,markdown}  Output format (default: text)
    --strict             Treat warnings as errors
    --port PORT          Port for auto-started server (default: random available)
"""

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml


# Severity levels
CRITICAL = "critical"
WARNING = "warning"
INFO = "info"


class Difference:
    """Represents a difference between specs."""

    def __init__(self, severity: str, category: str, path: str, message: str, expected: Any = None, actual: Any = None):
        self.severity = severity
        self.category = category
        self.path = path
        self.message = message
        self.expected = expected
        self.actual = actual

    def to_dict(self) -> dict:
        result = {
            "severity": self.severity,
            "category": self.category,
            "path": self.path,
            "message": self.message,
        }
        if self.expected is not None:
            result["expected"] = self.expected
        if self.actual is not None:
            result["actual"] = self.actual
        return result


def find_available_port() -> int:
    """Find a random available port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def normalize_path(path: str) -> str:
    """Normalize path for comparison (handle parameter naming differences)."""
    import re

    return re.sub(r"\{[^}]+\}", "{param}", path)


def resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a $ref pointer in the spec."""
    if not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    current = spec
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return {}
    return current if isinstance(current, dict) else {}


def get_parameters_from_spec(spec: dict, params_list: list) -> list[dict]:
    """Extract and resolve parameters from a spec."""
    resolved = []
    for param in params_list:
        if "$ref" in param:
            resolved.append(resolve_ref(spec, param["$ref"]))
        else:
            resolved.append(param)
    return resolved


def compare_parameters(
    ref_spec: dict,
    actual_spec: dict,
    ref_params: list,
    actual_params: list,
    path: str,
    method: str,
) -> list[Difference]:
    """Compare parameters between specs."""
    differences = []

    ref_resolved = get_parameters_from_spec(ref_spec, ref_params)
    actual_resolved = get_parameters_from_spec(actual_spec, actual_params)

    ref_by_name = {p.get("name"): p for p in ref_resolved if p.get("name")}
    actual_by_name = {p.get("name"): p for p in actual_resolved if p.get("name")}

    # Check for missing required parameters
    for name, ref_param in ref_by_name.items():
        if name not in actual_by_name:
            is_required = ref_param.get("required", False)
            if ref_param.get("in") == "path":
                is_required = True
            severity = CRITICAL if is_required else WARNING
            differences.append(
                Difference(
                    severity=severity,
                    category="missing_parameter",
                    path=f"{method.upper()} {path}",
                    message=f"Missing {'required ' if is_required else ''}parameter: {name}",
                    expected=name,
                )
            )
        else:
            actual_param = actual_by_name[name]
            # Check parameter location
            if ref_param.get("in") != actual_param.get("in"):
                differences.append(
                    Difference(
                        severity=WARNING,
                        category="parameter_location",
                        path=f"{method.upper()} {path}",
                        message=f"Parameter '{name}' in wrong location",
                        expected=ref_param.get("in"),
                        actual=actual_param.get("in"),
                    )
                )
            # Check required flag for query params
            if ref_param.get("in") == "query":
                ref_required = ref_param.get("required", False)
                actual_required = actual_param.get("required", False)
                if ref_required and not actual_required:
                    differences.append(
                        Difference(
                            severity=WARNING,
                            category="parameter_required",
                            path=f"{method.upper()} {path}",
                            message=f"Parameter '{name}' should be required",
                            expected=True,
                            actual=False,
                        )
                    )

    # Check for extra parameters (info level)
    for name in actual_by_name:
        if name not in ref_by_name:
            differences.append(
                Difference(
                    severity=INFO,
                    category="extra_parameter",
                    path=f"{method.upper()} {path}",
                    message=f"Extra parameter not in reference spec: {name}",
                    actual=name,
                )
            )

    return differences


def compare_responses(ref_responses: dict, actual_responses: dict, path: str, method: str) -> list[Difference]:
    """Compare response codes between specs."""
    differences = []

    # Important response codes that should be present
    critical_codes = {"200", "201", "204"}

    for code in ref_responses:
        if code not in actual_responses:
            severity = CRITICAL if code in critical_codes else WARNING
            differences.append(
                Difference(
                    severity=severity,
                    category="missing_response_code",
                    path=f"{method.upper()} {path}",
                    message=f"Missing response code: {code}",
                    expected=code,
                )
            )

    # Extra response codes (info level)
    for code in actual_responses:
        if code not in ref_responses:
            differences.append(
                Difference(
                    severity=INFO,
                    category="extra_response_code",
                    path=f"{method.upper()} {path}",
                    message=f"Extra response code not in reference spec: {code}",
                    actual=code,
                )
            )

    return differences


def compare_operation(
    ref_spec: dict,
    actual_spec: dict,
    ref_op: dict,
    actual_op: dict,
    path: str,
    method: str,
) -> list[Difference]:
    """Compare a single operation between specs."""
    differences = []

    # Check operationId
    ref_op_id = ref_op.get("operationId")
    actual_op_id = actual_op.get("operationId")
    if ref_op_id and not actual_op_id:
        differences.append(
            Difference(
                severity=WARNING,
                category="missing_operation_id",
                path=f"{method.upper()} {path}",
                message="Missing operationId",
                expected=ref_op_id,
            )
        )
    elif ref_op_id and actual_op_id and ref_op_id != actual_op_id:
        differences.append(
            Difference(
                severity=INFO,
                category="different_operation_id",
                path=f"{method.upper()} {path}",
                message="Different operationId",
                expected=ref_op_id,
                actual=actual_op_id,
            )
        )

    # Check tags
    ref_tags = set(ref_op.get("tags", []))
    actual_tags = set(actual_op.get("tags", []))
    missing_tags = ref_tags - actual_tags
    for tag in missing_tags:
        differences.append(
            Difference(
                severity=WARNING,
                category="missing_tag",
                path=f"{method.upper()} {path}",
                message=f"Missing tag: {tag}",
                expected=tag,
            )
        )

    # Check parameters
    ref_params = ref_op.get("parameters", [])
    actual_params = actual_op.get("parameters", [])
    differences.extend(compare_parameters(ref_spec, actual_spec, ref_params, actual_params, path, method))

    # Check responses
    ref_responses = ref_op.get("responses", {})
    actual_responses = actual_op.get("responses", {})
    differences.extend(compare_responses(ref_responses, actual_responses, path, method))

    return differences


def strip_path_prefix(paths: dict, prefix: str) -> dict:
    """Strip a common prefix from all paths."""
    if not prefix:
        return paths
    prefix = prefix.rstrip("/")
    result = {}
    for path, value in paths.items():
        if path.startswith(prefix):
            new_path = path[len(prefix) :] or "/"
            result[new_path] = value
        else:
            result[path] = value
    return result


def detect_path_prefix(paths: dict) -> str:
    """Detect common path prefix from paths."""
    if not paths:
        return ""
    path_list = list(paths.keys())
    if not path_list:
        return ""

    # Find common prefix
    first = path_list[0]
    prefix_parts = first.split("/")

    for path in path_list[1:]:
        parts = path.split("/")
        common_len = 0
        for i, (a, b) in enumerate(zip(prefix_parts, parts)):
            if a == b and not a.startswith("{"):
                common_len = i + 1
            else:
                break
        prefix_parts = prefix_parts[:common_len]

    if not prefix_parts or prefix_parts == [""]:
        return ""
    return "/".join(prefix_parts)


def compare_specs(ref_spec: dict, actual_spec: dict, path_prefix: str = "") -> list[Difference]:
    """Compare two OpenAPI specs and return list of differences."""
    differences = []

    ref_paths = ref_spec.get("paths", {})
    actual_paths = actual_spec.get("paths", {})

    # Auto-detect and strip path prefix from actual spec
    if not path_prefix:
        path_prefix = detect_path_prefix(actual_paths)
    actual_paths = strip_path_prefix(actual_paths, path_prefix)

    # Build normalized path mappings
    ref_normalized = {normalize_path(p): p for p in ref_paths}
    actual_normalized = {normalize_path(p): p for p in actual_paths}

    # Check for missing endpoints
    for norm_path, ref_path in ref_normalized.items():
        if norm_path not in actual_normalized:
            differences.append(
                Difference(
                    severity=CRITICAL,
                    category="missing_endpoint",
                    path=ref_path,
                    message=f"Missing endpoint: {ref_path}",
                    expected=ref_path,
                )
            )
            continue

        actual_path = actual_normalized[norm_path]
        ref_methods = ref_paths[ref_path]
        actual_methods = actual_paths[actual_path]

        # Check methods for this endpoint
        for method in ref_methods:
            if method.startswith("x-"):
                continue
            if method not in actual_methods:
                differences.append(
                    Difference(
                        severity=CRITICAL,
                        category="missing_method",
                        path=ref_path,
                        message=f"Missing HTTP method: {method.upper()}",
                        expected=method.upper(),
                    )
                )
            else:
                ref_op = ref_methods[method]
                actual_op = actual_methods[method]
                differences.extend(compare_operation(ref_spec, actual_spec, ref_op, actual_op, ref_path, method))

        # Check for extra methods (info level)
        for method in actual_methods:
            if method.startswith("x-"):
                continue
            if method not in ref_methods:
                differences.append(
                    Difference(
                        severity=INFO,
                        category="extra_method",
                        path=actual_path,
                        message=f"Extra HTTP method not in reference spec: {method.upper()}",
                        actual=method.upper(),
                    )
                )

    # Check for extra endpoints (info level)
    for norm_path, actual_path in actual_normalized.items():
        if norm_path not in ref_normalized:
            differences.append(
                Difference(
                    severity=INFO,
                    category="extra_endpoint",
                    path=actual_path,
                    message=f"Extra endpoint not in reference spec: {actual_path}",
                    actual=actual_path,
                )
            )

    return differences


def format_text(differences: list[Difference], passed: bool) -> str:
    """Format differences as plain text."""
    lines = [
        "OpenAPI Spec Comparison",
        "=======================",
    ]

    critical_count = sum(1 for d in differences if d.severity == CRITICAL)
    warning_count = sum(1 for d in differences if d.severity == WARNING)
    info_count = sum(1 for d in differences if d.severity == INFO)

    status = "PASSED" if passed else "FAILED"
    lines.append(f"Status: {status}")
    lines.append(f"Critical: {critical_count} | Warnings: {warning_count} | Info: {info_count}")
    lines.append("")

    if critical_count > 0:
        lines.append("CRITICAL:")
        for d in differences:
            if d.severity == CRITICAL:
                msg = f"[C] {d.message}"
                if d.expected:
                    msg += f" (expected: {d.expected})"
                lines.append(msg)
        lines.append("")

    if warning_count > 0:
        lines.append("WARNINGS:")
        for d in differences:
            if d.severity == WARNING:
                msg = f"[W] {d.message} for {d.path}"
                if d.expected:
                    msg += f" (expected: {d.expected})"
                lines.append(msg)
        lines.append("")

    if info_count > 0:
        lines.append("INFO:")
        for d in differences:
            if d.severity == INFO:
                msg = f"[I] {d.message}"
                if d.actual:
                    msg += f" ({d.actual})"
                lines.append(msg)
        lines.append("")

    return "\n".join(lines)


def format_json(differences: list[Difference], passed: bool) -> str:
    """Format differences as JSON."""
    critical_count = sum(1 for d in differences if d.severity == CRITICAL)
    warning_count = sum(1 for d in differences if d.severity == WARNING)
    info_count = sum(1 for d in differences if d.severity == INFO)

    result = {
        "passed": passed,
        "summary": {
            "critical": critical_count,
            "warning": warning_count,
            "info": info_count,
        },
        "differences": [d.to_dict() for d in differences],
    }
    return json.dumps(result, indent=2)


def format_markdown(differences: list[Difference], passed: bool) -> str:
    """Format differences as Markdown."""
    lines = ["# OpenAPI Comparison Report", ""]

    critical_count = sum(1 for d in differences if d.severity == CRITICAL)
    warning_count = sum(1 for d in differences if d.severity == WARNING)
    info_count = sum(1 for d in differences if d.severity == INFO)

    status = "PASSED" if passed else "FAILED"
    lines.extend(
        [
            "## Summary",
            f"- **Status**: {status}",
            f"- **Critical**: {critical_count}",
            f"- **Warnings**: {warning_count}",
            f"- **Info**: {info_count}",
            "",
        ]
    )

    if critical_count > 0:
        lines.extend(["## Critical Issues", ""])
        for d in differences:
            if d.severity == CRITICAL:
                lines.append(f"### {d.category.replace('_', ' ').title()}")
                lines.append(f"- **Path**: {d.path}")
                lines.append(f"- **Message**: {d.message}")
                if d.expected:
                    lines.append(f"- **Expected**: {d.expected}")
                lines.append("")

    if warning_count > 0:
        lines.extend(["## Warnings", ""])
        for d in differences:
            if d.severity == WARNING:
                lines.append(f"### {d.category.replace('_', ' ').title()}")
                lines.append(f"- **Path**: {d.path}")
                lines.append(f"- **Message**: {d.message}")
                if d.expected:
                    lines.append(f"- **Expected**: {d.expected}")
                lines.append("")

    if info_count > 0:
        lines.extend(["## Info", ""])
        for d in differences:
            if d.severity == INFO:
                lines.append(f"- {d.message}")
                if d.path:
                    lines.append(f"  - Path: {d.path}")
        lines.append("")

    return "\n".join(lines)


def load_reference_spec(path: str) -> dict:
    """Load the reference OpenAPI spec from YAML file."""
    spec_path = Path(path)
    if not spec_path.exists():
        print(f"Error: Reference spec not found at {path}", file=sys.stderr)
        sys.exit(2)

    with open(spec_path) as f:
        return yaml.safe_load(f)


def fetch_actual_spec(url: str) -> dict:
    """Fetch the OpenAPI spec from a running server."""
    try:
        response = httpx.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as e:
        print(f"Error fetching OpenAPI spec: {e}", file=sys.stderr)
        sys.exit(2)


def start_server(port: int) -> subprocess.Popen:
    """Start the FastAPI server and return the process."""
    env = {
        "PATH_PREFIX": "/api",
        "DISABLE_ISOLATION_MODE": "True",
    }

    import os

    full_env = os.environ.copy()
    full_env.update(env)

    process = subprocess.Popen(
        [sys.executable, "realworld_dummy_server.py", str(port)],
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return process


def wait_for_server(url: str, timeout: int = 30) -> bool:
    """Wait for server to be ready."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code in (200, 404):
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def main():
    parser = argparse.ArgumentParser(description="Compare FastAPI OpenAPI spec against reference")
    parser.add_argument(
        "--server-url",
        help="Server base URL without /api prefix, e.g., http://127.0.0.1:8000 (default: auto-start)",
    )
    parser.add_argument(
        "--reference",
        default="realworld/specs/api/openapi.yml",
        help="Reference spec path (default: realworld/specs/api/openapi.yml)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "markdown"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    parser.add_argument("--port", type=int, help="Port for auto-started server (default: random available)")

    args = parser.parse_args()

    # Load reference spec
    ref_spec = load_reference_spec(args.reference)

    server_process = None
    try:
        if args.server_url:
            # Use existing server (user provides base URL, e.g., http://127.0.0.1:8000)
            base_url = args.server_url.rstrip("/")
            # OpenAPI spec is typically served at root, not under /api
            openapi_url = f"{base_url}/openapi.json"
        else:
            # Start server
            port = args.port or find_available_port()
            server_process = start_server(port)

            base_url = f"http://127.0.0.1:{port}/api"
            openapi_url = f"http://127.0.0.1:{port}/openapi.json"

            if not wait_for_server(f"http://127.0.0.1:{port}/api/tags"):
                print("Error: Server failed to start within timeout", file=sys.stderr)
                sys.exit(2)

        # Fetch actual spec
        actual_spec = fetch_actual_spec(openapi_url)

        # Compare specs
        differences = compare_specs(ref_spec, actual_spec)

        # Determine pass/fail
        critical_count = sum(1 for d in differences if d.severity == CRITICAL)
        warning_count = sum(1 for d in differences if d.severity == WARNING)

        if args.strict:
            passed = critical_count == 0 and warning_count == 0
        else:
            passed = critical_count == 0

        # Format output
        if args.format == "json":
            output = format_json(differences, passed)
        elif args.format == "markdown":
            output = format_markdown(differences, passed)
        else:
            output = format_text(differences, passed)

        print(output)

        # Exit code: 0=pass, 1=critical issues
        sys.exit(0 if passed else 1)

    finally:
        if server_process:
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()


if __name__ == "__main__":
    main()
