"""Run Aureon's daily credit renewal and usage-counter reset.

This is a one-shot worker intended to be invoked by the deployment scheduler
at midnight in the timezone configured for the Supabase database. The RPC is
atomic and idempotent per database date, so retries do not grant credits twice.

Required environment:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY

Apply the `run_daily_maintenance` function and related schema from
`supabase_schema.sql` before scheduling this worker.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import httpx


LOGGER = logging.getLogger("aureon.daily_cron")
DEFAULT_TIMEOUT_SECONDS = 60.0
RPC_NAME = "run_daily_maintenance"


class WorkerConfigurationError(RuntimeError):
    """Raised when required worker configuration is missing or invalid."""


class WorkerExecutionError(RuntimeError):
    """Raised when Supabase does not complete the daily maintenance RPC."""


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise WorkerConfigurationError(f"Required environment variable is missing: {name}")
    return value


def _timeout_seconds() -> float:
    raw = os.getenv("AUREON_CRON_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise WorkerConfigurationError("AUREON_CRON_TIMEOUT_SECONDS must be a number") from exc
    if timeout <= 0:
        raise WorkerConfigurationError("AUREON_CRON_TIMEOUT_SECONDS must be greater than zero")
    return timeout


def _maintenance_endpoint(supabase_url: str) -> str:
    normalized = supabase_url.rstrip("/")
    try:
        parsed = httpx.URL(normalized)
    except httpx.InvalidURL as exc:
        raise WorkerConfigurationError("SUPABASE_URL must be a valid HTTP(S) URL") from exc
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise WorkerConfigurationError("SUPABASE_URL must be a valid HTTP(S) URL")
    return f"{normalized}/rest/v1/rpc/{RPC_NAME}"


def run_daily_maintenance() -> dict[str, Any]:
    """Call the transactional Supabase procedure and return its summary."""
    supabase_url = _required_env("SUPABASE_URL")
    service_role_key = _required_env("SUPABASE_SERVICE_ROLE_KEY")
    endpoint = _maintenance_endpoint(supabase_url)
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = httpx.post(
            endpoint,
            headers=headers,
            json={},
            timeout=_timeout_seconds(),
        )
    except httpx.HTTPError as exc:
        raise WorkerExecutionError("Could not reach the Supabase daily maintenance RPC") from exc

    if response.status_code >= 400:
        LOGGER.error("Daily maintenance RPC returned HTTP %s", response.status_code)
        raise WorkerExecutionError(
            f"Supabase daily maintenance RPC failed with HTTP {response.status_code}"
        )
    try:
        result = response.json()
    except json.JSONDecodeError as exc:
        raise WorkerExecutionError("Supabase daily maintenance RPC returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("success") is not True:
        raise WorkerExecutionError("Supabase daily maintenance RPC returned an unsuccessful result")
    return result


def main() -> int:
    logging.basicConfig(
        level=os.getenv("AUREON_CRON_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        result = run_daily_maintenance()
    except (WorkerConfigurationError, WorkerExecutionError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except Exception:
        LOGGER.exception("Unexpected daily maintenance worker failure")
        return 1

    LOGGER.info(
        "Daily maintenance completed: run_date=%s credited_accounts=%s "
        "credits_added=%s balances_capped=%s trackers_reset=%s",
        result.get("run_date"),
        result.get("credited_accounts"),
        result.get("credits_added"),
        result.get("balances_capped"),
        result.get("trackers_reset"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())