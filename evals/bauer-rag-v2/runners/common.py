from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


EVAL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EVAL_ROOT.parents[1]
DEFAULT_TOKEN_ENV = "RAG_API_TOKEN"
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_revision() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_cases(split: str, acknowledge_holdout: bool = False) -> list[dict[str, Any]]:
    if split in {"holdout", "all"} and not acknowledge_holdout:
        raise ValueError(
            "The tuning-locked holdout requires --acknowledge-locked-holdout."
        )
    names = ["development", "holdout"] if split == "all" else [split]
    cases: list[dict[str, Any]] = []
    for name in names:
        payload = load_json(EVAL_ROOT / "cases" / f"{name}.yaml")
        for case in payload["cases"]:
            cases.append({**case, "split": name})
    return cases


def load_manifest() -> dict[str, Any]:
    return load_json(EVAL_ROOT / "baselines" / "corpus-manifest.json")


def case_prompt(case: dict[str, Any]) -> str:
    for field in ("prompt_en", "prompt_de"):
        value = case.get(field)
        if isinstance(value, str) and value.strip():
            return value
    raise ValueError(f"Evaluation case {case.get('id', '<unknown>')} has no prompt")


def create_run_directory(run_id: str) -> Path:
    if not run_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in run_id):
        raise ValueError("run_id may contain only letters, digits, '-' and '_'.")
    target = EVAL_ROOT / "raw-runs" / run_id
    target.mkdir(parents=True, exist_ok=False)
    return target


def write_json_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def write_run_metadata(run_dir: Path, run: dict[str, Any]) -> None:
    write_json_exclusive(
        run_dir / "metadata.json",
        {key: value for key, value in run.items() if key != "observations"},
    )


def write_observation(
    run_dir: Path,
    ordinal: int,
    observation: dict[str, Any],
) -> None:
    label = "-".join(
        (
            str(observation.get("case_id") or "case"),
            str(observation.get("system") or "system"),
            f"r{observation.get('repetition') or 1}",
        )
    )
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", label)
    write_json_exclusive(
        run_dir / "observations" / f"{ordinal:05d}-{safe_label}.json",
        observation,
    )


def request_json(
    *,
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 120,
) -> tuple[int, Any, float]:
    body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    # LibreChat deliberately rejects authenticated non-browser clients and assigns
    # the default violation score needed for an immediate temporary ban. Evaluation
    # traffic therefore uses the same browser-shaped identity as the provisioning
    # scripts while remaining identifiable through its immutable run metadata.
    headers = {
        "Accept": "application/json",
        "User-Agent": DEFAULT_BROWSER_USER_AGENT,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    try:
        decoded = json.loads(raw.decode("utf-8-sig")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = {"unparsed_body": raw.decode("utf-8", errors="replace")[:10_000]}
    return status, decoded, elapsed_ms


def require_secret(env_name: str) -> str:
    value = os.getenv(env_name, "")
    if not value:
        raise ValueError(f"Required credential environment variable is unset: {env_name}")
    return value


def normalize_v1(payload: Any, allowed_file_ids: set[str]) -> tuple[list[dict[str, Any]], int]:
    results: list[dict[str, Any]] = []
    unauthorized = 0
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, list) or len(item) < 2 or not isinstance(item[0], dict):
            continue
        document, distance = item[0], item[1]
        metadata = document.get("metadata") or {}
        file_id = str(metadata.get("file_id") or "")
        if file_id not in allowed_file_ids:
            unauthorized += 1
            continue
        source = str(metadata.get("source") or "")
        results.append(
            {
                "file_id": file_id,
                "filename": source.replace("\\", "/").rsplit("/", 1)[-1],
                "content": str(document.get("page_content") or ""),
                "page": metadata.get("page"),
                "section": metadata.get("section") or [],
                "score": 1 - float(distance),
                "route": "v1",
            }
        )
    return results, unauthorized


def normalize_v2(payload: Any, allowed_file_ids: set[str]) -> tuple[list[dict[str, Any]], int]:
    results: list[dict[str, Any]] = []
    unauthorized = 0
    source = payload.get("results", []) if isinstance(payload, dict) else []
    for item in source:
        if not isinstance(item, dict):
            continue
        if str(item.get("file_id") or "") not in allowed_file_ids:
            unauthorized += 1
            continue
        results.append({**item, "route": "v2"})
    return results, unauthorized


def extract_text(value: Any) -> str:
    """Best-effort extraction from LibreChat and OpenAI-compatible response shapes."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (extract_text(item) for item in value)))
    if not isinstance(value, dict):
        return ""
    for key in ("text", "content", "answer"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    choices = value.get("choices")
    if isinstance(choices, list) and choices:
        return extract_text(choices[0].get("message", {}))
    return "\n".join(
        filter(
            None,
            (
                extract_text(child)
                for key, child in value.items()
                if key not in {"metadata", "tokenUsage", "usage"}
            ),
        )
    )


def collect_evidence(value: Any) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict):
            return
        tool_call = node.get("tool_call")
        if node.get("type") == "tool_call" and isinstance(tool_call, dict):
            output = tool_call.get("output")
            if isinstance(output, str) and output.strip():
                evidence.append(
                    {
                        "evidence_id": str(tool_call.get("id") or ""),
                        "tool_name": str(tool_call.get("name") or ""),
                        "content": output,
                        "source_type": (
                            "synthetic_demo"
                            if "bauer_synthetic_demo" in output
                            else "tool_output"
                        ),
                    }
                )
        file_id = node.get("file_id") or node.get("fileId")
        content = node.get("content") or node.get("page_content")
        if file_id and content:
            metadata = node.get("metadata") or {}
            evidence.append(
                {
                    "file_id": str(file_id),
                    "filename": node.get("filename") or node.get("fileName"),
                    "content": str(content),
                    "page": node.get("page"),
                    "citation_id": node.get("citation_id") or metadata.get("citationId"),
                    "source_type": node.get("source_type") or metadata.get("sourceType"),
                }
            )
        for child in node.values():
            visit(child)

    visit(value)
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in evidence:
        key = (
            str(item.get("file_id") or item.get("evidence_id") or ""),
            str(item.get("page") or ""),
            hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
        )
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def run_metadata(kind: str, run_id: str, extra: dict[str, Any]) -> dict[str, Any]:
    manifest_path = EVAL_ROOT / "baselines" / "corpus-manifest.json"
    return {
        "schema_version": 1,
        "kind": kind,
        "run_id": run_id,
        "started_at": utc_now(),
        "git_commit": git_revision(),
        "corpus_manifest_sha256": sha256_file(manifest_path),
        **extra,
    }


def percentile(values: Iterable[float], percent: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percent
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 2)
