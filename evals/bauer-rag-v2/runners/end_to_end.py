from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    collect_evidence,
    create_run_directory,
    extract_text,
    load_cases,
    load_manifest,
    request_json,
    require_secret,
    run_metadata,
    utc_now,
    write_json_exclusive,
    write_observation,
    write_run_metadata,
)


def newest_assistant_message(messages: Any) -> dict[str, Any] | None:
    if not isinstance(messages, list):
        return None
    candidates = []
    for message in messages:
        if not isinstance(message, dict) or message.get("isCreatedByUser") is not False:
            continue
        final_text = str(message.get("text") or "").strip()
        content = message.get("content")
        if not final_text and isinstance(content, list):
            final_text = "\n".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
        if final_text:
            candidates.append(message)
    return candidates[-1] if candidates else None


def run_agent_case(
    *,
    base_url: str,
    token: str,
    agent_id: str,
    prompt: str,
    timeout: float,
    keep_conversation: bool,
) -> dict[str, Any]:
    message_id = str(uuid.uuid4())
    payload = {
        "text": prompt,
        "endpoint": "agents",
        "agent_id": agent_id,
        "conversationId": None,
        "messageId": message_id,
        "parentMessageId": "00000000-0000-0000-0000-000000000000",
        "responseMessageId": None,
        "sender": "User",
        "isCreatedByUser": True,
        "isTemporary": False,
        "isRegenerate": False,
        "clientTimestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "timezone": "Europe/Berlin",
    }
    started_at = time.perf_counter()
    status, started, post_ms = request_json(
        method="POST",
        url=f"{base_url.rstrip('/')}/api/agents/chat/agents",
        token=token,
        payload=payload,
        timeout=min(timeout, 120),
    )
    if status not in {200, 201} or not isinstance(started, dict):
        raise RuntimeError(f"Agent start failed with HTTP {status}: {started}")
    conversation_id = str(started.get("conversationId") or "")
    if not conversation_id:
        raise RuntimeError("Agent start did not return conversationId")

    messages: Any = []
    assistant: dict[str, Any] | None = None
    try:
        while time.perf_counter() - started_at < timeout:
            poll_status, messages, _ = request_json(
                method="GET",
                url=f"{base_url.rstrip('/')}/api/messages/{conversation_id}",
                token=token,
                timeout=min(timeout, 120),
            )
            if poll_status == 200:
                assistant = newest_assistant_message(messages)
                if assistant is not None:
                    break
            elif poll_status not in {502, 503, 504}:
                raise RuntimeError(f"Message poll failed with HTTP {poll_status}: {messages}")
            time.sleep(2)
        if assistant is None:
            raise TimeoutError(f"No completed Agent response after {timeout} seconds")
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        return {
            "http_status": status,
            "post_elapsed_ms": post_ms,
            "elapsed_ms": elapsed_ms,
            "conversation_id": conversation_id,
            "answer": extract_text(assistant),
            "evidence": collect_evidence(messages),
            "assistant_message": assistant,
            "messages": messages,
        }
    finally:
        if not keep_conversation:
            request_json(
                method="DELETE",
                url=f"{base_url.rstrip('/')}/api/convos",
                token=token,
                payload={"arg": {"conversationId": conversation_id}},
                timeout=60,
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run fresh-conversation, serial V1/V2 LibreChat Agent evaluations."
    )
    parser.add_argument("--base-url", default=os.getenv("LIBRECHAT_URL", ""))
    parser.add_argument("--token-env", default="LIBRECHAT_TOKEN")
    parser.add_argument("--v1-agent-id", required=True)
    parser.add_argument("--v2-agent-id", required=True)
    parser.add_argument("--split", choices=("development", "holdout", "all"), default="development")
    parser.add_argument("--acknowledge-locked-holdout", action="store_true")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--keep-conversations", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    if not args.base_url:
        parser.error("--base-url or LIBRECHAT_URL is required")
    if not 1 <= args.repetitions <= 20:
        parser.error("--repetitions must be between 1 and 20")
    try:
        token = require_secret(args.token_env)
        cases = load_cases(args.split, args.acknowledge_locked_holdout)
    except ValueError as error:
        parser.error(str(error))

    manifest = load_manifest()
    run_dir = create_run_directory(args.run_id)
    run = run_metadata(
        "end_to_end",
        args.run_id,
        {
            "split": args.split,
            "repetitions": args.repetitions,
            "serial_execution": True,
            "fresh_conversation_per_observation": True,
            "alternating_order": True,
            "base_url": args.base_url,
            "systems": {
                "v1": {"agent_id": args.v1_agent_id},
                "v2": {"agent_id": args.v2_agent_id},
            },
            "file_count": manifest["file_count"],
            "observations": [],
        },
    )
    write_run_metadata(run_dir, run)

    for case_index, case in enumerate(cases):
        for repetition in range(1, args.repetitions + 1):
            order = ("v1", "v2") if (case_index + repetition) % 2 else ("v2", "v1")
            for sequence, system in enumerate(order, start=1):
                agent_id = args.v1_agent_id if system == "v1" else args.v2_agent_id
                try:
                    observation = run_agent_case(
                        base_url=args.base_url,
                        token=token,
                        agent_id=agent_id,
                        prompt=case["prompt_en"],
                        timeout=args.timeout,
                        keep_conversation=args.keep_conversations,
                    )
                except Exception as error:
                    observation = {
                        "error": f"{type(error).__name__}: {error}",
                        "answer": "",
                        "evidence": [],
                    }
                    if args.fail_fast:
                        raise
                observation.update(
                    {
                        "case_id": case["id"],
                        "split": case["split"],
                        "category": case["category"],
                        "system": system,
                        "agent_id": agent_id,
                        "repetition": repetition,
                        "sequence_in_pair": sequence,
                        "pair_order": list(order),
                        "completed_at": utc_now(),
                    }
                )
                run["observations"].append(observation)
                write_observation(run_dir, len(run["observations"]), observation)
                print(
                    f"{case['id']} repetition={repetition} system={system} "
                    f"answer_chars={len(observation['answer'])}"
                )

    run["completed_at"] = utc_now()
    write_json_exclusive(run_dir / "run.json", run)
    print(run_dir / "run.json")


if __name__ == "__main__":
    main()
