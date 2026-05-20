"""Shared GLM quota/limit parsing for push_glm_usage*.py."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

UNIT_FIVE_HOUR = 3
UNIT_WEEKLY = 6
UNIT_MCP_MONTHLY = 5

MCP_LABEL = "MCP monthly"
DEFAULT_LABELS = ("5h limit", "weekly limit")


class GlmQuotaParseError(RuntimeError):
    """Raised when quota payload cannot be parsed."""


@dataclass(slots=True, frozen=True)
class ParsedUsageRow:
    label: str
    left_percent: float
    reset_at: str | None = None
    stat_text: str | None = None


def unwrap_limits(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data")
    if isinstance(data, Mapping):
        data_map = cast(Mapping[str, Any], data)
        raw = data_map.get("limits")
    else:
        raw = payload.get("limits")

    if not isinstance(raw, Sequence):
        return []

    limits: list[Mapping[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            limits.append(cast(Mapping[str, Any], item))
    return limits


def parse_percent(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return max(0.0, min(100.0, float(value)))
    return 0.0


def _parse_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def epoch_to_reset_iso(value: int | float) -> str:
    ts = float(value)
    if ts > 1e12:
        ts /= 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def coerce_reset_at(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return epoch_to_reset_iso(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            return epoch_to_reset_iso(int(stripped))
        return stripped
    return None


def reset_at_from_limit(item: Mapping[str, Any]) -> str | None:
    for key in (
        "nextResetTime",
        "next_reset_time",
        "reset_at",
        "resetAt",
        "reset_time",
        "resetTime",
        "expire_time",
        "expireTime",
    ):
        normalized = coerce_reset_at(item.get(key))
        if normalized:
            return normalized
    return None


def reset_timestamp_ms(item: Mapping[str, Any]) -> float | None:
    raw = item.get("nextResetTime") or item.get("next_reset_time")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        return ts if ts > 1e12 else ts * 1000.0
    if isinstance(raw, str) and raw.strip().isdigit():
        ts = float(raw.strip())
        return ts if ts > 1e12 else ts * 1000.0
    return None


def _row_from_token_limit(label: str, item: Mapping[str, Any]) -> ParsedUsageRow:
    return ParsedUsageRow(
        label=label,
        left_percent=parse_percent(item.get("percentage")),
        reset_at=reset_at_from_limit(item),
    )


def _row_from_mcp(
    item: Mapping[str, Any],
    *,
    stat_text: str,
) -> ParsedUsageRow:
    total = _parse_int(item.get("usage"))
    remaining = _parse_int(item.get("remaining"))
    left_percent = (remaining / total * 100.0) if total > 0 else 0.0
    return ParsedUsageRow(
        label=MCP_LABEL,
        left_percent=max(0.0, min(100.0, left_percent)),
        reset_at=reset_at_from_limit(item),
        stat_text=stat_text,
    )


def _token_limits(limits: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [lim for lim in limits if lim.get("type") == "TOKENS_LIMIT"]


def _pick_five_hour(limits: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for item in limits:
        if item.get("type") == "TOKENS_LIMIT" and item.get("unit") == UNIT_FIVE_HOUR:
            return item
    return None


def _pick_five_hour_fallback(
    tokens: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if not tokens:
        return None
    ordered = sorted(tokens, key=lambda item: reset_timestamp_ms(item) or float("inf"))
    return ordered[0]


def _pick_weekly(limits: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for item in limits:
        if item.get("unit") != UNIT_WEEKLY:
            continue
        if item.get("type") in (None, "TOKENS_LIMIT"):
            return item
    return None


def _pick_mcp(limits: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for item in limits:
        if item.get("type") != "TIME_LIMIT":
            continue
        unit = item.get("unit")
        if unit is None or unit == UNIT_MCP_MONTHLY:
            return item
    return None


def parse_display_rows(
    payload: Mapping[str, Any],
    *,
    labels: Sequence[str] = DEFAULT_LABELS,
    mcp_stat: Callable[[int, int], str] | None = None,
) -> list[ParsedUsageRow]:
    code = payload.get("code")
    if code is not None and int(code) != 200:
        msg = payload.get("msg") or payload.get("message") or "unknown error"
        raise GlmQuotaParseError(f"GLM API returned code {code}: {msg}")

    limits = unwrap_limits(payload)
    tokens = _token_limits(limits)
    if not tokens:
        raise GlmQuotaParseError("No TOKENS_LIMIT entries in quota response.")

    five_h = _pick_five_hour(limits) or _pick_five_hour_fallback(tokens)
    weekly = _pick_weekly(limits)

    rows: list[ParsedUsageRow] = []
    if five_h:
        rows.append(_row_from_token_limit(labels[0], five_h))
    if weekly:
        weekly_label = labels[1] if len(labels) > 1 else "weekly limit"
        rows.append(_row_from_token_limit(weekly_label, weekly))
    elif mcp_item := _pick_mcp(limits):
        remaining = _parse_int(mcp_item.get("remaining"))
        total = _parse_int(mcp_item.get("usage"))
        fmt = mcp_stat or (lambda r, t: f"{r} left")
        rows.append(_row_from_mcp(mcp_item, stat_text=fmt(remaining, total)))

    return rows


def parse_quota_rows(
    payload: Mapping[str, Any],
    *,
    labels: Sequence[str] = DEFAULT_LABELS,
    mcp_stat: Callable[[int, int], str] | None = None,
) -> list[ParsedUsageRow]:
    """Backward-compatible alias for parse_display_rows."""
    return parse_display_rows(payload, labels=labels, mcp_stat=mcp_stat)
