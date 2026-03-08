"""Tool call parsing and normalized envelopes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional


_MCP_TAG_RE = re.compile(
    r'<tool:mcp\s+name="([^"]+)">\s*(.*?)\s*</tool:mcp>',
    flags=re.DOTALL | re.IGNORECASE,
)
_DIRECT_TAG_RE = re.compile(
    r"<tool:([A-Za-z0-9_.-]+)>\s*(.*?)\s*</tool:\1>",
    flags=re.DOTALL | re.IGNORECASE,
)


@dataclass
class ToolInvocation:
    call_id: str
    name: str
    source: str
    arguments: Dict[str, Any]
    step: int


@dataclass
class ToolResultEnvelope:
    ok: bool
    tool: str
    call_id: str
    result: Optional[Dict[str, Any]]
    error: Optional[str]
    latency_ms: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "tool": self.tool,
            "call_id": self.call_id,
            "result": self.result if isinstance(self.result, dict) else None,
            "error": self.error if self.error else None,
            "latency_ms": max(0, int(self.latency_ms)),
        }


def build_call_id(step: int) -> str:
    return f"tc_{max(1, int(step))}"


def parse_json_object_or_error(raw: str, label: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise RuntimeError(f"Invalid {label} JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{label} payload must be a JSON object")
    return parsed


def parse_tool_tag_v1(text: str, step: int) -> Optional[ToolInvocation]:
    body = str(text or "")

    mcp_match = _MCP_TAG_RE.search(body)
    if mcp_match:
        name = mcp_match.group(1).strip()
        args = parse_json_object_or_error(mcp_match.group(2), "mcp tool arguments")
        return ToolInvocation(
            call_id=build_call_id(step),
            name=name,
            source="mcp",
            arguments=args,
            step=max(1, int(step)),
        )

    direct_match = _DIRECT_TAG_RE.search(body)
    if not direct_match:
        return None
    tool_name = direct_match.group(1).strip().lower()
    if tool_name == "mcp":
        return None

    raw_args = direct_match.group(2)
    if tool_name in {"web_search", "web_fetch", "shell"}:
        args: Dict[str, Any] = {"value": str(raw_args or "").strip()}
    else:
        args = parse_json_object_or_error(raw_args, f"{tool_name} tool")

    return ToolInvocation(
        call_id=build_call_id(step),
        name=tool_name,
        source="builtin",
        arguments=args,
        step=max(1, int(step)),
    )


def tool_trace_entry(invocation: ToolInvocation, envelope: ToolResultEnvelope) -> Dict[str, Any]:
    return {
        "call_id": invocation.call_id,
        "tool": invocation.name,
        "source": invocation.source,
        "ok": bool(envelope.ok),
        "latency_ms": max(0, int(envelope.latency_ms)),
        "error": envelope.error if envelope.error else None,
    }

