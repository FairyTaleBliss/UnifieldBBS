from __future__ import annotations

import asyncio
import time
from typing import Any

from .config import CAWPostingConfig


TERMINAL_PACT_STATUSES = {"rejected", "expired", "revoked", "completed"}


def require_sdk_client():
    try:
        from cobo_agentic_wallet.client import WalletAPIClient
    except ImportError as exc:
        raise RuntimeError(
            "Cobo Agentic Wallet SDK is not installed. Use Python 3.11+ and run: "
            "pip install cobo-agentic-wallet"
        ) from exc
    return WalletAPIClient


def require_caw_credentials(config: CAWPostingConfig, *, api_key_override: str | None = None) -> None:
    missing = []
    if not config.api_url:
        missing.append("AGENT_WALLET_API_URL")
    if not (api_key_override or config.api_key):
        missing.append("AGENT_WALLET_API_KEY")
    if not config.wallet_id:
        missing.append("AGENT_WALLET_WALLET_ID")
    if missing:
        raise RuntimeError("Missing CAW environment variables: " + ", ".join(missing))


def caw_spec_payload(pact_spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "policies": pact_spec["policies"],
        "completion_conditions": pact_spec["completion_conditions"],
        "execution_plan": pact_spec["execution_plan"],
    }


def extract_pact_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("pact_id", "id"):
            value = payload.get(key)
            if value:
                return str(value)
        for key in ("result", "data", "pact"):
            found = extract_pact_id(payload.get(key))
            if found:
                return found
    return None


def extract_pact_api_key(payload: Any) -> str | None:
    if isinstance(payload, dict):
        value = payload.get("api_key")
        if value:
            return str(value)
        for key in ("result", "data", "pact"):
            found = extract_pact_api_key(payload.get(key))
            if found:
                return found
    return None


def extract_tx_hash(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("tx_hash", "transaction_hash", "hash", "transactionHash"):
            value = payload.get(key)
            if value:
                return str(value)
        for key in ("transaction", "result", "data", "record"):
            found = extract_tx_hash(payload.get(key))
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = extract_tx_hash(item)
            if found:
                return found
    return None


async def submit_pact_async(config: CAWPostingConfig, pact_spec: dict[str, Any]) -> dict[str, Any]:
    require_caw_credentials(config)
    WalletAPIClient = require_sdk_client()
    async with WalletAPIClient(base_url=config.api_url, api_key=config.api_key) as client:
        return await client.submit_pact(
            wallet_id=config.wallet_id,
            intent=pact_spec["intent"],
            original_intent=pact_spec.get("original_intent"),
            spec=caw_spec_payload(pact_spec),
            name=pact_spec.get("name"),
        )


async def get_pact_async(config: CAWPostingConfig, pact_id: str) -> dict[str, Any]:
    require_caw_credentials(config)
    WalletAPIClient = require_sdk_client()
    async with WalletAPIClient(base_url=config.api_url, api_key=config.api_key) as client:
        return await client.get_pact(pact_id)


async def contract_call_async(
    config: CAWPostingConfig,
    call: dict[str, Any],
    *,
    api_key_override: str | None = None,
) -> dict[str, Any]:
    require_caw_credentials(config, api_key_override=api_key_override)
    WalletAPIClient = require_sdk_client()
    async with WalletAPIClient(base_url=config.api_url, api_key=api_key_override or config.api_key) as client:
        return await client.contract_call(
            config.wallet_id,
            chain_id=call["chain_id"],
            contract_addr=call["contract_addr"],
            value=str(call.get("value", "0")),
            calldata=call["calldata"],
            request_id=call.get("request_id"),
            src_addr=call.get("src_addr") or config.src_address or None,
            description=call.get("description") or call.get("label"),
        )


async def get_transaction_by_request_id_async(
    config: CAWPostingConfig,
    request_id: str,
    *,
    ext: bool = True,
    api_key_override: str | None = None,
) -> dict[str, Any]:
    require_caw_credentials(config, api_key_override=api_key_override)
    WalletAPIClient = require_sdk_client()
    async with WalletAPIClient(base_url=config.api_url, api_key=api_key_override or config.api_key) as client:
        return await client.get_user_transaction_by_request_id(config.wallet_id, request_id, ext=ext)


async def wait_for_pact_status_async(
    config: CAWPostingConfig,
    pact_id: str,
    *,
    desired_status: str = "active",
    timeout_seconds: int = 300,
    poll_seconds: int = 5,
) -> dict[str, Any]:
    started = time.monotonic()
    last_status = None
    while True:
        pact = await get_pact_async(config, pact_id)
        status = str(pact.get("status", ""))
        if status != last_status:
            elapsed = int(time.monotonic() - started)
            print(f"pact status -> {status or 'unknown'} (elapsed {elapsed}s)")
            last_status = status
        if status == desired_status:
            return pact
        if status in TERMINAL_PACT_STATUSES:
            raise RuntimeError(f"Pact reached terminal status before {desired_status}: {status}")
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError(f"Timed out waiting for pact {pact_id} to reach {desired_status}")
        await asyncio.sleep(poll_seconds)


def submit_pact(config: CAWPostingConfig, pact_spec: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(submit_pact_async(config, pact_spec))


def get_pact(config: CAWPostingConfig, pact_id: str) -> dict[str, Any]:
    return asyncio.run(get_pact_async(config, pact_id))


def contract_call(
    config: CAWPostingConfig,
    call: dict[str, Any],
    *,
    api_key_override: str | None = None,
) -> dict[str, Any]:
    return asyncio.run(contract_call_async(config, call, api_key_override=api_key_override))


def get_transaction_by_request_id(
    config: CAWPostingConfig,
    request_id: str,
    *,
    ext: bool = True,
    api_key_override: str | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        get_transaction_by_request_id_async(
            config,
            request_id,
            ext=ext,
            api_key_override=api_key_override,
        )
    )


def wait_for_pact_status(
    config: CAWPostingConfig,
    pact_id: str,
    *,
    desired_status: str = "active",
    timeout_seconds: int = 300,
    poll_seconds: int = 5,
) -> dict[str, Any]:
    return asyncio.run(
        wait_for_pact_status_async(
            config,
            pact_id,
            desired_status=desired_status,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
    )
