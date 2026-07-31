from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


V4_ROOT = Path(
    os.getenv(
        "BAUER_V4_BENCHMARK_ROOT",
        r"D:\02_Code\LibreChat_Setup-rag-v4",
    )
)
V2_RUNNERS = V4_ROOT / "evals" / "bauer-rag-v2" / "runners"
sys.path.insert(0, str(V2_RUNNERS))

from common import DEFAULT_BROWSER_USER_AGENT, require_secret  # noqa: E402
import end_to_end  # noqa: E402


V1_AGENT_ID = "agent_Z8A2LtQWLeP4KuUDbvqZL"
V2_AGENT_ID = "agent_pmPMcA25UXS7vznUaz-DU"
PROTECTED_ARCHIVE_AGENT_ID = "agent_QnRNYPGlShuSnY0CYgQnm"
V3_CANDIDATE_BACKEND_COMMIT = "11ea00066300ed3ca5bf4ec75fd9d76a6d43dc59"
EXPECTED_CASE_IDS = tuple(f"B{index:02d}" for index in range(1, 31))
DEVELOPMENT_CASES = (
    V4_ROOT / "evals" / "bauer-rag-v2" / "cases" / "development.yaml"
)
PROVISION_STATE = Path(
    r"D:\02_Code\LibreChat_Setup\tmp\knowledge-base-provision-state.json"
)


class BenchmarkError(RuntimeError):
    pass


