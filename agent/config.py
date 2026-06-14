from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CAWPostingConfig:
    api_url: str
    api_key: str
    wallet_id: str
    src_address: str
    pact_api_key: str
    chain_id: str
    staking_addr: str
    usdc_addr: str
    usdc_symbol: str
    usdc_decimals: int
    max_stake_usdc: str
    pact_ttl_seconds: int
    max_tx_count: int
    post_sync_url: str

    @property
    def max_stake_raw(self) -> int:
        return token_units(self.max_stake_usdc, self.usdc_decimals)


def token_units(amount: str, decimals: int) -> int:
    whole, _, fraction = str(amount).partition(".")
    fraction = (fraction + ("0" * decimals))[:decimals]
    return int(whole or "0") * (10 ** decimals) + int(fraction or "0")


def load_caw_posting_config() -> CAWPostingConfig:
    return CAWPostingConfig(
        api_url=os.getenv("AGENT_WALLET_API_URL", ""),
        api_key=os.getenv("AGENT_WALLET_API_KEY", ""),
        wallet_id=os.getenv("AGENT_WALLET_WALLET_ID", ""),
        src_address=os.getenv("CAW_SRC_ADDRESS", ""),
        pact_api_key=os.getenv("CAW_PACT_API_KEY", ""),
        chain_id=os.getenv("CAW_CHAIN_ID", "BASE_ETH"),
        staking_addr=os.getenv("STAKING_ADDR", "0x6623Af17C813252CDBE29d062817fd27Bd865c35"),
        usdc_addr=os.getenv("USDC_ADDR", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
        usdc_symbol=os.getenv("USDC_SYMBOL", "USDC"),
        usdc_decimals=int(os.getenv("USDC_DECIMALS", "6")),
        max_stake_usdc=os.getenv("CAW_MAX_STAKE_USDC", "10"),
        pact_ttl_seconds=int(os.getenv("CAW_PACT_TTL_SECONDS", "86400")),
        max_tx_count=int(os.getenv("CAW_MAX_TX_COUNT", "3")),
        post_sync_url=os.getenv("UNIFIELDBBS_CREATE_POST_URL", "http://127.0.0.1:3000/api/create_post"),
    )



def load_caw_pairing_config(api_key_override: str | None = None) -> CAWPostingConfig:
    config = load_caw_posting_config()
    pairing_api_key = (api_key_override or os.getenv("CAW_PAIRING_API_KEY", "")).strip()
    if not pairing_api_key:
        raise RuntimeError("Missing CAW agent API key for this wallet binding")
    return replace(config, api_key=pairing_api_key, wallet_id="", src_address="", pact_api_key="")


def load_caw_bound_posting_config(
    wallet_id: str,
    wallet_address: str,
    *,
    api_key_override: str | None = None,
) -> CAWPostingConfig:
    if not wallet_id:
        raise RuntimeError("Missing paired CAW wallet id")
    if not wallet_address:
        raise RuntimeError("Missing paired CAW wallet address")
    config = load_caw_pairing_config(api_key_override=api_key_override)
    return replace(
        config,
        wallet_id=str(wallet_id).strip(),
        src_address=str(wallet_address).strip(),
    )
