#!/usr/bin/env python3
"""
Minimal EIP-7702 verification driver for this repo.

Usage example:
    RPC_URL=http://127.0.0.1:8545 \
    PAYMASTER_B_PRIVATE_KEY=0x... \
    python scripts/7702_driver.py

Requirements:
    - The RPC endpoint must support EIP-7702 / type-4 transactions.
    - Contract artifacts must already exist in build/contracts.
"""

from __future__ import annotations

import json
import os
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import rlp
import requests
from eth_account import Account
from eth_keys import keys
from eth_utils import keccak, to_canonical_address, to_checksum_address
from hexbytes import HexBytes
from web3 import Web3
from web3.contract import Contract


ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build" / "contracts"
DEFAULT_RPC_URL = os.getenv("RPC_URL", "http://127.0.0.1:8545")
DEFAULT_FUND_AMOUNT = int(os.getenv("USER_A_FUND_AMOUNT", str(1_000 * 10**6)))
DEFAULT_TOTAL_AMOUNT = int(os.getenv("STAKE_TOTAL_AMOUNT", str(500 * 10**6)))
DEFAULT_FEE_AMOUNT = int(os.getenv("STAKE_FEE_AMOUNT", str(10 * 10**6)))
DEFAULT_TYPE4_GAS = int(os.getenv("TYPE4_GAS_LIMIT", "700000"))
DEFAULT_DEPLOY_GAS_BUFFER = int(os.getenv("DEPLOY_GAS_BUFFER", "50000"))

w3 = Web3(Web3.HTTPProvider(
        DEFAULT_RPC_URL,
        request_kwargs={"timeout": 30},
    ))



def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value




def load_artifact(contract_name: str) -> dict[str, Any]:
    artifact_path = BUILD_DIR / f"{contract_name}.json"
    if not artifact_path.exists():
        raise FileNotFoundError(
            f"Missing artifact: {artifact_path}. Run brownie compile first."
        )

    return json.loads(artifact_path.read_text())


def fee_params() -> dict[str, int]:
    latest_block = w3.eth.get_block("latest")
    base_fee = int(latest_block.get("baseFeePerGas", 0))

    try:
        priority_fee = int(w3.eth.max_priority_fee)
    except Exception:
        priority_fee = 1_000_000_000

    max_fee = max(priority_fee * 2, base_fee * 2 + priority_fee)
    return {
        "maxPriorityFeePerGas": priority_fee,
        "maxFeePerGas": max_fee,
    }


def build_contract(contract_name: str) -> Contract:
    artifact = load_artifact(contract_name)
    return w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"])


def send_signed_transaction(tx: dict[str, Any], private_key: str):
    signer = Account.from_key(private_key)

    tx.setdefault("chainId", w3.eth.chain_id)
    tx.setdefault("nonce", w3.eth.get_transaction_count(signer.address))
    tx.setdefault("value", 0)
    tx.setdefault("data", b"")
    tx.setdefault("from", signer.address)
    tx.update({k: v for k, v in fee_params().items() if k not in tx})

    if "gas" not in tx:
        gas_estimate = w3.eth.estimate_gas(tx)
        tx["gas"] = gas_estimate + DEFAULT_DEPLOY_GAS_BUFFER

    signed = Account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(tx_hash)


def deploy_contract(
    contract_name: str,
    deployer_key: str,
    *constructor_args: Any,
) -> Contract:
    signer = Account.from_key(deployer_key)
    contract = build_contract(contract_name)
    tx = contract.constructor(*constructor_args).build_transaction(
        {
            "from": signer.address,
            "nonce": w3.eth.get_transaction_count(signer.address),
            "chainId": w3.eth.chain_id,
            **fee_params(),
        }
    )
    receipt = send_signed_transaction(tx, deployer_key)
    return w3.eth.contract(address=receipt.contractAddress, abi=contract.abi)


