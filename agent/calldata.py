from __future__ import annotations

from eth_hash.auto import keccak


def function_selector(signature: str) -> str:
    return "0x" + keccak(signature.encode("utf-8"))[:4].hex()


def encode_uint256(value: int) -> str:
    if value < 0:
        raise ValueError("uint256 cannot be negative")
    return value.to_bytes(32, byteorder="big").hex()


def encode_address(address: str) -> str:
    clean = address.lower().removeprefix("0x")
    if len(clean) != 40:
        raise ValueError(f"invalid EVM address: {address}")
    return clean.rjust(64, "0")


def encode_approve_calldata(spender: str, amount: int) -> str:
    return function_selector("approve(address,uint256)") + encode_address(spender) + encode_uint256(amount)


def encode_deposit_calldata(amount: int) -> str:
    return function_selector("deposit(uint256)") + encode_uint256(amount)


APPROVE_SELECTOR = function_selector("approve(address,uint256)")
DEPOSIT_SELECTOR = function_selector("deposit(uint256)")