class RefreshableLibreChatSession:
    def __init__(
        self,
        *,
        base_origin: str,
        access_token: str,
        refresh_token_cookie: str,
    ) -> None:
        self.base_origin = base_origin
        self.access_token = access_token
        self.refresh_token_cookie = refresh_token_cookie
        self.refresh_count = 0

    @staticmethod
    def _expiry(access_token: str) -> int:
        try:
            encoded = access_token.split(".")[1]
            encoded += "=" * (-len(encoded) % 4)
            payload = json.loads(
                base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
            )
            expiry = int(payload["exp"])
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise BenchmarkError("LibreChat access token has no readable expiry")
        if expiry <= 0:
            raise BenchmarkError("LibreChat access token expiry is invalid")
        return expiry

    def _refresh(self) -> None:
        request = urllib.request.Request(
            f"{self.base_origin}/api/auth/refresh",
            data=b"",
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Cookie": f"refreshToken={self.refresh_token_cookie}",
                "User-Agent": DEFAULT_BROWSER_USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
                status = int(response.status)
                set_cookie_headers = response.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as error:
            error.read()
            raise BenchmarkError(
                f"LibreChat session refresh failed with HTTP {error.code}"
            )
        if status != 200:
            raise BenchmarkError(
                f"LibreChat session refresh failed with HTTP {status}"
            )
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
            access_token = str(payload.get("token") or "")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            raise BenchmarkError("LibreChat session refresh returned invalid JSON")
        if not access_token:
            raise BenchmarkError("LibreChat session refresh returned no bearer token")
        for header in set_cookie_headers:
            parsed = SimpleCookie()
            parsed.load(header)
            if "refreshToken" in parsed:
                updated = parsed["refreshToken"].value
                if updated:
                    self.refresh_token_cookie = updated
        self.access_token = access_token
        self.refresh_count += 1

    def token_for_observation(self, maximum_observation_seconds: float) -> str:
        minimum_lifetime = maximum_observation_seconds + 90
        if self._expiry(self.access_token) - time.time() < minimum_lifetime:
            self._refresh()
        return self.access_token

    def clear(self) -> None:
        self.access_token = ""
        self.refresh_token_cookie = ""


def _https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BenchmarkError(
            "--base-url must be an HTTPS origin without credentials, "
            "query, fragment, or path"
        )
    return urlunsplit(("https", parsed.netloc, "", "", ""))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_case_ids(value: str) -> tuple[str, ...]:
    identifiers = tuple(item.strip() for item in value.split(",") if item.strip())
    if (
        not identifiers
        or len(set(identifiers)) != len(identifiers)
        or any(item not in EXPECTED_CASE_IDS for item in identifiers)
    ):
        raise BenchmarkError("--case-ids must be unique public B01-B30 identifiers")
    return identifiers


def _parse_systems(value: str) -> tuple[str, ...]:
    identifiers = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    allowed = ("v1", "v2", "v3", "v4")
    if (
        not identifiers
        or len(set(identifiers)) != len(identifiers)
        or any(item not in allowed for item in identifiers)
    ):
        raise BenchmarkError("--systems must be unique values from v1,v2,v3,v4")
    return identifiers


def _parse_supplemental_source_ids(value: str) -> frozenset[str]:
    identifiers = tuple(item.strip() for item in value.split(",") if item.strip())
    if len(set(identifiers)) != len(identifiers) or any(
        len(item) > 256 for item in identifiers
    ):
        raise BenchmarkError("--v4-supplemental-source-ids is invalid")
    return frozenset(identifiers)


def _load_cases(
    path: Path,
    selected_case_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise BenchmarkError("development case file has no cases")
    identifiers = tuple(str(item.get("id") or "") for item in cases)
    if identifiers != EXPECTED_CASE_IDS:
        raise BenchmarkError("development cases must be exactly B01-B30 in order")
    for item in cases:
        if str(item.get("id") or "").startswith("H"):
            raise BenchmarkError("locked holdout case detected")
        if not str(item.get("prompt_en") or item.get("prompt_de") or "").strip():
            raise BenchmarkError(f"{item.get('id')} has no development prompt")
    by_id = {str(item["id"]): item for item in cases}
    return [by_id[item] for item in selected_case_ids]


def _bauer_file_ids(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    records = payload.get("uploaded", {}).get("bauer-kompressoren", {}).values()
    result = {
        str(record.get("fileId") or "").strip()
        for record in records
        if str(record.get("fileId") or "").strip()
    }
    if len(result) != 373:
        raise BenchmarkError("protected Bauer source scope is not exactly 373 files")
    return result


def _sanitize_evidence(
    evidence: Any,
    allowed_file_ids: set[str],
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(evidence, list):
        return [], 0
    allowed_fields = (
        "file_id",
        "filename",
        "content",
        "page",
        "citation_id",
        "source_type",
        "evidence_id",
        "tool_name",
    )
    result: list[dict[str, Any]] = []
    unauthorized = 0
    for item in evidence:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("file_id") or "").strip()
        if file_id and file_id not in allowed_file_ids:
            unauthorized += 1
            continue
        sanitized = {
            field: item.get(field)
            for field in allowed_fields
            if item.get(field) is not None
        }
        if sanitized:
            result.append(sanitized)
    return result, unauthorized


def _visible_assistant_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    text = str(message.get("text") or "").strip()
    if text:
        return text
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ).strip()


def _v4_boundary_metadata(value: Any, visible_answer: str) -> dict[str, Any]:
    envelopes: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict):
            return
        envelope = node.get("bauerV4")
        if isinstance(envelope, dict) and envelope.get("directFinal") is True:
            envelopes.append(envelope)
        elif isinstance(node.get("v4ValidationFingerprint"), str):
            envelopes.append(
                {
                    "directFinal": True,
                    "status": node.get("v4Status"),
                    "releaseId": node.get("releaseId"),
                    "validationPassed": node.get("v4ValidationPassed"),
                    "answerMode": node.get("v4AnswerMode"),
                    "validationFingerprint": node.get(
                        "v4ValidationFingerprint"
                    ),
                    "repairCount": node.get("v4RepairCount"),
                    "finalAnswerSha256": node.get("v4FinalAnswerSha256"),
                }
            )
        for child in node.values():
            visit(child)

    visit(value)
    if not envelopes:
        raise BenchmarkError("V4 response has no direct-final validation envelope")
    normalized = []
    for envelope in envelopes:
        answer = str(envelope.get("finalAnswer") or "").strip()
        fingerprint = str(envelope.get("validationFingerprint") or "").strip()
        repair_count = envelope.get("repairCount")
        answer_sha256 = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        final_answer_sha256 = str(
            envelope.get("finalAnswerSha256") or answer_sha256
        ).strip()
        item = {
            "status": str(envelope.get("status") or "").strip(),
            "release_id": str(envelope.get("releaseId") or "").strip(),
            "validation_passed": envelope.get("validationPassed") is True,
            "answer_mode": str(envelope.get("answerMode") or "").strip(),
            "validation_fingerprint": fingerprint,
            "repair_count": repair_count,
            "final_answer_sha256": final_answer_sha256,
        }
        if (
            final_answer_sha256
            != hashlib.sha256(visible_answer.encode("utf-8")).hexdigest()
            or item["status"] not in {"complete", "partial", "not_found"}
            or item["validation_passed"] is not True
            or item["answer_mode"]
            not in {"lossless_deterministic", "grounded_structured"}
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            or not isinstance(repair_count, int)
            or repair_count < 0
            or repair_count > 1
        ):
            raise BenchmarkError("V4 direct-final validation envelope is invalid")
        normalized.append(item)
    signatures = {
        json.dumps(item, sort_keys=True, separators=(",", ":"))
        for item in normalized
    }
    if len(signatures) != 1:
        raise BenchmarkError("parallel V4 validation envelopes differ")
    return {
        **normalized[0],
        "envelope_count": len(envelopes),
    }


def _write_replace(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise BenchmarkError(f"refusing to overwrite benchmark evidence: {path}")
    _write_replace(path, payload)


def _run_agent_case_with_verified_cleanup(**kwargs: Any) -> dict[str, Any]:
    cleanup_statuses: list[int] = []
    original_request_json = end_to_end.request_json

    def tracked_request_json(**request_kwargs: Any) -> tuple[int, Any, float]:
        response = original_request_json(**request_kwargs)
        if (
            str(request_kwargs.get("method") or "").upper() == "DELETE"
            and str(request_kwargs.get("url") or "").endswith("/api/convos")
        ):
            cleanup_statuses.append(int(response[0]))
        return response

    end_to_end.request_json = tracked_request_json
    try:
        result = end_to_end.run_agent_case(**kwargs)
    except Exception as error:
        cleanup_status = cleanup_statuses[0] if len(cleanup_statuses) == 1 else 0
        error.conversation_delete_http_status = cleanup_status
        error.conversation_deleted = cleanup_status in {200, 201, 204}
        raise
    finally:
        end_to_end.request_json = original_request_json
    if len(cleanup_statuses) != 1 or cleanup_statuses[0] not in {200, 201, 204}:
        raise BenchmarkError(
            "temporary LibreChat conversation deletion was not confirmed"
        )
    result["conversation_delete_http_status"] = cleanup_statuses[0]
    result["conversation_deleted"] = True
    return result


def _progress_payload(
    *,
    args: argparse.Namespace,
    base_origin: str,
    cases_sha256: str,
    observations: list[dict[str, Any]],
    session_refresh_count: int,
) -> dict[str, Any]:
    completed = sum(not item.get("error_type") for item in observations)
    systems = {
        "v1": {"agent_id": V1_AGENT_ID},
        "v2": {"agent_id": V2_AGENT_ID},
        "v3": {"agent_id": args.v3_agent_id},
        "v4": {"agent_id": args.v4_agent_id},
    }
    return {
        "schema_version": 1,
        "kind": "end_to_end",
        "run_id": "selected-librechat-development-answer-quality-20260731",
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "base_origin": base_origin,
        "systems": systems,
        "v3_agent_id": args.v3_agent_id,
        "v4_agent_id": args.v4_agent_id,
        "librechat_overlay_commit": args.librechat_overlay_commit,
        "v3_candidate_backend_commit": args.v3_candidate_backend_commit,
        "v4_candidate_backend_commit": args.v4_candidate_backend_commit,
        "development_cases_sha256": cases_sha256,
        "split": "development",
        "selected_case_ids": list(args.selected_case_ids),
        "selected_systems": list(args.selected_systems),
        "selection": {
            "method": "fixed seeded random public-development selection",
            "case_ids": list(args.selected_case_ids),
        },
        "v4_supplemental_source_ids": sorted(
            args.v4_supplemental_source_ids
        ),
        "expected_case_count": len(args.selected_case_ids),
        "expected_observation_count": (
            len(args.selected_case_ids) * len(args.selected_systems)
        ),
        "attempted_observation_count": len(observations),
        "completed_observation_count": completed,
        "error_observation_count": len(observations) - completed,
        "observations": observations,
        "execution": {
            "serial": True,
            "rotating_system_order": True,
            "system_order_cycle": [
                list(args.selected_systems[index:] + args.selected_systems[:index])
                for index in range(len(args.selected_systems))
            ],
        },
        "browser_shaped_http_api_client": True,
        "browser_ui_used": False,
        "login_attempt_count": 1,
        "authenticated_session_refresh_count": session_refresh_count,
        "bearer_refreshed_before_expiry": True,
        "fresh_conversation_per_query": True,
        "exact_answer_bodies_retained": True,
        "answer_extraction": "assistant text or assistant content text parts",
        "locked_holdout_opened": False,
        "private_shadow_only": True,
        "production_promotion": False,
        "credentials_or_tokens_emitted": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run a selected public-development case set through selected live "
            "LibreChat Agents and retain every exact answer."
        )
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--v3-agent-id", required=True)
    parser.add_argument("--v4-agent-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--librechat-overlay-commit", required=True)
    parser.add_argument(
        "--v3-candidate-backend-commit",
        default=V3_CANDIDATE_BACKEND_COMMIT,
    )
    parser.add_argument("--v4-candidate-backend-commit", required=True)
    parser.add_argument("--unit-test-count", required=True, type=int)
    parser.add_argument("--unit-test-evidence-sha256", required=True)
    parser.add_argument(
        "--case-ids",
        default=",".join(EXPECTED_CASE_IDS),
    )
    parser.add_argument("--systems", default="v1,v2,v3,v4")
    parser.add_argument("--v4-supplemental-source-ids", default="")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument(
        "--development-cases",
        type=Path,
        default=DEVELOPMENT_CASES,
    )
    parser.add_argument(
        "--provision-state",
        type=Path,
        default=PROVISION_STATE,
    )
    args = parser.parse_args()
    args.selected_case_ids = _parse_case_ids(args.case_ids)
    args.selected_systems = _parse_systems(args.systems)
    args.v4_supplemental_source_ids = _parse_supplemental_source_ids(
        args.v4_supplemental_source_ids
    )

    if re.fullmatch(r"agent_[A-Za-z0-9_-]+", args.v3_agent_id) is None:
        parser.error("--v3-agent-id is invalid")
    if re.fullmatch(r"agent_[A-Za-z0-9_-]+", args.v4_agent_id) is None:
        parser.error("--v4-agent-id is invalid")
    if args.v3_agent_id in {
        V1_AGENT_ID,
        V2_AGENT_ID,
        PROTECTED_ARCHIVE_AGENT_ID,
    }:
        raise BenchmarkError("V3 Agent ID reuses a protected Agent ID")
    if args.v4_agent_id in {
        V1_AGENT_ID,
        V2_AGENT_ID,
        args.v3_agent_id,
        PROTECTED_ARCHIVE_AGENT_ID,
    }:
        raise BenchmarkError("V4 Agent ID reuses a protected Agent ID")
    if (
        args.v3_candidate_backend_commit != V3_CANDIDATE_BACKEND_COMMIT
        or re.fullmatch(
            r"[0-9a-f]{40}",
            args.librechat_overlay_commit,
        )
        is None
        or re.fullmatch(
            r"[0-9a-f]{40}",
            args.v4_candidate_backend_commit,
        )
        is None
        or args.unit_test_count < 40
        or re.fullmatch(r"[0-9a-f]{64}", args.unit_test_evidence_sha256) is None
    ):
        raise BenchmarkError("deployed V3, V4, or LibreChat evidence seal is invalid")
    if not DEFAULT_BROWSER_USER_AGENT.startswith("Mozilla/5.0"):
        raise BenchmarkError("inherited LibreChat client is not browser-shaped")
    if args.timeout < 60 or args.timeout > 900:
        parser.error("--timeout must be between 60 and 900 seconds")
    if args.output.exists():
        raise BenchmarkError(f"refusing to overwrite benchmark evidence: {args.output}")

    base_origin = _https_origin(args.base_url)
    cases = _load_cases(args.development_cases, args.selected_case_ids)
    allowed_file_ids = _bauer_file_ids(args.provision_state)
    cases_sha256 = _sha256_file(args.development_cases)
    token = require_secret("LIBRECHAT_TOKEN")
    refresh_token_cookie = require_secret("LIBRECHAT_REFRESH_TOKEN_COOKIE")
    os.environ.pop("LIBRECHAT_TOKEN", None)
    os.environ.pop("LIBRECHAT_REFRESH_TOKEN_COOKIE", None)
    token_session = RefreshableLibreChatSession(
        base_origin=base_origin,
        access_token=token,
        refresh_token_cookie=refresh_token_cookie,
    )
    token = ""
    refresh_token_cookie = ""

    progress_path = args.output.with_suffix(args.output.suffix + ".progress.json")
    all_systems = (
        ("v1", V1_AGENT_ID),
        ("v2", V2_AGENT_ID),
        ("v3", args.v3_agent_id),
        ("v4", args.v4_agent_id),
    )
    systems = tuple(
        item for item in all_systems if item[0] in args.selected_systems
    )
    systems = tuple(
        next(item for item in systems if item[0] == system)
        for system in args.selected_systems
    )
    observations: list[dict[str, Any]] = []
    for case_index, case in enumerate(cases):
        case_id = str(case["id"])
        prompt = str(case.get("prompt_en") or case.get("prompt_de") or "").strip()
        rotation = case_index % len(systems)
        order = systems[rotation:] + systems[:rotation]
        for order_position, (system, agent_id) in enumerate(order, start=1):
            live: dict[str, Any] | None = None
            try:
                live = _run_agent_case_with_verified_cleanup(
                    base_url=base_origin,
                    token=token_session.token_for_observation(args.timeout),
                    agent_id=agent_id,
                    prompt=prompt,
                    timeout=args.timeout,
                    keep_conversation=False,
                )
                answer = _visible_assistant_text(live.get("assistant_message"))
                if not answer:
                    raise BenchmarkError(
                        "completed assistant message had no visible answer text"
                    )
                system_allowed_file_ids = allowed_file_ids
                if system == "v4":
                    system_allowed_file_ids = (
                        allowed_file_ids | args.v4_supplemental_source_ids
                    )
                boundary = (
                    _v4_boundary_metadata(live.get("messages"), answer)
                    if system == "v4"
                    else None
                )
                evidence, unauthorized = _sanitize_evidence(
                    live.get("evidence"),
                    system_allowed_file_ids,
                )
                observation = {
                    "case_id": case_id,
                    "split": "development",
                    "category": str(case.get("category") or ""),
                    "system": system,
                    "agent_id": agent_id,
                    "repetition": 1,
                    "case_execution_order": [item[0] for item in order],
                    "case_order_position": order_position,
                    "prompt": prompt,
                    "answer": answer,
                    "answer_sha256": hashlib.sha256(
                        answer.encode("utf-8")
                    ).hexdigest(),
                    "answer_character_count": len(answer),
                    "answer_extraction": (
                        "assistant_text_or_content_text_parts"
                    ),
                    "http_status": int(live.get("http_status") or 0),
                    "post_elapsed_ms": live.get("post_elapsed_ms"),
                    "elapsed_ms": live.get("elapsed_ms"),
                    "evidence": evidence,
                    "evidence_count": len(evidence),
                    "unauthorized_result_count": unauthorized,
                    "conversation_delete_http_status": int(
                        live.get("conversation_delete_http_status") or 0
                    ),
                    "conversation_deleted": bool(
                        live.get("conversation_deleted")
                    ),
                    "completed_at_utc": datetime.now(UTC).isoformat(),
                }
                if boundary is not None:
                    observation.update(
                        {
                            "status": boundary["status"],
                            "release_id": boundary["release_id"],
                            "validation": {
                                "passed": boundary["validation_passed"],
                                "answer_mode": boundary["answer_mode"],
                                "validation_fingerprint": boundary[
                                    "validation_fingerprint"
                                ],
                                "repair_count": boundary["repair_count"],
                            },
                            "direct_final_envelope_count": boundary[
                                "envelope_count"
                            ],
                        }
                    )
            except Exception as error:
                cleanup_status = int(
                    getattr(error, "conversation_delete_http_status", 0)
                    or (
                        live.get("conversation_delete_http_status", 0)
                        if live is not None
                        else 0
                    )
                )
                observation = {
                    "case_id": case_id,
                    "split": "development",
                    "category": str(case.get("category") or ""),
                    "system": system,
                    "agent_id": agent_id,
                    "repetition": 1,
                    "case_execution_order": [item[0] for item in order],
                    "case_order_position": order_position,
                    "prompt": prompt,
                    "answer": "",
                    "answer_sha256": hashlib.sha256(b"").hexdigest(),
                    "answer_character_count": 0,
                    "error_type": type(error).__name__,
                    "http_status": 0,
                    "evidence": [],
                    "evidence_count": 0,
                    "unauthorized_result_count": 0,
                    "conversation_delete_http_status": cleanup_status,
                    "conversation_deleted": bool(
                        getattr(error, "conversation_deleted", False)
                        or (
                            live.get("conversation_deleted", False)
                            if live is not None
                            else False
                        )
                    ),
                    "completed_at_utc": datetime.now(UTC).isoformat(),
                }
            observations.append(observation)
            _write_replace(
                progress_path,
                _progress_payload(
                    args=args,
                    base_origin=base_origin,
                    cases_sha256=cases_sha256,
                    observations=observations,
                    session_refresh_count=token_session.refresh_count,
                ),
            )

    payload = _progress_payload(
        args=args,
        base_origin=base_origin,
        cases_sha256=cases_sha256,
        observations=observations,
        session_refresh_count=token_session.refresh_count,
    )
    payload["completed_at_utc"] = datetime.now(UTC).isoformat()
    payload["all_observations_attempted"] = len(observations) == (
        len(args.selected_case_ids) * len(args.selected_systems)
    )
    payload["all_answers_captured"] = all(
        bool(item.get("answer")) and not item.get("error_type")
        for item in observations
    )
    payload["temporary_conversations_deleted"] = all(
        bool(item.get("conversation_deleted")) for item in observations
    )
    payload["authorization_violations"] = sum(
        int(item.get("unauthorized_result_count") or 0)
        for item in observations
    )
    _write_exclusive(args.output, payload)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "attempted_observation_count": (
                    payload["attempted_observation_count"]
                ),
                "completed_observation_count": (
                    payload["completed_observation_count"]
                ),
                "error_observation_count": payload["error_observation_count"],
                "all_answers_captured": payload["all_answers_captured"],
                "authenticated_session_refresh_count": (
                    payload["authenticated_session_refresh_count"]
                ),
                "credentials_or_tokens_emitted": False,
            },
            separators=(",", ":"),
        )
    )
    token_session.clear()


if __name__ == "__main__":
    main()
