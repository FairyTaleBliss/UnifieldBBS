from __future__ import annotations

from typing import Any

from .calldata import APPROVE_SELECTOR, DEPOSIT_SELECTOR
from .config import CAWPostingConfig


def build_unifieldbbs_pact(config: CAWPostingConfig, *, title: str, category: str) -> dict[str, Any]:
    max_raw = str(config.max_stake_raw)
    intent = f"Publish a stake-backed UnifieldBBS post in {category}"
    return {
        "name": f"UnifieldBBS agent posting: {title}",
        "intent": intent,
        "original_intent": (
            f"Create a stake-backed UnifieldBBS post titled '{title}' in {category}. "
            "The agent may only approve USDC and deposit stake within the configured CAW policy boundary."
        ),
        "execution_plan": (
            "# Summary\n"
            f"The agent will publish a UnifieldBBS post titled '{title}' by staking up to "
            f"{config.max_stake_usdc} {config.usdc_symbol}.\n\n"
            "# Operations\n"
            "1. Call USDC.approve(staking_contract, amount).\n"
            "2. Call BBSStaking.deposit(amount).\n"
            "3. Submit the deposit transaction hash to UnifieldBBS so the backend can verify "
            "Deposited(address,uint256,uint256) and create the post.\n\n"
            "# Risk Controls\n"
            "- Contract calls are limited to the configured USDC and BBSStaking contracts.\n"
            "- approve.spender must equal the BBSStaking contract.\n"
            "- approve.amount and deposit.amount must not exceed the per-post stake cap.\n"
            "- The pact expires by transaction count or elapsed time."
        ),
        "policies": [
            {
                "name": "allow-usdc-approve-for-unifieldbbs",
                "type": "contract_call",
                "rules": {
                    "effect": "allow",
                    "when": {
                        "chain_in": [config.chain_id],
                        "target_in": [
                            {
                                "chain_id": config.chain_id,
                                "contract_addr": config.usdc_addr,
                                "function_id": APPROVE_SELECTOR,
                            }
                        ],
                        "params_match": [
                            {"param_name": "spender", "op": "eq", "value": config.staking_addr},
                            {"param_name": "amount", "op": "lte", "value": max_raw},
                        ],
                    },
                    "function_abis": [
                        {
                            "type": "function",
                            "name": "approve",
                            "selector": APPROVE_SELECTOR,
                            "inputs": [
                                {"name": "spender", "type": "address"},
                                {"name": "amount", "type": "uint256"},
                            ],
                        }
                    ],
                },
            },
            {
                "name": "allow-unifieldbbs-deposit",
                "type": "contract_call",
                "rules": {
                    "effect": "allow",
                    "when": {
                        "chain_in": [config.chain_id],
                        "target_in": [
                            {
                                "chain_id": config.chain_id,
                                "contract_addr": config.staking_addr,
                                "function_id": DEPOSIT_SELECTOR,
                            }
                        ],
                        "params_match": [
                            {"param_name": "amount", "op": "lte", "value": max_raw},
                        ],
                    },
                    "function_abis": [
                        {
                            "type": "function",
                            "name": "deposit",
                            "selector": DEPOSIT_SELECTOR,
                            "inputs": [{"name": "amount", "type": "uint256"}],
                        }
                    ],
                },
            },
        ],
        "completion_conditions": [
            {"type": "tx_count", "threshold": str(config.max_tx_count)},
            {"type": "time_elapsed", "threshold": str(config.pact_ttl_seconds)},
        ],
    }
