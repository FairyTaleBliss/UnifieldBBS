from __future__ import annotations

import argparse
import json
from uuid import uuid4

from .bbs_sync import sync_deposit_to_bbs, update_agent_run_status
from .calldata import encode_approve_calldata, encode_deposit_calldata
from .caw_sdk_client import (
    contract_call,
    extract_pact_api_key,
    extract_pact_id,
    extract_tx_hash,
    get_pact,
    get_transaction_by_request_id,
    submit_pact,
    wait_for_pact_status,
)
from .config import load_caw_posting_config, token_units
from .pact_builder import build_unifieldbbs_pact


def request_ids_for_agent_run(agent_run_id: str | None) -> tuple[str, str]:
    if agent_run_id:
        short_id = str(agent_run_id).split("-")[0][:8]
        return f"unifieldbbs-{short_id}-approve", f"unifieldbbs-{short_id}-deposit"
    return f"unifieldbbs-approve-{uuid4().hex[:12]}", f"unifieldbbs-deposit-{uuid4().hex[:12]}"


def build_demo_plan(title: str, category: str, stake_usdc: str, *, agent_run_id: str | None = None) -> dict:
    config = load_caw_posting_config()
    stake_raw = token_units(stake_usdc, config.usdc_decimals)
    if stake_raw > config.max_stake_raw:
        raise ValueError(
            f"stake {stake_usdc} {config.usdc_symbol} exceeds CAW_MAX_STAKE_USDC={config.max_stake_usdc}"
        )

    pact_spec = build_unifieldbbs_pact(config, title=title, category=category)
    approve_request_id, deposit_request_id = request_ids_for_agent_run(agent_run_id)
    return {
        "pact_spec": pact_spec,
        "contract_calls": [
            {
                "label": "approve_usdc",
                "chain_id": config.chain_id,
                "contract_addr": config.usdc_addr,
                "calldata": encode_approve_calldata(config.staking_addr, stake_raw),
                "value": "0",
                "request_id": approve_request_id,
            },
            {
                "label": "deposit_stake",
                "chain_id": config.chain_id,
                "contract_addr": config.staking_addr,
                "calldata": encode_deposit_calldata(stake_raw),
                "value": "0",
                "request_id": deposit_request_id,
            },
        ],
        "post_draft": {
            "title": title,
            "category": category,
            "actor_type": "human_via_agent",
            "stake_amount": stake_usdc,
            "stake_token": config.usdc_symbol,
            "status": "pending_deposit",
        },
    }


def execute_contract_calls(config, plan: dict, *, pact_api_key: str | None = None) -> dict:
    results = []
    for call in plan["contract_calls"]:
        response = contract_call(config, call, api_key_override=pact_api_key)
        tx_hash = extract_tx_hash(response)
        results.append(
            {
                "label": call["label"],
                "request_id": call["request_id"],
                "contract_call": call,
                "response": response,
                "tx_hash": tx_hash,
            }
        )
    return {
        "calls": results,
        "deposit_tx_hash": next(
            (item["tx_hash"] for item in results if item["label"] == "deposit_stake" and item["tx_hash"]),
            None,
        ),
    }