def sign_authorization(
    user_key: str,
    proxy_addr: str,
    nonce: int,
) -> dict[str, Any]:
    checksum_proxy = to_checksum_address(proxy_addr)
    chain_id = w3.eth.chain_id

    authorization_payload = [chain_id, to_canonical_address(checksum_proxy), nonce]
    authorization_hash = keccak(b"\x05" + rlp.encode(authorization_payload))

    user_private_key = keys.PrivateKey(HexBytes(user_key))
    signature = user_private_key.sign_msg_hash(authorization_hash)

    authorization = {
        "chainId": chain_id,
        "address": checksum_proxy,
        "nonce": nonce,
        "yParity": signature.v,
        "r": signature.r,
        "s": signature.s,
        "authorizationHash": authorization_hash,
    }

    # Cross-check against eth-account's native implementation to catch mistakes.
    reference = Account.sign_authorization(
        {
            "chainId": chain_id,
            "address": checksum_proxy,
            "nonce": nonce,
        },
        user_key,
    )
    if reference.authorization_hash != authorization_hash:
        raise RuntimeError("Manual authorization hash does not match eth-account")
    if (reference.y_parity, reference.r, reference.s) != (
        authorization["yParity"],
        authorization["r"],
        authorization["s"],
    ):
        raise RuntimeError("Manual authorization signature does not match eth-account")

    return authorization


def send_type4_tx(
    paymaster_key: str,
    user_addr: str,
    auth_list: list[dict[str, Any]],
    call_data: bytes | HexBytes | str,
):
    paymaster = Account.from_key(paymaster_key)
    user_checksum = to_checksum_address(user_addr)

    tx = {
        "type": 4,
        "chainId": w3.eth.chain_id,
        "nonce": w3.eth.get_transaction_count(paymaster.address),
        "to": user_checksum,
        "value": 0,
        "data": HexBytes(call_data),
        "accessList": [],
        "authorizationList": [
            {
                "chainId": auth["chainId"],
                "address": to_checksum_address(auth["address"]),
                "nonce": auth["nonce"],
                "yParity": auth["yParity"],
                "r": auth["r"],
                "s": auth["s"],
            }
            for auth in auth_list
        ],
        **fee_params(),
    }

    if tx["to"] != user_checksum:
        raise RuntimeError("Type 4 transaction must target the user EOA")

    estimate_payload = dict(tx)
    estimate_payload["from"] = paymaster.address
    try:
        gas_estimate = w3.eth.estimate_gas(estimate_payload)
        tx["gas"] = gas_estimate + DEFAULT_DEPLOY_GAS_BUFFER
    except Exception:
        tx["gas"] = DEFAULT_TYPE4_GAS

    signed = Account.sign_transaction(tx, paymaster_key)
    raw_tx = HexBytes(signed.raw_transaction)
    if raw_tx[0] != 0x04:
        raise RuntimeError("Signed transaction is not a type-4 transaction")

    tx_hash = w3.eth.send_raw_transaction(raw_tx)
    return w3.eth.wait_for_transaction_receipt(tx_hash)


def format_usdc(amount: int) -> str:
    return f"{amount / 10**6:.6f}"


