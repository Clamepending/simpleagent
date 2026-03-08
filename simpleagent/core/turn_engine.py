"""Deterministic tool-loop runtime for /v1 turn execution."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .tools import ToolInvocation, ToolResultEnvelope, tool_trace_entry


@dataclass
class TurnLoopOutcome:
    assistant_message: str
    tool_trace: List[Dict[str, Any]]
    steps: int


def run_turn_loop(
    *,
    initial_messages: List[Dict[str, str]],
    call_model: Callable[[List[Dict[str, str]]], str],
    parse_tool_call: Callable[[str, int], Optional[ToolInvocation]],
    execute_tool_call: Callable[[ToolInvocation], ToolResultEnvelope],
    max_tool_passes: int,
    max_turn_seconds: int,
    max_output_chars: int,
    tool_result_hint: str = "Now continue and answer the user directly.",
    limit_message: str = "I hit the tool-call limit for this turn. Please narrow the request and try again.",
) -> TurnLoopOutcome:
    messages = list(initial_messages or [])
    trace: List[Dict[str, Any]] = []
    max_passes = max(1, int(max_tool_passes))
    turn_deadline = time.time() + max(1, int(max_turn_seconds))
    final_text = ""

    for step in range(1, max_passes + 2):
        if time.time() > turn_deadline:
            final_text = "I hit the turn timeout limit. Please retry with a narrower request."
            break

        assistant_text = str(call_model(messages) or "").strip()
        if not assistant_text:
            final_text = "Model returned an empty response."
            break

        invocation = parse_tool_call(assistant_text, step)
        if invocation is None:
            final_text = assistant_text
            break

        if step > max_passes:
            final_text = limit_message
            break

        started = time.time()
        envelope = execute_tool_call(invocation)
        elapsed_ms = int((time.time() - started) * 1000)
        normalized = ToolResultEnvelope(
            ok=bool(envelope.ok),
            tool=invocation.name,
            call_id=invocation.call_id,
            result=envelope.result if isinstance(envelope.result, dict) else None,
            error=str(envelope.error or "").strip() or None,
            latency_ms=elapsed_ms,
        )
        trace.append(tool_trace_entry(invocation, normalized))

        tool_payload = json.dumps(normalized.to_dict(), ensure_ascii=True)
        messages.append({"role": "assistant", "content": assistant_text})
        messages.append(
            {
                "role": "user",
                "content": f"TOOL_RESULT {invocation.name}\n{tool_payload}\n{tool_result_hint}",
            }
        )
        final_text = assistant_text
    else:
        final_text = limit_message

    final_text = (final_text or "").strip()
    if max_output_chars > 0 and len(final_text) > max_output_chars:
        final_text = f"{final_text[:max_output_chars]}\n...[truncated]"

    return TurnLoopOutcome(
        assistant_message=final_text,
        tool_trace=trace,
        steps=len(trace),
    )

