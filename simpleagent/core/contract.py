"""Runtime contract DTOs for /v1 endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


RUNTIME_CONTRACT_VERSION_V1 = "v1"
TOOLS_PROTOCOL_V1 = "tool-tag-v1"


class ContractValidationError(ValueError):
    """Raised when /v1 request payload fails schema validation."""


@dataclass
class TurnTenant:
    bot_id: str
    session_id: str


@dataclass
class TurnModel:
    provider: str = ""
    model: str = ""


@dataclass
class ToolPolicy:
    enabled_tools: List[str] = field(default_factory=list)
    disabled_tools: List[str] = field(default_factory=list)


@dataclass
class RuntimeContext:
    memory_docs: Dict[str, str] = field(default_factory=dict)
    tool_policy: ToolPolicy = field(default_factory=ToolPolicy)


@dataclass
class TurnRequest:
    request_id: str
    deployment_id: str
    tenant: TurnTenant
    user_message: str
    model: TurnModel
    runtime_context: RuntimeContext
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION_V1


def _as_dict(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    raise ContractValidationError("JSON object body is required")


def _as_non_empty_str(value: Any, field_name: str, max_len: int = 2048) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractValidationError(f"{field_name} is required")
    return text[:max_len]


def _as_optional_str(value: Any, max_len: int = 256) -> str:
    return str(value or "").strip()[:max_len]


def _as_string_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in value.items():
        key = str(k or "").strip()
        if not key:
            continue
        out[key] = str(v or "").strip()
    return out


def _as_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def parse_turn_request(payload: Any) -> TurnRequest:
    data = _as_dict(payload)
    tenant_raw = _as_dict(data.get("tenant") or {})
    model_raw = _as_dict(data.get("model") or {})
    runtime_context_raw = _as_dict(data.get("runtime_context") or {})
    tool_policy_raw = _as_dict(runtime_context_raw.get("tool_policy") or {})

    contract_version = _as_optional_str(
        data.get("runtime_contract_version", RUNTIME_CONTRACT_VERSION_V1),
        max_len=32,
    ) or RUNTIME_CONTRACT_VERSION_V1
    if contract_version != RUNTIME_CONTRACT_VERSION_V1:
        raise ContractValidationError(
            f"Unsupported runtime_contract_version '{contract_version}'"
        )

    return TurnRequest(
        request_id=_as_non_empty_str(data.get("request_id"), "request_id", max_len=128),
        deployment_id=_as_non_empty_str(data.get("deployment_id"), "deployment_id", max_len=128),
        tenant=TurnTenant(
            bot_id=_as_non_empty_str(tenant_raw.get("bot_id"), "tenant.bot_id", max_len=128),
            session_id=_as_non_empty_str(
                tenant_raw.get("session_id"), "tenant.session_id", max_len=256
            ),
        ),
        user_message=_as_non_empty_str(data.get("user_message"), "user_message", max_len=16000),
        model=TurnModel(
            provider=_as_optional_str(model_raw.get("provider"), max_len=64),
            model=_as_optional_str(model_raw.get("model"), max_len=256),
        ),
        runtime_context=RuntimeContext(
            memory_docs=_as_string_map(runtime_context_raw.get("memory_docs")),
            tool_policy=ToolPolicy(
                enabled_tools=_as_string_list(tool_policy_raw.get("enabled_tools")),
                disabled_tools=_as_string_list(tool_policy_raw.get("disabled_tools")),
            ),
        ),
        runtime_contract_version=contract_version,
    )


def health_payload(
    *,
    runtime_kind: str,
    runtime_version: str,
    runtime_contract_version: str,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "runtime_kind": runtime_kind,
        "runtime_version": runtime_version,
        "runtime_contract_version": runtime_contract_version,
    }


def capabilities_payload(
    *,
    runtime_kind: str,
    runtime_version: str,
    runtime_contract_version: str,
    features: Dict[str, bool],
    max_tool_passes: int,
) -> Dict[str, Any]:
    return {
        "runtime_kind": runtime_kind,
        "runtime_version": runtime_version,
        "runtime_contract_version": runtime_contract_version,
        "tools_protocol": TOOLS_PROTOCOL_V1,
        "max_tool_passes": max(1, int(max_tool_passes)),
        "features": features,
    }


def turn_success_payload(
    *,
    runtime_kind: str,
    runtime_version: str,
    runtime_contract_version: str,
    trace_id: str,
    event_id: str,
    assistant_message: str,
    tool_trace: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "ok": True,
        "status": "ok",
        "trace_id": trace_id,
        "event_id": event_id,
        "runtime_kind": runtime_kind,
        "runtime_version": runtime_version,
        "runtime_contract_version": runtime_contract_version,
        "assistant_message": assistant_message,
        "tool_trace": tool_trace,
    }


def turn_error_payload(
    *,
    runtime_kind: str,
    runtime_version: str,
    runtime_contract_version: str,
    trace_id: str,
    event_id: str,
    error: str,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "trace_id": trace_id,
        "event_id": event_id,
        "runtime_kind": runtime_kind,
        "runtime_version": runtime_version,
        "runtime_contract_version": runtime_contract_version,
        "error": str(error or "runtime turn failed"),
    }