def main() -> None:
    paymaster_key = '0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d'
    # rpc_url = normalize_rpc_url(DEFAULT_RPC_URL)
    fund_amount = DEFAULT_FUND_AMOUNT
    total_amount = DEFAULT_TOTAL_AMOUNT
    fee_amount = DEFAULT_FEE_AMOUNT

    if fee_amount <= 0:
        raise RuntimeError("STAKE_FEE_AMOUNT must be greater than 0")
    if total_amount <= fee_amount:
        raise RuntimeError("STAKE_TOTAL_AMOUNT must be greater than STAKE_FEE_AMOUNT")

    # W3 = build_web3(rpc_url)
    # if not W3.is_connected():
    #     raise RuntimeError(f"Failed to connect to RPC_URL: {rpc_url}")

    paymaster = Account.from_key(paymaster_key)
    # user_key = os.getenv("USER_A_PRIVATE_KEY")
    user_key = '0x'+'0000000000000000000000000000000000000000000000000000000000000001'
    if user_key:
        user = Account.from_key(user_key)
    else:
        user = Account.create()

    print("=== EIP-7702 Verification Driver ===")
    # print(f"RPC URL: {rpc_url}")
    print(f"Chain ID: {w3.eth.chain_id}")
    print(f"User: {user.address}")
    print(f"Paymaster: {paymaster.address}")
    print()

    print("[1/5] Deploying contracts...")
    usdc = deploy_contract(
        "Erc20",
        paymaster_key,
        1_000_000 * 10**6,
        "Mock USDC",
        6,
        "USDC",
    )
    aave = deploy_contract("MockAave", paymaster_key)
    staking = deploy_contract("BBSStaking", paymaster_key, usdc.address, aave.address)
    paygasforstaking = deploy_contract("PayGasForStaking", paymaster_key)

    print(f"MockUSDC: {usdc.address}")
    print(f"MockAave:  {aave.address}")
    print(f"Staking:   {staking.address}")
    print(f"PayGasForStaking:     {paygasforstaking.address}")
    print()

    print("[2/5] Funding user with MockUSDC...")
    transfer_receipt = send_signed_transaction(
        usdc.functions.transfer(user.address, fund_amount).build_transaction(
            {
                "from": paymaster.address,
                "nonce": w3.eth.get_transaction_count(paymaster.address),
                "chainId": w3.eth.chain_id,
                **fee_params(),
            }
        ),
        paymaster_key,
    )
    if transfer_receipt.status != 1:
        raise RuntimeError("Funding transaction failed")

    print(f"User funded: {format_usdc(usdc.functions.balanceOf(user.address).call())} USDC")
    print()

    print("[3/5] Creating EIP-7702 authorization...")
    user_nonce = w3.eth.get_transaction_count(user.address)
    signed_auth = sign_authorization(user.key.hex(), paygasforstaking.address, user_nonce)
    print(f"Authorization hash: {HexBytes(signed_auth['authorizationHash']).hex()}")
    print("Authorization prefix check: 0x05 over RLP([chain_id, proxy, nonce])")
    print()

    print("[4/5] Sending type-4 transaction from paymaster B...")
    pre_user_balance = usdc.functions.balanceOf(user.address).call()
    pre_paymaster_balance = usdc.functions.balanceOf(paymaster.address).call()
    pre_total_staked = staking.functions.totalStaked().call()

    call_data = paygasforstaking.functions.executeSponsorStake(
        usdc.address,
        staking.address,
        total_amount,
        fee_amount,
        paymaster.address,
    )._encode_transaction_data()

    receipt = send_type4_tx(
        paymaster_key=paymaster_key,
        user_addr=user.address,
        auth_list=[signed_auth],
        call_data=call_data,
    )

    if receipt.status != 1:
        raise RuntimeError("Type-4 transaction execution failed")

    post_user_balance = usdc.functions.balanceOf(user.address).call()
    post_paymaster_balance = usdc.functions.balanceOf(paymaster.address).call()
    post_total_staked = staking.functions.totalStaked().call()
    staking_count = staking.functions.stakingCount().call()
    latest_stake = staking.functions.stakings(staking_count).call()
    expected_deposit = total_amount - fee_amount

    print(f"Type-4 tx hash: {receipt.transactionHash.hex()}")
    print("Transaction prefix check: 0x04")
    print()

    print("[5/5] Verifying balance changes...")
    print(f"User USDC:      {format_usdc(pre_user_balance)} -> {format_usdc(post_user_balance)}")
    print(f"Paymaster USDC: {format_usdc(pre_paymaster_balance)} -> {format_usdc(post_paymaster_balance)}")
    print(f"totalStaked:      {format_usdc(pre_total_staked)} -> {format_usdc(post_total_staked)}")
    print(f"Latest staking id: {staking_count}")
    print(f"Latest staking user: {latest_stake[0]}")
    print(f"Latest staking amount: {format_usdc(latest_stake[1])}")
    print(f"Latest staking withdrawn: {latest_stake[3]}")
    print()

    if pre_user_balance - post_user_balance != total_amount:
        raise RuntimeError("User balance delta does not match total amount")
    if post_paymaster_balance - pre_paymaster_balance != fee_amount:
        raise RuntimeError("Paymaster did not receive the expected fee")
    if post_total_staked - pre_total_staked != expected_deposit:
        raise RuntimeError("Staking total did not increase by the expected deposit amount")
    if to_checksum_address(latest_stake[0]) != to_checksum_address(user.address):
        raise RuntimeError("Latest staking record is not owned by user")
    if int(latest_stake[1]) != expected_deposit:
        raise RuntimeError("Latest staking amount is incorrect")
    if bool(latest_stake[3]):
        raise RuntimeError("Latest staking should not be withdrawn")

    print("Verification passed.")
    print(f"Expected deposit amount: {format_usdc(expected_deposit)} USDC")
    print(f"Expected fee amount:     {format_usdc(fee_amount)} USDC")


if __name__ == "__main__":
    main()