def fetch_transactions_for_plan(config, plan: dict, *, pact_api_key: str | None = None) -> dict:
    transactions = []
    for call in plan["contract_calls"]:
        request_id = call["request_id"]
        record = get_transaction_by_request_id(config, request_id, api_key_override=pact_api_key)
        transactions.append(
            {
                "label": call["label"],
                "request_id": request_id,
                "record": record,
                "tx_hash": extract_tx_hash(record),
            }
        )
    return {
        "transactions": transactions,
        "deposit_tx_hash": next(
            (item["tx_hash"] for item in transactions if item["label"] == "deposit_stake" and item["tx_hash"]),
            None,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build or submit a CAW Pact + contract-call plan for UnifieldBBS posting.")
    parser.add_argument("--title", default="Agent-posted stake-backed request")
    parser.add_argument("--category", default="resources")
    parser.add_argument("--stake", default="1")
    parser.add_argument("--submit-pact", action="store_true", help="Submit the generated PactSpec to Cobo Agentic Wallet.")
    parser.add_argument("--get-pact", help="Fetch a CAW pact by pact id and print its status/details.")
    parser.add_argument("--wait-active", action="store_true", help="After submitting or fetching a pact, poll until status becomes active.")
    parser.add_argument("--execute-calls", action="store_true", help="Execute approve and deposit contract calls through CAW.")
    parser.add_argument("--fetch-transactions", action="store_true", help="Fetch transaction records by the generated request_id values.")
    parser.add_argument("--sync-post", action="store_true", help="Sync the deposit tx hash to UnifieldBBS /api/create_post.")
    parser.add_argument("--agent-run-id", help="Attach the verified deposit tx to an existing UnifieldBBS agent_runs row.")
    parser.add_argument("--deposit-tx-hash", help="Skip CAW lookup and sync this deposit tx hash to UnifieldBBS.")
    parser.add_argument("--pact-api-key", help="Use this approved Pact API key for contract calls. Defaults to CAW_PACT_API_KEY or active pact response api_key.")
    parser.add_argument("--require-pact-api-key", action="store_true", help="Refuse to execute contract calls unless a Pact API key is available.")
    parser.add_argument("--timeout", type=int, default=300, help="Max seconds to wait when --wait-active is used.")
    parser.add_argument("--poll", type=int, default=5, help="Polling interval seconds when --wait-active is used.")
    args = parser.parse_args()

    config = load_caw_posting_config()

    if args.get_pact:
        pact = wait_for_pact_status(
            config,
            args.get_pact,
            timeout_seconds=args.timeout,
            poll_seconds=args.poll,
        ) if args.wait_active else get_pact(config, args.get_pact)
        print(json.dumps(pact, indent=2))
        return

    plan = build_demo_plan(args.title, args.category, args.stake, agent_run_id=args.agent_run_id)

    if args.deposit_tx_hash and not args.sync_post:
        raise RuntimeError("--deposit-tx-hash is only meaningful with --sync-post")
    if args.sync_post and not args.agent_run_id:
        raise RuntimeError("--sync-post requires --agent-run-id so the verified tx completes the existing frontend request")

    if not any([args.submit_pact, args.execute_calls, args.fetch_transactions, args.sync_post]):
        print(json.dumps(plan, indent=2))
        return

    result = {
        "plan": plan,
        "agent_run_id": args.agent_run_id,
        "pact_response": None,
        "pact_id": None,
        "contract_calls": plan["contract_calls"],
        "post_draft": plan["post_draft"],
        "pact_api_key_available": False,
    }

    pact_api_key = args.pact_api_key or config.pact_api_key or None

    if args.submit_pact:
        pact_resp = submit_pact(config, plan["pact_spec"])
        pact_id = extract_pact_id(pact_resp)
        result["pact_response"] = pact_resp
        result["pact_id"] = pact_id
        if args.wait_active:
            if not pact_id:
                raise RuntimeError("submit_pact response did not include pact_id/id; cannot wait for active status")
            active_pact = wait_for_pact_status(
                config,
                pact_id,
                timeout_seconds=args.timeout,
                poll_seconds=args.poll,
            )
            result["active_pact"] = active_pact
            pact_api_key = pact_api_key or extract_pact_api_key(active_pact)
        else:
            pact_api_key = pact_api_key or extract_pact_api_key(pact_resp)

    result["pact_api_key_available"] = bool(pact_api_key)

    if args.require_pact_api_key and not pact_api_key:
        raise RuntimeError(
            "A Pact API key is required for contract calls. Approve the pact, then pass --pact-api-key "
            "or set CAW_PACT_API_KEY."
        )

    execution = None
    transactions = None
    if args.execute_calls:
        if args.agent_run_id:
            update_agent_run_status(
                config.post_sync_url,
                args.agent_run_id,
                status="executing",
                agent_wallet=config.src_address,
                pact_id=result.get("pact_id"),
                approve_request_id=plan["contract_calls"][0]["request_id"],
                deposit_request_id=plan["contract_calls"][1]["request_id"],
            )
        execution = execute_contract_calls(config, plan, pact_api_key=pact_api_key)
        result["execution"] = execution
    if args.fetch_transactions:
        transactions = fetch_transactions_for_plan(config, plan, pact_api_key=pact_api_key)
        result["transactions"] = transactions

    deposit_tx_hash = (
        args.deposit_tx_hash
        or (execution or {}).get("deposit_tx_hash")
        or (transactions or {}).get("deposit_tx_hash")
    )
    if args.sync_post:
        if not deposit_tx_hash:
            raise RuntimeError("Cannot sync post without a deposit tx hash")
        approve_tx_hash = None
        if execution:
            approve_tx_hash = next((item.get("tx_hash") for item in execution.get("calls", []) if item.get("label") == "approve_usdc"), None)
        if not approve_tx_hash and transactions:
            approve_tx_hash = next((item.get("tx_hash") for item in transactions.get("transactions", []) if item.get("label") == "approve_usdc"), None)
        result["post_sync"] = sync_deposit_to_bbs(
            config.post_sync_url,
            tx_hash=deposit_tx_hash,
            category=args.category,
            agent_run_id=args.agent_run_id,
            pact_id=result.get("pact_id") or args.get_pact,
            approve_request_id=plan["contract_calls"][0]["request_id"],
            approve_tx=approve_tx_hash,
            deposit_request_id=plan["contract_calls"][1]["request_id"],
            agent_wallet=config.src_address,
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"ERROR: {exc}")


