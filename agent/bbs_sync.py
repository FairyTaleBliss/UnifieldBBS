from __future__ import annotations

from typing import Any

import requests


def sync_deposit_to_bbs(
    create_post_url: str,
    *,
    tx_hash: str,
    category: str,
    agent_run_id: str | None = None,
    pact_id: str | None = None,
    approve_request_id: str | None = None,
    approve_tx: str | None = None,
    deposit_request_id: str | None = None,
    agent_wallet: str | None = None,
) -> dict[str, Any]:
    payload = {"tx_hash": tx_hash, "category": category}
    for key, value in {
        "agent_run_id": agent_run_id,
        "pact_id": pact_id,
        "approve_request_id": approve_request_id,
        "approve_tx": approve_tx,
        "deposit_request_id": deposit_request_id,
        "agent_wallet": agent_wallet,
    }.items():
        if value:
            payload[key] = value

    response = requests.post(
        create_post_url,
        json=payload,
        timeout=30,
    )
    try:
        payload = response.json()
    except Exception:
        payload = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(f"Post sync failed ({response.status_code}): {payload}")
    return payload


def agent_run_status_url(create_post_url: str, agent_run_id: str) -> str:
    base = create_post_url.rstrip("/")
    if base.endswith("/api/create_post"):
        base = base[: -len("/api/create_post")]
    return f"{base}/api/agent-runs/{agent_run_id}/status"


def update_agent_run_status(create_post_url: str, agent_run_id: str, *, status: str, **fields: Any) -> dict[str, Any]:
    payload = {"status": status}
    payload.update({key: value for key, value in fields.items() if value not in (None, "")})
    response = requests.post(
        agent_run_status_url(create_post_url, agent_run_id),
        json=payload,
        timeout=30,
    )
    try:
        payload = response.json()
    except Exception:
        payload = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(f"Agent run status update failed ({response.status_code}): {payload}")
    return payload
