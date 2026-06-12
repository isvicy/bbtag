"""Shared StepFun account parsing for push_stepfun_balance*.py."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

DEFAULT_BASE_URL = "https://api.StepFun.com/v1"
ACCOUNTS_PATH = "/accounts"

ENV_API_KEYS = ("STEPFUN_API_KEY", "STEPFUN_TOKEN")


class StepFunAccountError(RuntimeError):
    """Raised when credentials or account API calls fail."""


@dataclass(frozen=True, slots=True)
class AccountView:
    account_type: str
    balance: float
    cash_total: float
    voucher_total: float
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class CashRatioView:
    """Display ratio: used/voucher; bar fill = used_percent (black = used)."""

    used_amount: float
    voucher_total: float
    balance: float
    left_percent: float
    used_percent: float
    summary_text: str


def default_base_url() -> str:
    env = os.getenv("STEPFUN_BASE_URL", "").strip()
    return env or DEFAULT_BASE_URL


def resolve_api_key(cli_key: str | None) -> str:
    if cli_key and cli_key.strip():
        return cli_key.strip()
    for name in ENV_API_KEYS:
        value = os.getenv(name, "").strip()
        if value:
            return value
    raise StepFunAccountError(
        "STEPFUN_API_KEY is not set. Export STEPFUN_API_KEY or pass --api-key."
    )


def _to_float(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def format_money(value: float) -> str:
    return f"¥{value:,.2f}"


def format_money_amount(value: float) -> str:
    """Compact amount for ratio line (no thousands separator)."""
    return f"¥{value:.2f}"


def format_money_console(value: float) -> str:
    """ASCII-safe amount for terminal output (Windows GBK consoles)."""
    return f"CNY {value:,.2f}"


def compute_cash_ratio(account: AccountView) -> CashRatioView:
    """Ratio line: used/voucher; % left = balance/voucher (voucher is denominator)."""
    balance = account.balance
    voucher = account.voucher_total

    if voucher > 0:
        used = voucher - balance
        if used < 0:
            used = 0.0
        left_percent = min(100.0, max(0.0, balance / voucher * 100.0))
        used_percent = min(100.0, max(0.0, used / voucher * 100.0))
    else:
        used = 0.0
        left_percent = 0.0
        used_percent = 0.0

    left_rounded = round(left_percent, 1)
    used_rounded = round(used_percent, 1)
    summary_text = (
        f"{format_money_amount(used)} / {format_money_amount(voucher)} "
        f"({left_rounded:.1f}% left)"
    )
    return CashRatioView(
        used_amount=used,
        voucher_total=voucher,
        balance=balance,
        left_percent=left_rounded,
        used_percent=used_rounded,
        summary_text=summary_text,
    )


def format_type_label(account_type: str) -> str:
    normalized = account_type.strip().lower()
    if normalized in ("prepaid", "postpaid"):
        return normalized
    return normalized or "unknown"


def parse_account_payload(
    payload: Mapping[str, Any],
    *,
    fetched_at: datetime | None = None,
) -> AccountView:
    if payload.get("object") != "account":
        raise StepFunAccountError(
            'Expected JSON object with object="account" from /accounts.'
        )

    account_type = str(payload.get("type") or "unknown")
    return AccountView(
        account_type=account_type,
        balance=_to_float(payload.get("balance")),
        cash_total=_to_float(payload.get("total_cash_balance")),
        voucher_total=_to_float(payload.get("total_voucher_balance")),
        fetched_at=fetched_at or datetime.now().astimezone(),
    )


def fetch_account_json(
    base_url: str,
    api_key: str,
    timeout: float,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{ACCOUNTS_PATH}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "push_stepfun_balance.py",
        "Accept": "application/json",
    }

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise StepFunAccountError(
                "Authentication failed with 401/403. Check your StepFun API key."
            ) from exc
        details = exc.read().decode("utf-8", errors="replace").strip()
        suffix = f": {details}" if details else ""
        raise StepFunAccountError(f"StepFun API returned HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise StepFunAccountError(f"Request failed: {exc.reason}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise StepFunAccountError(f"Failed to parse API response as JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise StepFunAccountError("Expected a JSON object from /accounts.")
    return payload


def format_ratio_console(ratio: CashRatioView) -> str:
    """ASCII-safe ratio line for terminal output."""
    return (
        f"{format_money_console(ratio.used_amount)} / "
        f"{format_money_console(ratio.voucher_total)} "
        f"({ratio.left_percent:.1f}% left)"
    )


def format_updated_text(fetched_at: datetime) -> str:
    local = fetched_at.astimezone()
    return f"updated {local.strftime('%H:%M')}"


def format_updated_text_long(fetched_at: datetime) -> str:
    local = fetched_at.astimezone()
    return f"updated {local.strftime('%Y-%m-%d %H:%M')}"
