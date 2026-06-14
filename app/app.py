import os
import json
import uuid
import base64
import hashlib
from datetime import datetime, timezone
import logging
import sys
import threading
import time

import requests
import markdown
from dotenv import load_dotenv
from flask import Flask, redirect, request, render_template, session, url_for, jsonify, Response
from supabase import create_client, Client
from supabase.lib.client_options import SyncClientOptions
from eth_hash.auto import keccak
from eth_account import Account
from eth_account.messages import encode_defunct
from cryptography.fernet import Fernet, InvalidToken

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from agent.calldata import encode_approve_calldata, encode_deposit_calldata
    from agent.caw_sdk_client import (
        contract_call,
        create_wallet_address,
        estimate_contract_call_fee,
        extract_pact_api_key,
        extract_pact_id,
        extract_tx_hash,
        get_pact,
        get_pair_info_by_wallet,
        get_wallet,
        initiate_wallet_pair,
        get_transaction_by_request_id,
        submit_pact,
        wait_for_pact_status,
    )
    from agent.config import (
        load_caw_bound_posting_config,
        load_caw_pairing_config,
        load_caw_posting_config,
        token_units,
    )
    from agent.caw_onboarding import onboard_mpc_wallet
    from agent.pact_builder import build_unifieldbbs_pact
    CAW_IMPORT_ERROR = None
except Exception as exc:
    CAW_IMPORT_ERROR = exc

try:
    from .reader_agent_core import DEFAULT_INTENT, recommend_posts
except ImportError:
    from reader_agent_core import DEFAULT_INTENT, recommend_posts

MESSAGE_TTL = 300  # 5 minutes

# Load environment variables before reading chain/app configuration.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
BASE_CHAIN_ID = int(os.getenv("BASE_CHAIN_ID", "8453"))
BASE_CHAIN_ID_HEX = os.getenv("BASE_CHAIN_ID_HEX", hex(BASE_CHAIN_ID))
BASE_CHAIN_NAME = os.getenv("BASE_CHAIN_NAME", "Base Mainnet")
BASE_BLOCK_EXPLORER_URL = os.getenv("BASE_BLOCK_EXPLORER_URL", "https://basescan.org")
STAKING_ADDR = os.getenv("STAKING_ADDR", "0x6623Af17C813252CDBE29d062817fd27Bd865c35")
USDC_ADDR = os.getenv("USDC_ADDR", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
USDC_SYMBOL = os.getenv("USDC_SYMBOL", "USDC")
USDC_DECIMALS = int(os.getenv("USDC_DECIMALS", "6"))
NETWORK_SLUG = os.getenv("NETWORK_SLUG", "base")
READER_AGENT_DEFAULT_INTENT = os.getenv("READER_AGENT_DEFAULT_INTENT", DEFAULT_INTENT)
DEFAULT_AGENT_MEMORY = os.getenv(
    "READER_AGENT_DEFAULT_MEMORY",
    "Prefer active, concrete posts with clear data/API/resource value. Avoid expired posts and vague marketing posts. Budget preference: <= 10 USDC."
)
VISIBILITY_MODES = {"public", "login_required", "paid_full_text"}

def get_event_topic(event_signature):
    return keccak(event_signature.encode('utf-8')).hex()

DEPOSIT_EVENT_TOPIC = get_event_topic("Deposited(address,uint256,uint256)")
WITHDRAW_EVENT_TOPIC = get_event_topic("Withdrawn(address,uint256,uint256)")

def verify_staking_event_tx_and_get_id(tx_hash, event_topic, event_name):
    try:
        response = requests.post(BASE_RPC_URL, json={
            "jsonrpc": "2.0",
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash],
            "id": 1
        }, timeout=10)
        
        if response.status_code != 200:
            return None, None, None, "RPC request failed"
        
        data = response.json()
        if "result" not in data or not data["result"]:
            return None, None, None, "TX not found or pending"
        
        receipt = data["result"]
        if receipt.get("status") != "0x1":
            return None, None, None, "TX failed on chain"
        
        logs = receipt.get("logs", [])
        staking_id = None
        indexed_address = None
        amount = None
        expected_topic = event_topic.replace("0x", "")
        
        for log in logs:
            if log.get("address", "").lower() == STAKING_ADDR.lower():
                topics = log.get("topics", [])
                data = log.get("data", "")
                if len(topics) == 2 and topics[0].replace("0x", "") == expected_topic:
                    indexed_address = '0x'+topics[1][-40:].lower()
                    staking_id = str(int(data[2:66], 16))
                    amount = str(int(data[66:130], 16))
                    break
        
        if not staking_id:
            return None, None, None, f"No valid {event_name} event found"
        
        return indexed_address, staking_id, amount, None
    except Exception as e:
        logger.error(f"Error verifying {event_name.lower()} tx: {str(e)}")
        return None, None, None, str(e)


def verify_staking_tx_and_get_id(tx_hash):
    return verify_staking_event_tx_and_get_id(tx_hash, DEPOSIT_EVENT_TOPIC, "Deposit")


def verify_withdraw_tx_and_get_id(tx_hash):
    return verify_staking_event_tx_and_get_id(tx_hash, WITHDRAW_EVENT_TOPIC, "Withdraw")


# def refresh_wallet_session():
#     user = session.get("user")
#     if not user or user.get("login_type") != "wallet" or not supabase:
#         return user

#     try:
#         response = supabase.table("wallets").select("*").eq("wallet_address", user["address"].lower()).limit(1).execute()
#         if response.data:
#             wallet = response.data[0]
#             user["created_at"] = wallet.get("created_at", user.get("created_at"))
#             session["user"] = user
#     except Exception as e:
#         logger.error(f"Failed to refresh wallet session for {user.get('address')}: {str(e)}")

#     return user

def get_or_create_user(address):
    address = address.lower()
    
    # Reset Postgrest auth to service role to avoid session pollution from Twitter login
    # service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    # if service_key:
    #     supabase.postgrest.auth(service_key)
        
    try:
        response = supabase.table("wallets").select("*").eq("wallet_address", address).execute()
        if response.data:
            supabase.table("wallets").update({"last_login": "now()"}).eq("wallet_address", address).execute()
            return response.data[0], False
        
        new_user = supabase.auth.admin.create_user({
            "email": f"{address}@wallet.local",
            "password": os.urandom(32).hex(),
            "email_confirm": True,
            "user_metadata": {"wallet_address": address}
        })
        user_id = new_user.user.id
        
        wallet_record = supabase.table("wallets").insert({
            "user_id": user_id,
            "wallet_address": address
        }).execute()
        
        return wallet_record.data[0], True
    except Exception as e:
        logger.error(f"❌ CRITICAL Error in get_or_create_user for address {address}: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise e


# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "your-secret-key-at-least-32-chars")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("❌ CRITICAL: Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env")

try:
    supabase: Client = create_client(
        SUPABASE_URL,
        SUPABASE_KEY,
        options=SyncClientOptions(auto_refresh_token=False, persist_session=False)
    ) if SUPABASE_URL else None
except Exception as e:
    logger.error(f"❌ Failed to initialize Supabase: {str(e)}")
    supabase = None

@app.before_request
def reset_supabase_auth():
    """
    Ensure the global supabase client is reset to service_role state 
    before each request to avoid session pollution between users.
    """
    if supabase and SUPABASE_KEY:
        supabase.postgrest.auth(SUPABASE_KEY)

def get_chain_config():
    return {
        "rpc_url": BASE_RPC_URL,
        "chain_id": BASE_CHAIN_ID,
        "chain_id_hex": BASE_CHAIN_ID_HEX,
        "chain_name": BASE_CHAIN_NAME,
        "block_explorer_url": BASE_BLOCK_EXPLORER_URL,
        "network_slug": NETWORK_SLUG,
        "staking_addr": STAKING_ADDR,
        "usdc_addr": USDC_ADDR,
        "usdc_symbol": USDC_SYMBOL,
        "usdc_decimals": USDC_DECIMALS,
    }


@app.context_processor
def inject_chain_config():
    return {"chain_config": get_chain_config()}


def parse_tags(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [tag.strip() for tag in value.split(",") if tag.strip()]
    return []


def format_token_amount(raw_amount):
    try:
        return str(int(raw_amount) / (10 ** USDC_DECIMALS))
    except Exception:
        return None


def extract_staking_id(post):
    staking_value = post.get("staking")
    if isinstance(staking_value, str) and ":" in staking_value:
        _, staking_suffix = staking_value.split(":", 1)
        return staking_suffix.strip()
    return None


def get_post_status(post):
    if post.get("status"):
        return post.get("status")
    if post.get("delete"):
        return "expired"
    if post.get("live"):
        return "active"
    return "draft"


def get_actor_type(post):
    if post.get("actor_type"):
        return post.get("actor_type")
    if post.get("posted_by"):
        return post.get("posted_by")
    if post.get("agent_wallet"):
        return "human_via_agent"
    return "human"


def get_visibility_mode(post):
    mode = post.get("visibility_mode") or "public"
    return mode if mode in VISIBILITY_MODES else "public"


def get_access_requirement(post):
    mode = get_visibility_mode(post)
    if mode == "login_required":
        return "login"
    if mode == "paid_full_text":
        return "paid_full_text"
    return "none"


def is_post_owner(user, post):
    if not user:
        return False
    return user.get("id") in {post.get("user"), post.get("author_id")}


def can_view_full_post(user, post):
    mode = get_visibility_mode(post)
    if mode == "public":
        return True
    if is_post_owner(user, post):
        return True
    if mode == "login_required":
        return bool(user)
    return False


def get_content_excerpt(post, limit=320):
    explicit = post.get("content_excerpt")
    if explicit:
        return explicit
    content = post.get("content") or ""
    compact = " ".join(str(content).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def normalize_post_for_agent(post, user=None):
    staking_id = extract_staking_id(post)
    raw_value = post.get("value")
    can_view_full = can_view_full_post(user, post)
    content = post.get("content") or ""
    return {
        "id": post.get("id"),
        "type": post.get("type") or "stake_backed_post",
        "title": post.get("title") or "[UNTITLED_TRANSMISSION]",
        "content": content if can_view_full else "",
        "content_excerpt": get_content_excerpt(post),
        "visibility_mode": get_visibility_mode(post),
        "access_requirement": get_access_requirement(post),
        "content_locked": not can_view_full,
        "category": post.get("category") or "general",
        "created_at": post.get("created_at"),
        "updated_at": post.get("updated_at"),
        "status": get_post_status(post),
        "actor_type": get_actor_type(post),
        "agent_wallet": post.get("agent_wallet"),
        "acceptance_criteria": (post.get("acceptance_criteria") or "") if can_view_full else "",
        "tags": parse_tags(post.get("tags")),
        "budget": post.get("budget"),
        "deadline": post.get("deadline"),
        "stake_amount": format_token_amount(raw_value),
        "stake_amount_raw": raw_value,
        "stake_token": USDC_SYMBOL,
        "staking_label": post.get("staking"),
        "staking_id": staking_id,
        "network": NETWORK_SLUG,
        "post_url": url_for("post_detail", post_id=post.get("id"), _external=True),
        "markdown_url": url_for("post_detail_md", post_id=post.get("id"), _external=True),
        "json_url": url_for("post_detail_json", post_id=post.get("id"), _external=True),
    }


def get_recent_agent_demo_posts(limit=5):
    posts = []
    if supabase:
        try:
            response = supabase.table("staking_posts") \
                .select("*") \
                .eq("delete", False) \
                .order("id", desc=True) \
                .limit(limit) \
                .execute()
            posts = response.data
        except Exception as e:
            logger.error(f"Error fetching agent demo posts: {str(e)}")

    return [normalize_post_for_agent(post, session.get("user")) for post in posts]


def get_active_posts_for_reader(user, category=None, limit=20):
    posts = []
    if supabase:
        try:
            query = supabase.table("staking_posts") \
                .select("*") \
                .eq("live", True) \
                .eq("delete", False)
            if category:
                query = query.eq("category", category)
            response = query \
                .order("id", desc=True) \
                .limit(limit) \
                .execute()
            posts = response.data
        except Exception as e:
            logger.error(f"Error fetching Reader Agent posts: {str(e)}")
    return [normalize_post_for_agent(post, user) for post in posts]


def get_default_memory_for_user(user):
    if user.get("login_type") == "wallet":
        return f"{DEFAULT_AGENT_MEMORY} Wallet identity: {user.get('address')}."
    return DEFAULT_AGENT_MEMORY


def get_or_create_agent_memory(user):
    if not supabase:
        raise RuntimeError("Supabase client not configured")

    response = supabase.table("agent_memories") \
        .select("*") \
        .eq("user_id", user["id"]) \
        .limit(1) \
        .execute()
    if response.data:
        return response.data[0], False

    payload = {
        "user_id": user["id"],
        "login_type": user.get("login_type"),
        "wallet_address": user.get("address"),
        "memory_text": get_default_memory_for_user(user),
    }
    created = supabase.table("agent_memories").insert(payload).execute()
    if not created.data:
        raise RuntimeError("Failed to create agent memory")
    return created.data[0], True


def get_reader_demo_state(user):
    if not user:
        return {
            "logged_in": False,
            "intent": READER_AGENT_DEFAULT_INTENT,
            "memory_text": "",
            "result": None,
            "open_posts": get_active_posts_for_reader(None, limit=5),
        }

    try:
        memory, _ = get_or_create_agent_memory(user)
        posts = get_active_posts_for_reader(user, limit=10)
        result = recommend_posts(
            posts,
            READER_AGENT_DEFAULT_INTENT,
            memory_text=memory.get("memory_text") or "",
            mode="auto",
            limit=3,
        )
        return {
            "logged_in": True,
            "intent": READER_AGENT_DEFAULT_INTENT,
            "memory_text": memory.get("memory_text") or "",
            "result": result,
            "open_posts": posts[:5],
        }
    except Exception as e:
        logger.error(f"Error building Reader Agent demo state: {str(e)}")
        return {
            "logged_in": True,
            "intent": READER_AGENT_DEFAULT_INTENT,
            "memory_text": "",
            "result": {
                "schema": "unifieldbbs.reader_recommendations.v1",
                "mode": "error",
                "intent": READER_AGENT_DEFAULT_INTENT,
                "memory_used": False,
                "source_count": 0,
                "recommendations": [],
                "error": str(e),
            },
            "open_posts": [],
        }




def to_plain_caw_payload(payload, _seen=None, _depth=0):
    if _seen is None:
        _seen = set()
    if payload is None:
        return None
    if isinstance(payload, (str, int, float, bool)):
        return payload
    if _depth > 8:
        return str(payload)
    payload_id = id(payload)
    if payload_id in _seen:
        return str(payload)
    _seen.add(payload_id)
    if isinstance(payload, list):
        return [to_plain_caw_payload(item, _seen, _depth + 1) for item in payload]
    if isinstance(payload, dict):
        return {key: to_plain_caw_payload(value, _seen, _depth + 1) for key, value in payload.items()}
    if hasattr(payload, "model_dump"):
        try:
            return to_plain_caw_payload(payload.model_dump(mode="json"), _seen, _depth + 1)
        except TypeError:
            return to_plain_caw_payload(payload.model_dump(), _seen, _depth + 1)
    if hasattr(payload, "dict"):
        return to_plain_caw_payload(payload.dict(), _seen, _depth + 1)
    return {"raw": str(payload)}

def extract_first_value(payload, keys):
    if payload is None:
        return ""
    payload = to_plain_caw_payload(payload)
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        for nested_key in ("result", "data", "wallet", "pair", "pair_info", "address", "agent"):
            found = extract_first_value(payload.get(nested_key), keys)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = extract_first_value(item, keys)
            if found:
                return found
    return ""


def caw_pairing_error(error):
    message = str(error)
    if "wallet_id" in message and "valid UUID" in message:
        return "CAW pairing config error: wallet id must be a UUID."
    if "pact authorization is not authorized for this wallet" in message:
        return "This CAW credential belongs to another wallet. Start a fresh pairing session."
    if "Stored CAW pairing credential cannot be decrypted" in message:
        return message
    if "Missing CAW environment variables" in message:
        return message
    return message[:500] + ("..." if len(message) > 500 else "")


def normalize_pair_status(raw_status):
    status = str(raw_status or "pending").strip().lower()
    if status in {"paired", "claimed", "confirmed", "completed", "success"}:
        return "paired"
    if status in {"expired", "timeout", "timed_out"}:
        return "expired"
    if status in {"failed", "rejected", "cancelled", "canceled"}:
        return "failed"
    return "pending"


def normalize_caw_pairing_payload(payload):
    payload = to_plain_caw_payload(payload)
    pair_payload = payload.get("pair", payload) if isinstance(payload, dict) else payload
    wallet_payload = payload.get("wallet", {}) if isinstance(payload, dict) else {}
    address_payload = payload.get("address", {}) if isinstance(payload, dict) else {}
    token = extract_first_value(pair_payload, ["token", "pair_token", "pairing_token"])
    explicit_code = extract_first_value(pair_payload, ["code", "pair_code", "pairing_code", "claim_code", "token_code"])
    pair_code = explicit_code or (token if token.isdigit() and 6 <= len(token) <= 10 else "")
    wallet_address = extract_first_value(address_payload, ["address", "wallet_address", "evm_address", "src_address"])
    if not wallet_address:
        wallet_address = extract_first_value(wallet_payload, ["address", "wallet_address", "evm_address", "src_address"])
    return {
        "pair_status": normalize_pair_status(extract_first_value(pair_payload, ["token_status", "pair_status", "status"])),
        "pair_token": token or extract_first_value(pair_payload, ["pair_id", "id"]),
        "pair_code": pair_code,
        "caw_wallet_address": wallet_address,
        "caw_agent_id": extract_first_value(pair_payload, ["agent_principal_id", "agent_id", "external_id"]),
        "caw_owner_id": extract_first_value(pair_payload, ["claimer_principal_id", "owner_id", "owner_uuid"]),
        "pair_payload": payload if isinstance(payload, dict) else {"raw": payload},
    }



def get_binding_pair_expiry(binding):
    payload = (binding or {}).get("pair_payload") or {}
    pair_payload = payload.get("pair", payload) if isinstance(payload, dict) else {}
    return extract_first_value(pair_payload, ["expires_at"])


def is_binding_pair_expired(binding):
    expires_at = get_binding_pair_expiry(binding)
    if not expires_at:
        return False
    try:
        expires_at = expires_at.replace("Z", "+00:00")
        expiry = datetime.fromisoformat(expires_at)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expiry
    except (TypeError, ValueError):
        return False


def caw_binding_wallet_type(binding):
    payload = (binding or {}).get("pair_payload") or {}
    return extract_first_value(payload, ["wallet_type"]).upper()


def caw_credential_cipher():
    secret = os.getenv("CAW_CREDENTIAL_ENCRYPTION_KEY") or app.secret_key
    if not secret or secret == "your-secret-key-at-least-32-chars":
        raise RuntimeError(
            "Set SECRET_KEY or CAW_CREDENTIAL_ENCRYPTION_KEY before using repeatable CAW pairing."
        )
    digest = hashlib.sha256(str(secret).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_caw_api_key(api_key):
    if not api_key:
        raise RuntimeError("CAW provisioning did not return an API key.")
    return caw_credential_cipher().encrypt(str(api_key).encode("utf-8")).decode("ascii")


def decrypt_caw_binding_api_key(binding):
    payload = (binding or {}).get("pair_payload") or {}
    private_payload = payload.get("_private") if isinstance(payload, dict) else {}
    encrypted = (private_payload or {}).get("agent_api_key")
    if not encrypted:
        return ""
    try:
        return caw_credential_cipher().decrypt(str(encrypted).encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "Stored CAW pairing credential cannot be decrypted. Keep SECRET_KEY stable across restarts."
        ) from exc


def public_caw_binding(binding):
    if not binding:
        return binding
    public = dict(binding)
    payload = public.get("pair_payload")
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.pop("_private", None)
        public["pair_payload"] = payload
    return public


def caw_config_for_binding(binding):
    dynamic_api_key = decrypt_caw_binding_api_key(binding)
    return load_caw_pairing_config(api_key_override=dynamic_api_key or None)


def pairing_response(binding, *, connected=False, reused_pending=False, rotated=False, wallet=None):
    public_binding = public_caw_binding(binding)
    return {
        "success": True,
        "schema": "unifieldbbs.caw_pairing.v3",
        "connected": connected,
        "binding": public_binding,
        "pairing": public_binding,
        "wallet": wallet or {"uuid": (public_binding or {}).get("caw_wallet_id")},
        "reused_pending": reused_pending,
        "rotated": rotated,
    }

def wait_for_caw_wallet_active(config, wallet_id, *, timeout_seconds=90, poll_seconds=3):
    started = time.monotonic()
    last_wallet = {}
    while time.monotonic() - started <= timeout_seconds:
        last_wallet = get_wallet(config, wallet_id=wallet_id)
        status = extract_first_value(last_wallet, ["status"]).lower()
        if status == "active":
            return last_wallet
        time.sleep(poll_seconds)
    raise TimeoutError(f"CAW wallet {wallet_id} did not become active within {timeout_seconds}s. Last status: {extract_first_value(last_wallet, ['status']) or 'unknown'}")


def get_caw_binding_by_wallet(wallet_id):
    if not supabase or not wallet_id:
        return None
    try:
        response = supabase.table("caw_wallet_bindings") \
            .select("*") \
            .eq("caw_wallet_id", wallet_id) \
            .limit(1) \
            .execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.info(f"caw_wallet_bindings lookup unavailable: {str(e)}")
        return None


def get_caw_binding_by_address(wallet_address):
    if not supabase or not wallet_address:
        return None
    try:
        response = supabase.table("caw_wallet_bindings") \
            .select("*") \
            .ilike("caw_wallet_address", str(wallet_address).strip()) \
            .limit(1) \
            .execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.info(f"caw_wallet_bindings lookup by address unavailable: {str(e)}")
        return None


def get_caw_binding_by_id(binding_id):
    if not supabase or not binding_id:
        return None
    try:
        response = supabase.table("caw_wallet_bindings") \
            .select("*") \
            .eq("id", binding_id) \
            .limit(1) \
            .execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.info(f"caw_wallet_bindings lookup by id unavailable: {str(e)}")
        return None


def upsert_caw_binding(wallet_id, payload, *, agent_api_key=None):
    if not supabase:
        return None
    data = {key: value for key, value in payload.items() if value not in (None, "")}
    data["caw_wallet_id"] = wallet_id
    if data.get("pair_status") == "paired":
        data["paired_at"] = datetime.now(timezone.utc).isoformat()
    existing = get_caw_binding_by_wallet(wallet_id)
    pair_payload = data.get("pair_payload")
    pair_payload = dict(pair_payload) if isinstance(pair_payload, dict) else {}
    existing_payload = (existing or {}).get("pair_payload") or {}
    if isinstance(existing_payload, dict):
        for key in ("provisioning",):
            if key not in pair_payload and key in existing_payload:
                pair_payload[key] = existing_payload[key]
    existing_private = existing_payload.get("_private") if isinstance(existing_payload, dict) else {}
    private_payload = dict(existing_private or {})
    if agent_api_key:
        private_payload["agent_api_key"] = encrypt_caw_api_key(agent_api_key)
    if private_payload:
        pair_payload["_private"] = private_payload
    if pair_payload:
        data["pair_payload"] = pair_payload
    if existing:
        response = supabase.table("caw_wallet_bindings").update(data).eq("id", existing["id"]).execute()
    else:
        response = supabase.table("caw_wallet_bindings").insert(data).execute()
    return response.data[0] if response.data else get_caw_binding_by_wallet(wallet_id)


def caw_owner_session_from_binding(binding):
    wallet_address = binding.get("caw_wallet_address") or ""
    return {
        "id": binding["id"],
        "name": "CAW Owner",
        "login_type": "caw_owner",
        "caw_binding_id": binding["id"],
        "caw_wallet_id": binding.get("caw_wallet_id"),
        "caw_wallet_address": wallet_address,
        "caw_owner_id": binding.get("caw_owner_id"),
    }


def get_caw_session_state():
    user = session.get("user")
    if user and user.get("login_type") == "caw_owner":
        return {
            "connected": True,
            "user": user,
            "binding": public_caw_binding(get_caw_binding_by_id(user.get("caw_binding_id"))),
        }
    pending_id = session.get("pending_caw_binding_id")
    pending = get_caw_binding_by_id(pending_id) if pending_id else None
    return {"connected": False, "user": None, "binding": public_caw_binding(pending)}


AGENT_RUN_STATUSES = {"queued", "executing", "verified", "failed"}
AGENT_RUN_RECONCILE_LOCK = threading.Lock()
AGENT_RUNS_RECONCILING = set()
DEFAULT_AGENT_REQUESTER = os.getenv("CAW_DEMO_REQUESTER_LABEL", "CAW-linked user")
DEFAULT_AGENT_GOAL = os.getenv(
    "CAW_DEMO_GOAL",
    "Publish a stake-backed signal proving that a CAW-controlled agent can enter the UnifieldBBS feed after real approve and deposit transactions."
)


def has_agent_run_value(run, key):
    if not run:
        return False
    value = run.get(key)
    return bool(value and str(value).strip() and str(value).strip() != "pending")


def is_agent_run_verified(run):
    return bool(
        run
        and run.get("status") == "verified"
        and has_agent_run_value(run, "deposit_tx")
        and has_agent_run_value(run, "post_id")
    )


def can_reconcile_agent_run(run):
    if not run or is_agent_run_verified(run):
        return False
    if not has_agent_run_value(run, "deposit_request_id"):
        return False
    if run.get("status") == "executing":
        return True
    error_message = str(run.get("error_message") or "").lower()
    return run.get("status") == "failed" and (
        "timed out waiting for caw transaction hash" in error_message
        or "cannot be broadcast" in error_message
        or "pendingbroadcast" in error_message
    )


def get_agent_run_steps(run):
    pact_submitted = has_agent_run_value(run, "pact_id")
    approval_finished = has_agent_run_value(run, "approve_tx")
    run_failed = run.get("status") == "failed"
    deposit_submitted = has_agent_run_value(run, "deposit_request_id")
    return [
        {"name": "Intent received", "status": "done" if run else "pending"},
        {"name": "Pact submitted", "status": "done" if pact_submitted else "pending"},
        {
            "name": "Owner approval / Pact active",
            "status": "done" if approval_finished else ("waiting in CAW App" if pact_submitted else "pending"),
        },
        {"name": "Approve submitted", "status": "done" if has_agent_run_value(run, "approve_request_id") else "pending"},
        {"name": "Approve confirmed", "status": "done" if has_agent_run_value(run, "approve_tx") else "pending"},
        {"name": "Deposit submitted", "status": "done" if has_agent_run_value(run, "deposit_request_id") else "pending"},
        {
            "name": "Deposit confirmed",
            "status": "done" if has_agent_run_value(run, "deposit_tx") else ("failed" if run_failed and deposit_submitted else "pending"),
        },
        {"name": "Post created", "status": "done" if has_agent_run_value(run, "post_id") else "pending"},
    ]


def normalize_agent_run(run):
    if not run:
        return None
    normalized = dict(run)
    normalized["status"] = normalized.get("status") or "queued"
    normalized["goal"] = normalized.get("goal") or DEFAULT_AGENT_GOAL
    normalized["category"] = normalized.get("category") or "signals"
    normalized["stake_amount"] = normalized.get("stake_amount") or os.getenv("CAW_DEMO_STAKE_AMOUNT", "0.005")
    normalized["visibility_mode"] = normalized.get("visibility_mode") or "public"
    normalized["acceptance_criteria"] = normalized.get("acceptance_criteria") or "Verified CAW approve and deposit tx create a live post in the BBS feed."
    normalized["tags"] = parse_tags(normalized.get("tags"))
    normalized["agent_wallet"] = normalized.get("agent_wallet") or os.getenv("CAW_AGENT_WALLET_ADDRESS") or os.getenv("AGENT_WALLET_WALLET_ID") or "not configured"
    normalized["requester_label"] = normalized.get("requester_label") or DEFAULT_AGENT_REQUESTER
    normalized["pact_id"] = normalized.get("pact_id") or "pending"
    normalized["approve_request_id"] = normalized.get("approve_request_id") or "pending"
    normalized["deposit_request_id"] = normalized.get("deposit_request_id") or "pending"
    normalized["approve_tx"] = normalized.get("approve_tx") or ""
    normalized["deposit_tx"] = normalized.get("deposit_tx") or ""
    normalized["post_id"] = normalized.get("post_id") or ""
    normalized["is_verified"] = is_agent_run_verified(normalized)
    normalized["can_reconcile"] = can_reconcile_agent_run(normalized)
    normalized["steps"] = get_agent_run_steps(normalized)
    if normalized.get("post_id"):
        normalized["post_url"] = url_for("post_detail", post_id=normalized["post_id"])
        normalized["post_json_url"] = url_for("post_detail_json", post_id=normalized["post_id"])
        normalized["post_md_url"] = url_for("post_detail_md", post_id=normalized["post_id"])
    else:
        normalized["post_url"] = ""
        normalized["post_json_url"] = ""
        normalized["post_md_url"] = ""
    normalized["feed_url"] = url_for("feed_json", category=normalized["category"])
    return normalized


def get_recorded_agent_run():
    return normalize_agent_run({
        "id": os.getenv("CAW_DEMO_AGENT_RUN_ID", "recorded-sepolia-run"),
        "status": "verified" if os.getenv("CAW_DEMO_DEPOSIT_TX") else "queued",
        "requester_label": DEFAULT_AGENT_REQUESTER,
        "goal": DEFAULT_AGENT_GOAL,
        "category": os.getenv("CAW_DEMO_CATEGORY", "signals"),
        "stake_amount": os.getenv("CAW_DEMO_STAKE_AMOUNT", "0.005"),
        "visibility_mode": os.getenv("CAW_DEMO_VISIBILITY", "public"),
        "acceptance_criteria": os.getenv("CAW_DEMO_ACCEPTANCE", "Verified CAW approve and deposit tx create a live post in the BBS feed."),
        "tags": parse_tags(os.getenv("CAW_DEMO_TAGS", "caw,agent-posting,stake-backed")),
        "agent_wallet": os.getenv("CAW_AGENT_WALLET_ADDRESS") or os.getenv("AGENT_WALLET_WALLET_ID") or "not configured",
        "pact_id": os.getenv("CAW_DEMO_PACT_ID", "pact_pending"),
        "approve_request_id": os.getenv("CAW_DEMO_APPROVE_REQUEST_ID", "req_approve_pending"),
        "approve_tx": os.getenv("CAW_DEMO_APPROVE_TX", ""),
        "deposit_request_id": os.getenv("CAW_DEMO_DEPOSIT_REQUEST_ID", "req_deposit_pending"),
        "deposit_tx": os.getenv("CAW_DEMO_DEPOSIT_TX", ""),
        "post_id": os.getenv("CAW_DEMO_POST_ID", ""),
        "created_at": os.getenv("CAW_DEMO_CREATED_AT", "recorded receipt"),
    })


def get_agent_runs(limit=5):
    recorded = get_recorded_agent_run()
    persisted_runs = []
    if supabase:
        try:
            response = supabase.table("agent_runs") \
                .select("*") \
                .order("created_at", desc=True) \
                .limit(limit) \
                .execute()
            persisted_runs = [normalize_agent_run(run) for run in response.data]
        except Exception as e:
            logger.info(f"agent_runs unavailable, using recorded CAW run: {str(e)}")

    runs = []
    for run in persisted_runs:
        if not any(str(existing.get("id")) == str(run.get("id")) for existing in runs):
            runs.append(run)
    if recorded and not any(str(existing.get("id")) == str(recorded.get("id")) for existing in runs):
        runs.append(recorded)
    return runs[:limit]


def get_agent_run_by_id(agent_run_id):
    if not agent_run_id or not supabase:
        return None
    try:
        response = supabase.table("agent_runs") \
            .select("*") \
            .eq("id", agent_run_id) \
            .limit(1) \
            .execute()
        if response.data:
            return normalize_agent_run(response.data[0])
    except Exception as e:
        logger.info(f"agent_run lookup skipped: {str(e)}")
    return None


def update_agent_run_after_verified_tx(agent_run_id, *, indexed_address, tx_hash, post_id, data=None):
    if not agent_run_id or not supabase:
        return
    data = data or {}
    update_payload = {
        "status": "verified",
        "agent_wallet": data.get("agent_wallet") or indexed_address,
        "deposit_tx": tx_hash,
        "post_id": post_id,
        "error_message": None,
    }
    for incoming_key, db_key in {
        "pact_id": "pact_id",
        "approve_request_id": "approve_request_id",
        "approve_tx": "approve_tx",
        "deposit_request_id": "deposit_request_id",
    }.items():
        value = data.get(incoming_key)
        if value:
            update_payload[db_key] = value
    supabase.table("agent_runs").update(update_payload).eq("id", agent_run_id).execute()



def present_agent_value(value):
    return bool(value and str(value).strip() and str(value).strip() != "pending")


def get_agent_run_record_by_id(agent_run_id):
    if not agent_run_id or not supabase:
        return None
    try:
        response = supabase.table("agent_runs") \
            .select("*") \
            .eq("id", agent_run_id) \
            .limit(1) \
            .execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.info(f"agent_run raw lookup skipped: {str(e)}")
        return None


def agent_run_request_ids(agent_run_id):
    short_id = str(agent_run_id).split("-")[0][:8]
    return {
        "approve_request_id": f"unifieldbbs-{short_id}-approve",
        "deposit_request_id": f"unifieldbbs-{short_id}-deposit",
    }


def update_agent_run_fields(agent_run_id, **fields):
    if not agent_run_id or not supabase:
        return None
    payload = {key: value for key, value in fields.items() if value not in (None, "")}
    if not payload:
        return get_agent_run_record_by_id(agent_run_id)
    response = supabase.table("agent_runs").update(payload).eq("id", agent_run_id).execute()
    return response.data[0] if response.data else get_agent_run_record_by_id(agent_run_id)


def build_agent_run_caw_plan(agent_run):
    if CAW_IMPORT_ERROR:
        raise RuntimeError(f"CAW modules unavailable: {CAW_IMPORT_ERROR}")
    binding = get_caw_binding_by_address(agent_run.get("agent_wallet"))
    if not binding:
        raise RuntimeError("CAW paired wallet binding was not found for this Agent request.")
    if binding.get("pair_status") != "paired":
        raise RuntimeError("CAW wallet is not paired. Complete Owner Pairing before posting.")
    config = load_caw_bound_posting_config(
        binding.get("caw_wallet_id"),
        binding.get("caw_wallet_address"),
        api_key_override=decrypt_caw_binding_api_key(binding) or None,
    )
    stake_usdc = str(agent_run.get("stake_amount") or os.getenv("CAW_DEMO_STAKE_AMOUNT", "0.005"))
    stake_raw = token_units(stake_usdc, config.usdc_decimals)
    if stake_raw <= 0:
        raise ValueError("stake_amount must be greater than zero")
    if stake_raw > config.max_stake_raw:
        raise ValueError(f"stake {stake_usdc} {config.usdc_symbol} exceeds CAW_MAX_STAKE_USDC={config.max_stake_usdc}")

    title = (agent_run.get("goal") or "Agent-posted stake-backed request").strip()[:80]
    category = agent_run.get("category") or "signals"
    request_ids = agent_run_request_ids(agent_run["id"])
    return config, {
        "pact_spec": build_unifieldbbs_pact(config, title=title, category=category),
        "approve_request_id": request_ids["approve_request_id"],
        "deposit_request_id": request_ids["deposit_request_id"],
        "approve_call": {
            "label": "approve_usdc",
            "chain_id": config.chain_id,
            "contract_addr": config.usdc_addr,
            "calldata": encode_approve_calldata(config.staking_addr, stake_raw),
            "value": "0",
            "request_id": request_ids["approve_request_id"],
            "description": f"UnifieldBBS approve {stake_usdc} {config.usdc_symbol} for agent_run {agent_run['id']}",
        },
        "deposit_call": {
            "label": "deposit_stake",
            "chain_id": config.chain_id,
            "contract_addr": config.staking_addr,
            "calldata": encode_deposit_calldata(stake_raw),
            "value": "0",
            "request_id": request_ids["deposit_request_id"],
            "description": f"UnifieldBBS deposit {stake_usdc} {config.usdc_symbol} for agent_run {agent_run['id']}",
        },
    }


def compact_executor_error(error):
    message = str(error)
    if "CAW_NATIVE_GAS_INSUFFICIENT" in message:
        return message.split("CAW_NATIVE_GAS_INSUFFICIENT:", 1)[-1].strip()
    if "gas required exceeds allowance" in message:
        return "CAW could not build the deposit transaction because its automatic gas limit was too low. Create a new request after restarting Flask; the executor now supplies CAW's recommended gas limit."
    if "(404)" in message or "Reason: Not Found" in message:
        return "CAW wallet was not found for the active credentials. Pair again, then create a new Agent request."
    if "paired wallet binding was not found" in message:
        return message
    if "CAW wallet is not paired" in message:
        return message
    if "wallet_id" in message and "valid UUID" in message:
        return "CAW config error: AGENT_WALLET_WALLET_ID must be the wallet UUID, not the agent/external id."
    if "function_abis is required" in message:
        return "CAW Pact schema error: function_abis must be inside rules when params_match is used. Restart Flask after updating the Pact builder."
    if "function_abis[0].name" in message:
        return "CAW Pact schema error: function_abis entries must include a non-empty function name. Restart Flask after updating the Pact builder."
    if "Policy rules do not match" in message:
        return "CAW Pact policy schema error: policy rules do not match Cobo's expected contract_call schema."
    if "Missing CAW environment variables" in message:
        return message
    return message[:500] + ("..." if len(message) > 500 else "")


def caw_tx_status(record):
    if not isinstance(record, dict):
        return ""
    for key in ("status_display", "sub_status", "status"):
        value = record.get(key)
        if value:
            status = str(value).strip().lower()
            if status.isdigit():
                continue
            return status
    return ""


def is_caw_tx_failed(record):
    status = caw_tx_status(record)
    return status in {"failed", "rejected", "denied", "cancelled", "canceled"}


def is_caw_tx_completed(record):
    status = caw_tx_status(record)
    return status in {"completed", "complete", "success", "succeeded"}


def get_caw_tx_sub_status(record):
    if not isinstance(record, dict):
        return ""
    return str(record.get("sub_status") or "").strip().lower()


def get_caw_fee_reserve_wei(payload):
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    for key in ("fee", "recommended"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("fee"), dict):
        candidates.append(data["fee"])
    for ext_transaction in payload.get("ext_transactions") or []:
        ext_data = ext_transaction.get("data") if isinstance(ext_transaction, dict) else None
        if isinstance(ext_data, dict) and isinstance(ext_data.get("fee"), dict):
            candidates.append(ext_data["fee"])
    for candidate in candidates:
        try:
            gas_limit = int(candidate.get("gas_limit") or 0)
            max_fee_per_gas = int(candidate.get("max_fee_per_gas") or 0)
        except (TypeError, ValueError):
            continue
        if gas_limit > 0 and max_fee_per_gas > 0:
            return gas_limit * max_fee_per_gas
    return None


def get_native_balance_wei(address):
    if not address:
        return None
    try:
        response = requests.post(
            BASE_RPC_URL,
            json={
                "jsonrpc": "2.0",
                "method": "eth_getBalance",
                "params": [address, "latest"],
                "id": 1,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        return int(payload["result"], 16)
    except Exception as e:
        logger.info(f"Native gas balance preflight skipped for {address}: {str(e)}")
        return None


def ensure_caw_native_gas_balance(config, fee_payload, request_id):
    reserve_wei = get_caw_fee_reserve_wei(fee_payload)
    balance_wei = get_native_balance_wei(config.src_address)
    if reserve_wei is None or balance_wei is None or balance_wei >= reserve_wei:
        return
    shortfall_wei = reserve_wei - balance_wei
    raise RuntimeError(
        "CAW_NATIVE_GAS_INSUFFICIENT: "
        f"Deposit is signed but cannot be broadcast. Wallet has {balance_wei / 10**18:.6f} "
        f"{config.chain_id}; CAW requires up to {reserve_wei / 10**18:.6f} for gas. "
        f"Top up at least {shortfall_wei / 10**18:.6f} {config.chain_id}, then retry the same "
        f"Agent request ({request_id}); its deterministic request ID will resume without duplicating the transaction."
    )


def get_caw_tx_record(config, request_id, pact_api_key=None):
    try:
        return get_transaction_by_request_id(config, request_id, api_key_override=pact_api_key)
    except Exception as e:
        logger.info(f"CAW transaction lookup skipped for {request_id}: {str(e)}")
        return None


def wait_for_caw_tx_hash(config, request_id, *, pact_api_key=None, timeout_seconds=240, poll_seconds=5):
    deadline = time.monotonic() + timeout_seconds
    last_record = None
    while time.monotonic() < deadline:
        record = get_caw_tx_record(config, request_id, pact_api_key=pact_api_key)
        if record:
            last_record = record
            if is_caw_tx_failed(record):
                raise RuntimeError(f"CAW transaction {request_id} failed: {record}")
            tx_hash = extract_tx_hash(record)
            if tx_hash and is_caw_tx_completed(record):
                return tx_hash, record
            if not tx_hash and get_caw_tx_sub_status(record) == "pendingbroadcast":
                ensure_caw_native_gas_balance(config, record, request_id)
        time.sleep(poll_seconds)
    tx_hash = extract_tx_hash(last_record) if last_record else None
    if tx_hash:
        return tx_hash, last_record
    last_status = caw_tx_status(last_record) or "unknown"
    last_sub_status = get_caw_tx_sub_status(last_record) or "unknown"
    raise TimeoutError(
        f"Timed out waiting for CAW transaction hash: {request_id}. "
        f"Last CAW state: {last_status}/{last_sub_status}."
    )


def ensure_active_pact_for_agent_run(config, agent_run, plan):
    pact_id = agent_run.get("pact_id")
    pact = None
    pact_api_key = None
    if present_agent_value(pact_id):
        try:
            pact = get_pact(config, pact_id)
            pact_status = str(pact.get("status") or "").strip().lower()
            if pact_status == "active":
                pact_api_key = extract_pact_api_key(pact)
                return pact_id, pact_api_key, pact
        except Exception as e:
            logger.info(f"Existing pact unavailable for agent_run {agent_run.get('id')}: {str(e)}")

    pact_response = submit_pact(config, plan["pact_spec"])
    pact_id = extract_pact_id(pact_response)
    if not pact_id:
        raise RuntimeError(f"CAW submit_pact did not return pact_id: {pact_response}")
    update_agent_run_fields(agent_run["id"], pact_id=pact_id)
    pact = wait_for_pact_status(
        config,
        pact_id,
        timeout_seconds=int(os.getenv("CAW_PACT_APPROVAL_TIMEOUT_SECONDS", "600")),
        poll_seconds=5,
    )
    pact_api_key = extract_pact_api_key(pact) or extract_pact_api_key(pact_response)
    return pact_id, pact_api_key, pact


def ensure_caw_contract_call(config, call, *, pact_id, pact_api_key=None):
    request_id = call["request_id"]
    record = get_caw_tx_record(config, request_id, pact_api_key=pact_api_key)
    if record:
        if is_caw_tx_failed(record):
            raise RuntimeError(f"Existing CAW transaction {request_id} failed: {record}")
        tx_hash = extract_tx_hash(record)
        if tx_hash and is_caw_tx_completed(record):
            return tx_hash, record
        return wait_for_caw_tx_hash(config, request_id, pact_api_key=pact_api_key)

    call_with_source = {**call, "src_addr": config.src_address}
    fee_estimate = estimate_contract_call_fee(
        config,
        call_with_source,
    )
    recommended = fee_estimate.get("recommended") if isinstance(fee_estimate, dict) else None
    if recommended:
        call_with_source["fee"] = {
            "fee_type": fee_estimate.get("fee_type") or "EVM_EIP_1559",
            "max_fee_per_gas": str(recommended["max_fee_per_gas"]),
            "max_priority_fee_per_gas": str(recommended["max_priority_fee_per_gas"]),
            "gas_limit": str(recommended["gas_limit"]),
            "token_id": fee_estimate.get("token_id") or config.chain_id,
        }
        ensure_caw_native_gas_balance(config, call_with_source["fee"], request_id)
    response = contract_call(config, call_with_source, api_key_override=pact_api_key)
    if is_caw_tx_failed(response):
        raise RuntimeError(f"CAW contract call {request_id} failed: {response}")
    return wait_for_caw_tx_hash(config, request_id, pact_api_key=pact_api_key)


def execute_agent_run_backend(agent_run_id):
    agent_run = get_agent_run_record_by_id(agent_run_id)
    if not agent_run:
        raise RuntimeError("AGENT_RUN_NOT_FOUND")
    if agent_run.get("status") == "verified" and agent_run.get("post_id"):
        return get_agent_run_record_by_id(agent_run_id)

    config, plan = build_agent_run_caw_plan(agent_run)
    update_agent_run_fields(
        agent_run_id,
        status="executing",
        agent_wallet=config.src_address or os.getenv("CAW_AGENT_WALLET_ADDRESS"),
        approve_request_id=plan["approve_request_id"],
        deposit_request_id=plan["deposit_request_id"],
    )

    try:
        pact_id, pact_api_key, _ = ensure_active_pact_for_agent_run(config, agent_run, plan)
        update_agent_run_fields(agent_run_id, pact_id=pact_id)

        approve_tx, _ = ensure_caw_contract_call(config, plan["approve_call"], pact_id=pact_id, pact_api_key=pact_api_key)
        update_agent_run_fields(agent_run_id, approve_tx=approve_tx)

        deposit_tx, _ = ensure_caw_contract_call(config, plan["deposit_call"], pact_id=pact_id, pact_api_key=pact_api_key)
        post_payload, status_code = create_post_from_deposit_tx(
            deposit_tx,
            category=agent_run.get("category"),
            agent_run_id=agent_run_id,
            data={
                "agent_run_id": agent_run_id,
                "pact_id": pact_id,
                "approve_request_id": plan["approve_request_id"],
                "approve_tx": approve_tx,
                "deposit_request_id": plan["deposit_request_id"],
                "agent_wallet": config.src_address or os.getenv("CAW_AGENT_WALLET_ADDRESS"),
                "visibility_mode": agent_run.get("visibility_mode"),
            },
        )
        if status_code >= 400 or not post_payload.get("success"):
            raise RuntimeError(f"Post creation failed after verified CAW deposit: {post_payload}")
        update_agent_run_fields(agent_run_id, status="verified", deposit_tx=deposit_tx, post_id=post_payload.get("post_id"))
        return get_agent_run_record_by_id(agent_run_id)
    except Exception as e:
        update_agent_run_fields(agent_run_id, status="failed", error_message=compact_executor_error(e))
        raise


def reconcile_agent_run_backend(agent_run_id):
    agent_run = get_agent_run_record_by_id(agent_run_id)
    if not agent_run or is_agent_run_verified(agent_run):
        return agent_run

    config, plan = build_agent_run_caw_plan(agent_run)
    pact_id = agent_run.get("pact_id")
    if not present_agent_value(pact_id):
        return agent_run

    pact = get_pact(config, pact_id)
    pact_api_key = extract_pact_api_key(pact)

    approve_record = get_caw_tx_record(
        config,
        plan["approve_request_id"],
        pact_api_key=pact_api_key,
    )
    approve_tx = extract_tx_hash(approve_record) if approve_record and is_caw_tx_completed(approve_record) else None
    if approve_tx:
        update_agent_run_fields(agent_run_id, approve_tx=approve_tx)

    deposit_record = get_caw_tx_record(
        config,
        plan["deposit_request_id"],
        pact_api_key=pact_api_key,
    )
    if not deposit_record:
        return get_agent_run_record_by_id(agent_run_id)
    if is_caw_tx_failed(deposit_record):
        error = compact_executor_error(
            RuntimeError(f"CAW transaction {plan['deposit_request_id']} failed: {deposit_record}")
        )
        update_agent_run_fields(agent_run_id, status="failed", error_message=error)
        return get_agent_run_record_by_id(agent_run_id)

    deposit_tx = extract_tx_hash(deposit_record)
    if not deposit_tx or not is_caw_tx_completed(deposit_record):
        return get_agent_run_record_by_id(agent_run_id)

    update_agent_run_fields(agent_run_id, status="executing", deposit_tx=deposit_tx)
    post_payload, status_code = create_post_from_deposit_tx(
        deposit_tx,
        category=agent_run.get("category"),
        agent_run_id=agent_run_id,
        data={
            "agent_run_id": agent_run_id,
            "pact_id": pact_id,
            "approve_request_id": plan["approve_request_id"],
            "approve_tx": approve_tx or agent_run.get("approve_tx"),
            "deposit_request_id": plan["deposit_request_id"],
            "agent_wallet": config.src_address or os.getenv("CAW_AGENT_WALLET_ADDRESS"),
            "visibility_mode": agent_run.get("visibility_mode"),
        },
    )
    if status_code >= 400 or not post_payload.get("success"):
        raise RuntimeError(f"Post reconciliation failed after verified CAW deposit: {post_payload}")

    update_agent_run_fields(
        agent_run_id,
        status="verified",
        deposit_tx=deposit_tx,
        post_id=post_payload.get("post_id"),
    )
    return get_agent_run_record_by_id(agent_run_id)


def run_agent_reconcile_thread(agent_run_id):
    try:
        reconcile_agent_run_backend(agent_run_id)
    except Exception as e:
        error_message = compact_executor_error(e)
        update_agent_run_fields(agent_run_id, status="failed", error_message=error_message)
        logger.error(f"Agent reconciliation failed for {agent_run_id}: {str(e)}")
    finally:
        with AGENT_RUN_RECONCILE_LOCK:
            AGENT_RUNS_RECONCILING.discard(str(agent_run_id))


def schedule_agent_run_reconciliation(agent_run_id):
    run_id = str(agent_run_id)
    with AGENT_RUN_RECONCILE_LOCK:
        if run_id in AGENT_RUNS_RECONCILING:
            return False
        AGENT_RUNS_RECONCILING.add(run_id)
    thread = threading.Thread(target=run_agent_reconcile_thread, args=(run_id,), daemon=True)
    thread.start()
    return True


def run_agent_executor_thread(agent_run_id):
    try:
        execute_agent_run_backend(agent_run_id)
    except Exception as e:
        logger.error(f"Agent executor failed for {agent_run_id}: {str(e)}")
def get_caw_demo_state():
    max_stake = os.getenv("CAW_MAX_STAKE_USDC", "10")
    wallet_address = os.getenv("CAW_AGENT_WALLET_ADDRESS") or os.getenv("AGENT_WALLET_WALLET_ID") or "not configured"
    pact_id = os.getenv("CAW_DEMO_PACT_ID", "pact_pending")
    approve_request_id = os.getenv("CAW_DEMO_APPROVE_REQUEST_ID", "req_approve_pending")
    deposit_request_id = os.getenv("CAW_DEMO_DEPOSIT_REQUEST_ID", "req_deposit_pending")
    approve_tx = os.getenv("CAW_DEMO_APPROVE_TX", "")
    deposit_tx = os.getenv("CAW_DEMO_DEPOSIT_TX", "")
    post_id = os.getenv("CAW_DEMO_POST_ID", "")
    policy_status = os.getenv("CAW_DEMO_POLICY_STATUS", "ready for review")

    return {
        "agent_wallet": wallet_address,
        "pact_id": pact_id,
        "approve_request_id": approve_request_id,
        "deposit_request_id": deposit_request_id,
        "approve_tx": approve_tx,
        "deposit_tx": deposit_tx,
        "post_id": post_id,
        "policy_status": policy_status,
        "boundary": {
            "chain": os.getenv("CAW_CHAIN_ID", "BASE_ETH"),
            "token": USDC_SYMBOL,
            "staking_contract": STAKING_ADDR,
            "token_contract": USDC_ADDR,
            "max_stake": f"{max_stake} {USDC_SYMBOL}",
            "max_tx_count": os.getenv("CAW_MAX_TX_COUNT", "2"),
            "ttl": os.getenv("CAW_PACT_TTL_SECONDS", "86400"),
        },
        "steps": [
            {"name": "Intent drafted", "status": "done"},
            {"name": "Pact boundary prepared", "status": "done"},
            {"name": "Human approval", "status": "active" if pact_id == "pact_pending" else "done"},
            {"name": "USDC approve", "status": "done" if approve_tx else "pending"},
            {"name": "Stake deposit", "status": "done" if deposit_tx else "pending"},
            {"name": "Post enters feed", "status": "done" if post_id else "pending"},
        ],
    }



@app.route("/api/caw/session", methods=["GET"])
def api_caw_session():
    state = get_caw_session_state()
    return jsonify({
        "schema": "unifieldbbs.caw_session.v1",
        "connected": state["connected"],
        "user": state["user"],
        "binding": state["binding"],
    })


@app.route("/api/caw/pairing/start", methods=["POST"])
def api_caw_pairing_start():
    if CAW_IMPORT_ERROR:
        return jsonify({"error": f"CAW modules unavailable: {CAW_IMPORT_ERROR}"}), 500
    if not supabase:
        return jsonify({"error": "Supabase client not configured"}), 500
    try:
        existing_pending = get_caw_binding_by_id(session.get("pending_caw_binding_id"))
        existing_wallet_type = caw_binding_wallet_type(existing_pending)
        if existing_pending and existing_wallet_type and existing_wallet_type != "MPC":
            logger.info(
                "Discarding non-MPC CAW pairing binding %s (%s).",
                existing_pending.get("id"),
                existing_wallet_type,
            )
            session.pop("pending_caw_binding_id", None)
            session.modified = True
            existing_pending = None

        if existing_pending and existing_pending.get("pair_status") == "pending":
            wallet_id = existing_pending.get("caw_wallet_id")
            try:
                config = caw_config_for_binding(existing_pending)
                pair_info = get_pair_info_by_wallet(config, wallet_id=wallet_id)
                refreshed = normalize_caw_pairing_payload({"pair": pair_info})
                if not refreshed.get("caw_wallet_address"):
                    refreshed["caw_wallet_address"] = existing_pending.get("caw_wallet_address")

                expired = is_binding_pair_expired(existing_pending) or refreshed.get("pair_status") == "expired"
                if expired or not refreshed.get("pair_code"):
                    pair_info = initiate_wallet_pair(config, wallet_id=wallet_id)
                    refreshed = normalize_caw_pairing_payload({"pair": pair_info})
                    if not refreshed.get("caw_wallet_address"):
                        refreshed["caw_wallet_address"] = existing_pending.get("caw_wallet_address")
                    updated = upsert_caw_binding(wallet_id, refreshed) or existing_pending
                    return jsonify(pairing_response(updated, rotated=True))

                updated = upsert_caw_binding(wallet_id, refreshed) or existing_pending
                if updated.get("pair_status") == "paired":
                    session["user"] = caw_owner_session_from_binding(updated)
                    session.pop("pending_caw_binding_id", None)
                    session.modified = True
                    return jsonify(pairing_response(updated, connected=True))
                return jsonify(pairing_response(updated, reused_pending=True))
            except Exception as stale_error:
                logger.info(f"Discarding unrecoverable CAW pairing binding {existing_pending.get('id')}: {str(stale_error)}")
                session.pop("pending_caw_binding_id", None)
                session.modified = True

        short_id = uuid.uuid4().hex[:8]
        onboarding = onboard_mpc_wallet(
            agent_name=f"UnifieldBBS Pairing {short_id}",
            api_url=os.getenv("AGENT_WALLET_API_URL", ""),
            timeout_seconds=int(os.getenv("CAW_ONBOARD_TIMEOUT_SECONDS", "240")),
        )
        agent_api_key = onboarding["api_key"]
        agent_id = onboarding["agent_id"]
        wallet_id = onboarding["wallet_id"]
        config = load_caw_pairing_config(api_key_override=agent_api_key)
        wallet_response = wait_for_caw_wallet_active(config, wallet_id)
        address_response = {}
        try:
            address_response = create_wallet_address(config, wallet_id, chain_type="ETH")
        except Exception as address_error:
            logger.info(f"CAW wallet address creation skipped: {str(address_error)}")

        pair_response = initiate_wallet_pair(config, wallet_id=wallet_id)
        normalized = normalize_caw_pairing_payload({
            "pair": pair_response,
            "wallet": wallet_response,
            "address": address_response,
        })
        wallet_address = extract_first_value(address_response, ["address", "wallet_address", "evm_address"])
        if wallet_address and not normalized.get("caw_wallet_address"):
            normalized["caw_wallet_address"] = wallet_address
        if agent_id and not normalized.get("caw_agent_id"):
            normalized["caw_agent_id"] = agent_id
        normalized["pair_payload"]["provisioning"] = {
            "agent_id": agent_id,
            "wallet_type": "MPC",
            "profile_name": onboarding.get("profile_name"),
            "profile_path": onboarding.get("profile_path"),
            "runtime": onboarding.get("runtime"),
        }
        binding = upsert_caw_binding(
            wallet_id,
            normalized,
            agent_api_key=agent_api_key,
        )
        if binding:
            session["pending_caw_binding_id"] = binding["id"]
            session.modified = True
        return jsonify(
            pairing_response(
                binding or {**normalized, "caw_wallet_id": wallet_id},
                wallet=wallet_response,
            )
        )
    except Exception as e:
        logger.error(f"Error starting CAW pairing: {str(e)}")
        return jsonify({"error": caw_pairing_error(e)}), 500


@app.route("/api/caw/pairing/status", methods=["GET"])
def api_caw_pairing_status():
    if CAW_IMPORT_ERROR:
        return jsonify({"error": f"CAW modules unavailable: {CAW_IMPORT_ERROR}"}), 500
    try:
        state = get_caw_session_state()
        public_binding = state.get("binding")
        if state.get("connected"):
            return jsonify(pairing_response(public_binding, connected=True))

        binding = get_caw_binding_by_id(session.get("pending_caw_binding_id"))
        if not binding:
            return jsonify({
                "success": True,
                "schema": "unifieldbbs.caw_pairing.v3",
                "connected": False,
                "binding": None,
                "pairing": None,
                "message": "No pending CAW pairing request in this browser session.",
            })

        config = caw_config_for_binding(binding)
        wallet_id = binding.get("caw_wallet_id")
        pair_info = get_pair_info_by_wallet(config, wallet_id=wallet_id)
        wallet_response = {}
        try:
            wallet_response = get_wallet(config, wallet_id=wallet_id)
        except Exception as wallet_error:
            logger.info(f"CAW wallet metadata lookup skipped: {str(wallet_error)}")
        normalized = normalize_caw_pairing_payload({"pair": pair_info, "wallet": wallet_response})
        if not normalized.get("pair_code"):
            normalized["pair_code"] = binding.get("pair_code")
        if not normalized.get("caw_wallet_address"):
            normalized["caw_wallet_address"] = binding.get("caw_wallet_address")
        updated = upsert_caw_binding(wallet_id, normalized) or binding
        if updated and updated.get("pair_status") == "paired":
            session["user"] = caw_owner_session_from_binding(updated)
            session.pop("pending_caw_binding_id", None)
            session.modified = True
        return jsonify(
            pairing_response(
                updated,
                connected=bool(updated and updated.get("pair_status") == "paired"),
                wallet=wallet_response,
            )
        )
    except Exception as e:
        logger.error(f"Error checking CAW pairing status: {str(e)}")
        return jsonify({"error": caw_pairing_error(e)}), 500

@app.route("/agent-post")
@app.route("/post")
def agent_post():
    user = session.get("user")
    recent_posts = get_recent_agent_demo_posts()
    agent_runs = get_agent_runs()
    active_run = agent_runs[0] if agent_runs else get_recorded_agent_run()
    return render_template(
        "post.html",
        user=user,
        demo=get_caw_demo_state(),
        agent_runs=agent_runs,
        active_run=active_run,
        recent_posts=recent_posts,
        reader=get_reader_demo_state(user),
        caw_session=get_caw_session_state(),
    )


@app.route("/agent-demo")
def agent_demo_legacy():
    return redirect(url_for("agent_post"), code=302)



@app.route("/reader")
def reader_agent():
    user = session.get("user")
    memory_text = ""
    memory_error = ""

    if user:
        if "login_type" not in user or "id" not in user:
            session.pop("user", None)
            user = None
        elif user.get("login_type") == "wallet" and "address" not in user:
            session.pop("user", None)
            user = None

    if user:
        try:
            memory, _ = get_or_create_agent_memory(user)
            memory_text = memory.get("memory_text") or ""
        except Exception as e:
            memory_error = str(e)
            logger.error(f"Error loading Reader Agent memory: {str(e)}")

    open_posts = get_active_posts_for_reader(user, limit=8)
    return render_template(
        "reader.html",
        user=user,
        default_intent=READER_AGENT_DEFAULT_INTENT,
        memory_text=memory_text,
        memory_error=memory_error,
        open_posts=open_posts,
    )

@app.route("/api/agent-runs", methods=["POST"])
def api_create_agent_run():
    data = request.get_json(silent=True) or {}
    user = session.get("user") or {}
    if user.get("login_type") != "caw_owner":
        return jsonify({"error": "PAIRING_REQUIRED: connect a CAW Owner wallet before asking the Agent to post."}), 401
    binding = get_caw_binding_by_id(user.get("caw_binding_id"))
    if not binding or binding.get("pair_status") != "paired":
        return jsonify({"error": "PAIRING_REQUIRED: the CAW Owner pairing is not active."}), 401

    goal = str(data.get("goal") or "").strip()
    if not goal:
        return jsonify({"error": "goal is required"}), 400

    category = str(data.get("category") or "signals").strip() or "signals"
    visibility_mode = str(data.get("visibility_mode") or "public").strip()
    if visibility_mode not in VISIBILITY_MODES:
        visibility_mode = "public"

    tags = parse_tags(data.get("tags"))
    requester_label = str(data.get("requester_label") or DEFAULT_AGENT_REQUESTER).strip() or DEFAULT_AGENT_REQUESTER
    stake_amount = str(data.get("stake_amount") or os.getenv("CAW_DEMO_STAKE_AMOUNT", "0.005")).strip()
    acceptance_criteria = str(data.get("acceptance_criteria") or "Verified CAW approve and deposit tx create a live post in the BBS feed.").strip()

    payload = {
        "requester_label": requester_label,
        "status": "queued",
        "goal": goal,
        "category": category,
        "stake_amount": stake_amount,
        "visibility_mode": visibility_mode,
        "acceptance_criteria": acceptance_criteria,
        "tags": tags,
        "agent_wallet": binding.get("caw_wallet_address"),
    }

    if supabase:
        try:
            response = supabase.table("agent_runs").insert(payload).execute()
            if response.data:
                return jsonify({"success": True, "mode": "queued", "agent_run": normalize_agent_run(response.data[0])})
        except Exception as e:
            logger.info(f"agent_runs insert unavailable, returning preview run: {str(e)}")

    preview = normalize_agent_run({**payload, "id": f"local-{int(time.time())}", "created_at": "local preview"})
    return jsonify({"success": True, "mode": "local_preview", "agent_run": preview})


@app.route("/api/agent-runs/<agent_run_id>", methods=["GET"])
def api_get_agent_run(agent_run_id):
    run = get_agent_run_by_id(agent_run_id)
    if not run and agent_run_id == str(get_recorded_agent_run().get("id")):
        run = get_recorded_agent_run()
    if not run:
        return jsonify({"error": "AGENT_RUN_NOT_FOUND"}), 404
    if run.get("can_reconcile"):
        schedule_agent_run_reconciliation(agent_run_id)
    return jsonify({"schema": "unifieldbbs.agent_run.v1", "agent_run": run})


@app.route("/api/agent-runs/<agent_run_id>/status", methods=["POST"])
def api_update_agent_run_status(agent_run_id):
    if not supabase:
        return jsonify({"error": "Supabase client not configured"}), 500

    data = request.get_json(silent=True) or {}
    status = str(data.get("status") or "").strip()
    if status not in AGENT_RUN_STATUSES:
        return jsonify({"error": "invalid status"}), 400

    update_payload = {"status": status}
    for key in [
        "agent_wallet", "pact_id", "approve_request_id", "approve_tx",
        "deposit_request_id", "deposit_tx", "post_id", "error_message"
    ]:
        value = data.get(key)
        if value not in (None, ""):
            update_payload[key] = value

    try:
        response = supabase.table("agent_runs").update(update_payload).eq("id", agent_run_id).execute()
        if not response.data:
            return jsonify({"error": "AGENT_RUN_NOT_FOUND"}), 404
        return jsonify({"success": True, "agent_run": normalize_agent_run(response.data[0])})
    except Exception as e:
        logger.error(f"Error updating agent run status: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/agent-runs/<agent_run_id>/execute", methods=["POST"])
def api_execute_agent_run(agent_run_id):
    run = get_agent_run_record_by_id(agent_run_id)
    if not run:
        return jsonify({"error": "AGENT_RUN_NOT_FOUND"}), 404
    user = session.get("user") or {}
    active_wallet = str(user.get("caw_wallet_address") or "").strip().lower()
    run_wallet = str(run.get("agent_wallet") or "").strip().lower()
    if user.get("login_type") != "caw_owner" or not active_wallet:
        return jsonify({"error": "PAIRING_REQUIRED: connect the CAW Owner wallet that created this request."}), 401
    if not run_wallet or active_wallet != run_wallet:
        return jsonify({"error": "WALLET_MISMATCH: this Agent request belongs to a different CAW Owner wallet."}), 403
    normalized = normalize_agent_run(run)
    if normalized.get("status") == "verified" and normalized.get("post_id"):
        return jsonify({"success": True, "mode": "already_verified", "agent_run": normalized})

    update_agent_run_fields(agent_run_id, status="executing")
    thread = threading.Thread(target=run_agent_executor_thread, args=(agent_run_id,), daemon=True)
    thread.start()
    return jsonify({"success": True, "mode": "execution_started", "agent_run": get_agent_run_by_id(agent_run_id)}), 202


@app.route("/api/reader/memory", methods=["GET"])
def api_get_reader_memory():
    user = session.get("user")
    if not user or "id" not in user:
        return jsonify({"error": "LOGIN_REQUIRED"}), 401
    try:
        memory, created = get_or_create_agent_memory(user)
        return jsonify({
            "schema": "unifieldbbs.agent_memory.v1",
            "created": created,
            "memory": memory,
        })
    except Exception as e:
        logger.error(f"Error reading agent memory: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/reader/memory", methods=["POST"])
def api_update_reader_memory():
    user = session.get("user")
    if not user or "id" not in user:
        return jsonify({"error": "LOGIN_REQUIRED"}), 401

    data = request.get_json(silent=True) or {}
    memory_text = data.get("memory_text", "")
    if not isinstance(memory_text, str):
        return jsonify({"error": "memory_text must be a string"}), 400

    try:
        get_or_create_agent_memory(user)
        response = supabase.table("agent_memories") \
            .update({
                "memory_text": memory_text.strip(),
                "login_type": user.get("login_type"),
                "wallet_address": user.get("address"),
            }) \
            .eq("user_id", user["id"]) \
            .execute()
        memory = response.data[0] if response.data else None
        return jsonify({
            "schema": "unifieldbbs.agent_memory.v1",
            "memory": memory,
        })
    except Exception as e:
        logger.error(f"Error updating agent memory: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/reader/memory", methods=["DELETE"])
def api_delete_reader_memory():
    user = session.get("user")
    if not user or "id" not in user:
        return jsonify({"error": "LOGIN_REQUIRED"}), 401

    try:
        response = supabase.table("agent_memories") \
            .delete() \
            .eq("user_id", user["id"]) \
            .execute()
        return jsonify({
            "schema": "unifieldbbs.agent_memory.v1",
            "deleted": bool(response.data),
        })
    except Exception as e:
        logger.error(f"Error deleting agent memory: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/reader/recommend", methods=["POST"])
def api_reader_recommend():
    user = session.get("user")
    if not user or "id" not in user:
        return jsonify({"error": "LOGIN_REQUIRED"}), 401

    data = request.get_json(silent=True) or {}
    intent = data.get("intent") or READER_AGENT_DEFAULT_INTENT
    category = data.get("category")
    limit = min(int(data.get("limit", 5)), 10)
    mode = data.get("mode", "auto")

    try:
        memory, _ = get_or_create_agent_memory(user)
        posts = get_active_posts_for_reader(user, category=category, limit=30)
        result = recommend_posts(
            posts,
            intent,
            memory_text=memory.get("memory_text") or "",
            mode=mode,
            limit=limit,
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error running Reader Agent: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/wallet/verify", methods=["POST"])
def verify_wallet():
    data = request.get_json()
    address = data.get("address", "").lower()
    signature = data.get("signature", "")
    timestamp = data.get("timestamp", 0)
    
    if not address or not signature:
        return jsonify({"error": "Missing required fields"}), 400
    
    if abs(time.time() - timestamp) > MESSAGE_TTL:
        return jsonify({"error": "Request expired"}), 401
    
    try:
        expected_message = f"Commit to the self-cleaning network.\n\nWallet: {address}\nTimestamp: {timestamp}"
        message = encode_defunct(text=expected_message)
        
        try:
            recovered_address = Account.recover_message(message, signature=signature)
            if recovered_address.lower() != address.lower():
                return jsonify({"error": "Invalid signature"}), 401
        except Exception as e:
            logger.error(f"Signature recovery error: {str(e)}")
            return jsonify({"error": "Signature verification failed"}), 400

        user, is_new = get_or_create_user(address)
        
        session["user"] = {
            "id": user["user_id"],
            "address": user["wallet_address"],
            "name": f"{user['wallet_address'][:6]}...{user['wallet_address'][-4:]}",
            "login_type": "wallet",
            "created_at": user.get("created_at")
        }
        
        return jsonify({
            "success": True,
            "is_new_user": is_new,
            "user": {
                "address": user["wallet_address"],
                "created_at": user.get("created_at"),
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/post/<int:post_id>/live", methods=["POST"])
def make_post_live(post_id):
    user = session.get("user")
    if not user or user.get("login_type") != "wallet":
        return jsonify({"error": "Unauthorized"}), 403

    if supabase:
        try:
            # Verify ownership
            response = supabase.table("staking_posts").select("*").eq("id", post_id).eq("delete", False).single().execute()
            post = response.data
            if not post:
                return jsonify({"error": "POST_NOT_FOUND"}), 404
                
            if post.get("user") != user["id"] and post.get("author_id") != user["id"]:
                return jsonify({"error": "UNAUTHORIZED_ACCESS"}), 403
                
            # Perform update
            supabase.table("staking_posts").update({"live": True}).eq("id", post_id).execute()
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"Error making post {post_id} live: {str(e)}")
            return jsonify({"error": str(e)}), 500
            
    return jsonify({"success": True})


@app.route("/stake", methods=["GET"])
def stake():
    user = session.get("user")
    if not user or user.get("login_type") != "wallet":
        return redirect(url_for("category", category="signals"))
    
    return render_template("stake.html", user=user)


@app.route("/post/<int:post_id>/edit", methods=["GET", "POST"])
def edit_post(post_id):
    user = session.get("user")
    if not user:
        return redirect(url_for("category", category="signals"))
        
    post = None
    if supabase:
        try:
            response = supabase.table("staking_posts") \
                .select("*") \
                .eq("id", post_id) \
                .eq("delete", False) \
                .single() \
                .execute()
            post = response.data
        except Exception as e:
            logger.error(f"Error fetching post {post_id} for edit: {str(e)}")
            
    if not post:
        return "DECRYPT_ERROR: DATA_PACKET_NOT_FOUND", 404
        
    # Check if the user is the author
    if user.get("id") != post.get("user"):
        return "UNAUTHORIZED_ACCESS: KEY_MISMATCH", 403

    if request.method == "POST":
        title = request.form.get("title")
        content = request.form.get("content")
        # category = request.form.get("category")
        live = True if request.form.get("live") or request.form.get("live") == "true" else False

        if not title or not content:
            if live:
                supabase.table("staking_posts") \
                    .update({"live": live}) \
                    .eq("id", post_id) \
                    .execute()
                return redirect(url_for("post_detail", post_id=post_id))
            return "Missing title or content", 400

        if supabase:
            try:
                visibility_mode = request.form.get("visibility_mode") or "public"
                if visibility_mode not in VISIBILITY_MODES:
                    visibility_mode = "public"
                update_data = {
                    "title": title,
                    "content": content,
                    "content_excerpt": request.form.get("content_excerpt") or get_content_excerpt({"content": content}),
                    "visibility_mode": visibility_mode,
                    # "category": category
                }
                try:
                    supabase.table("staking_posts") \
                        .update(update_data) \
                        .eq("id", post_id) \
                        .execute()
                except Exception as update_error:
                    logger.warning(f"Retrying post update without visibility fields: {str(update_error)}")
                    update_data.pop("content_excerpt", None)
                    update_data.pop("visibility_mode", None)
                    supabase.table("staking_posts") \
                        .update(update_data) \
                        .eq("id", post_id) \
                        .execute()
                return redirect(url_for("post_detail", post_id=post_id))
            except Exception as e:
                logger.error(f"Error updating post {post_id}: {str(e)}")
                return f"Error: {str(e)}", 500
                
        return redirect(url_for("post_detail", post_id=post_id))

    return render_template("edit_post.html", user=user, post=post)


def create_post_from_deposit_tx(tx_hash, *, category=None, agent_run_id=None, data=None):
    data = data or {}
    if not tx_hash:
        return {"error": "Missing tx_hash"}, 400

    indexed_address, staking_id, amount, error = verify_staking_tx_and_get_id(tx_hash)
    if not staking_id:
        return {"error": error or "Could not verify staking transaction"}, 400

    agent_run = get_agent_run_record_by_id(agent_run_id) if agent_run_id else None
    user = None
    if agent_run:
        category = category or agent_run.get("category")
    else:
        user, is_new = get_or_create_user(indexed_address)

    staking_label = f"{NETWORK_SLUG}:{staking_id}"
    requested_visibility = data.get("visibility_mode") or (agent_run or {}).get("visibility_mode") or "public"
    if requested_visibility not in VISIBILITY_MODES:
        requested_visibility = "public"

    existing = supabase.table("staking_posts") \
        .select("id") \
        .eq("staking", staking_label) \
        .execute()

    if existing.data:
        post_id = existing.data[0]["id"]
        if agent_run_id and supabase:
            try:
                update_agent_run_after_verified_tx(
                    agent_run_id,
                    indexed_address=indexed_address,
                    tx_hash=tx_hash,
                    post_id=post_id,
                    data=data,
                )
            except Exception as e:
                logger.info(f"agent_run update skipped for existing post: {str(e)}")
        return {"success": True, "post_id": post_id}, 200

    post_data = {
        "user": user.get("user_id") if user else None,
        "author_name": agent_run.get("requester_label") if agent_run else None,
        "category": category or "signals",
        "staking": staking_label,
        "value": amount,
        "visibility_mode": requested_visibility,
    }

    if agent_run:
        post_data.update({
            "title": f"Agent-posted {agent_run.get('category', 'signals')} signal",
            "content": agent_run.get("goal"),
            "content_excerpt": agent_run.get("goal"),
            "live": True,
            "status": "active",
            "actor_type": "human_via_agent",
            "posted_by": "agent",
            "agent_wallet": indexed_address,
            "acceptance_criteria": agent_run.get("acceptance_criteria"),
            "tags": agent_run.get("tags") or [],
            "budget": agent_run.get("stake_amount"),
        })

    try:
        response = supabase.table("staking_posts").insert(post_data).execute()
    except Exception as insert_error:
        logger.warning(f"Retrying post insert with legacy fields only: {str(insert_error)}")
        for optional_key in [
            "visibility_mode", "author_name", "content_excerpt", "status", "actor_type",
            "posted_by", "agent_wallet", "acceptance_criteria", "tags", "budget"
        ]:
            post_data.pop(optional_key, None)
        response = supabase.table("staking_posts").insert(post_data).execute()

    if response.data:
        new_post_id = response.data[0]["id"]
        if agent_run_id and supabase:
            try:
                update_agent_run_after_verified_tx(
                    agent_run_id,
                    indexed_address=indexed_address,
                    tx_hash=tx_hash,
                    post_id=new_post_id,
                    data=data,
                )
            except Exception as e:
                logger.info(f"agent_run update skipped after post sync: {str(e)}")
        return {"success": True, "post_id": new_post_id}, 200

    return {"error": "Failed to create post"}, 500


@app.route("/api/create_post", methods=["POST"])
def create_post():
    data = request.get_json(silent=True) or {}
    tx_hash = request.args.get("tx_hash") or request.form.get("tx_hash") or data.get("tx_hash")
    category = request.args.get("category") or request.form.get("category") or data.get("category")
    agent_run_id = request.args.get("agent_run_id") or request.form.get("agent_run_id") or data.get("agent_run_id")
    requested_visibility = request.form.get("visibility_mode") or data.get("visibility_mode")
    payload, status_code = create_post_from_deposit_tx(
        tx_hash,
        category=category,
        agent_run_id=agent_run_id,
        data={**data, "visibility_mode": requested_visibility or data.get("visibility_mode")},
    )
    return jsonify(payload), status_code

@app.route("/api/deactivate_post", methods=["POST"])
def deactivate_post():
    tx_hash = request.args.get("tx_hash") or \
              request.form.get("tx_hash") or \
              (request.get_json(silent=True) or {}).get("tx_hash")
              
    if not tx_hash:
        return jsonify({"error": "Missing tx_hash"}), 400

    indexed_user, staking_id, amount, error = verify_withdraw_tx_and_get_id(tx_hash)
    # print(f"User: {user}")
    print(f"Indexed User: {indexed_user}")
    # assert user == indexed_user
    if not staking_id:
        return jsonify({"error": error or "Could not verify staking transaction"}), 400
    
    # print(f"Staking ID: {staking_id}")
    staking_label = f"{NETWORK_SLUG}:{staking_id}"
    # try:
    response = supabase.table("staking_posts").update({"delete": True}).eq("staking", staking_label).execute()
    if response.data:
        return jsonify({"success": True})
    # except Exception as e:
    #     logger.error(f"Error creating post: {str(e)}")



#def index():
#    return redirect(url_for("category", category="signals"))
@app.route("/")
@app.route("/<category>")
@app.route("/<category>/<int:page>")
def category(category='all', page = 1):
    user = session.get("user")
    
    if user:
        if "login_type" not in user or "id" not in user:
            session.pop("user", None)
            user = None
        elif user.get("login_type") == "wallet" and "address" not in user:
            session.pop("user", None)
            user = None

    # Pagination logic
    page_size = 10
    offset = (page - 1) * page_size
    
    posts = []
    if supabase:
        try:
            query = supabase.table("staking_posts") \
                .select("*") \
                .eq("live", True) \
                .eq("delete", False)
            if category != "all":
                query = query.eq("category", category)
            response = query \
                .order("id", desc=True) \
                .limit(page_size) \
                .offset(offset) \
                .execute()
            posts.extend(response.data)
        except Exception as e:
            logger.error(f"Error fetching posts: {str(e)}")
            
    return render_template("index.html", user=user, posts=posts, page=page, category=category, caw_session=get_caw_session_state())


@app.route("/<category>.md")
@app.route("/<category>.md/<int:page>")
def category_md(category, page = 1):
    user = session.get("user")
    
    if user:
        if "login_type" not in user or "id" not in user:
            session.pop("user", None)
            user = None
        elif user.get("login_type") == "wallet" and "address" not in user:
            session.pop("user", None)
            user = None

        
    # Pagination logic
    page_size = 10
    offset = (page - 1) * page_size
    
    posts = []
    if supabase:
        try:
            query = supabase.table("staking_posts") \
                .select("*") \
                .eq("live", True) \
                .eq("delete", False)
            if category:
                query = query.eq("category", category)
            response = query \
                .order("id", desc=True) \
                .limit(page_size) \
                .offset(offset) \
                .execute()
            posts = response.data
        except Exception as e:
            logger.error(f"Error fetching posts: {str(e)}")

    md_content = f"### {category.upper()}_TRANSMISSIONS // PAGE_{page}\n\n"
    
    if not posts:
        md_content += "> [!] EMPTY_STREAM: NO_DATA_PACKETS_DETECTED.\n"
    else:
        for post in posts:
            pid = post['id']
            pcat = (post.get('category') or 'GENERAL').upper()
            ptitle = post.get('title') or '[UNTITLED_TRANSMISSION]'
            pdate = post.get('created_at', 'N/A')
            purl = url_for('post_detail_md', post_id=pid, _external=True)
            agent_post = normalize_post_for_agent(post, user)
            
            md_content += f"#### ENTRY_{pid} // {pcat}\n"
            md_content += f"**TITLE**: {ptitle}\n"
            md_content += f"- TIMESTAMP: {pdate}\n"
            md_content += f"- STATUS: {agent_post['status']}\n"
            md_content += f"- ACTOR_TYPE: {agent_post['actor_type']}\n"
            md_content += f"- VISIBILITY: {agent_post['visibility_mode']}\n"
            md_content += f"- ACCESS: {agent_post['access_requirement']}\n"
            md_content += f"- STAKE: {agent_post['stake_amount'] or 'N/A'} {agent_post['stake_token']}\n"
            md_content += f"- TAGS: {', '.join(agent_post['tags']) if agent_post['tags'] else 'N/A'}\n"
            md_content += f"- EXCERPT: {agent_post['content_excerpt'] or 'N/A'}\n"
            md_content += f"- JSON: {agent_post['json_url']}\n"
            md_content += f"- DECRYPT: [READ_ENTRY]({purl})\n\n"
            md_content += "---\n\n"
            
    # Navigation
    nav = ""
    if page > 1:
        prev_url = url_for('category_md', category=category, page=page-1, _external=True)
        nav += f"[<< PREV_PAGE]({prev_url}) "
    if len(posts) == page_size:
        next_url = url_for('category_md', category=category, page=page+1, _external=True)
        nav += f"[NEXT_PAGE >>]({next_url})"
    
    if nav:
        md_content += f"\n{nav}\n"
        
    md_content += f"\n\n---\n&copy; 2026 STAKING_BBS // SELF_CLEANING_PROTOCOL"

    return Response(md_content, mimetype='text/markdown')


@app.route("/feed.json")
def feed_json():
    user = session.get("user")
    category = request.args.get("category")
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("page_size", 10))
    offset = (page - 1) * page_size

    posts = []
    if supabase:
        try:
            query = supabase.table("staking_posts") \
                .select("*") \
                .eq("live", True) \
                .eq("delete", False)
            if category:
                query = query.eq("category", category)
            response = query \
                .order("id", desc=True) \
                .limit(page_size) \
                .offset(offset) \
                .execute()
            posts = response.data
        except Exception as e:
            logger.error(f"Error fetching JSON feed: {str(e)}")

    return jsonify({
        "schema": "unifieldbbs.feed.v1",
        "page": page,
        "page_size": page_size,
        "category": category,
        "chain": get_chain_config(),
        "posts": [normalize_post_for_agent(post, user) for post in posts],
    })


@app.route("/post/<int:post_id>.json")
def post_detail_json(post_id):
    user = session.get("user")
    post = None
    if supabase:
        try:
            response = supabase.table("staking_posts") \
                .select("*") \
                .eq("delete", False) \
                .eq("id", post_id) \
                .single() \
                .execute()
            post = response.data
        except Exception as e:
            logger.error(f"Error fetching JSON post {post_id}: {str(e)}")

    if not post:
        return jsonify({"error": "POST_NOT_FOUND"}), 404

    return jsonify({
        "schema": "unifieldbbs.post.v1",
        "chain": get_chain_config(),
        "post": normalize_post_for_agent(post, user),
    })

@app.route("/post/<int:post_id>")
def post_detail(post_id):
    user = session.get("user")
    post = None
    if supabase:
        try:
            response = supabase.table("staking_posts") \
                .select("*") \
                .eq("delete", False) \
                .eq("id", post_id) \
                .single() \
                .execute()
            post = response.data
            
            # Render markdown
            if post and post.get("content"):
                post["html_content"] = markdown.markdown(post["content"], extensions=['fenced_code', 'tables'])
        except Exception as e:
            logger.error(f"Error fetching post {post_id}: {str(e)}")
            
    if not post:
        return "DECRYPT_ERROR: DATA_PACKET_NOT_FOUND", 404

    agent_post = normalize_post_for_agent(post, user)
    post["visibility_mode"] = agent_post["visibility_mode"]
    post["access_requirement"] = agent_post["access_requirement"]
    post["content_excerpt"] = agent_post["content_excerpt"]
    post["content_locked"] = agent_post["content_locked"]
    post["status"] = agent_post["status"]
    post["actor_type"] = agent_post["actor_type"]
    post["stake_amount"] = agent_post["stake_amount"]
    post["stake_token"] = agent_post["stake_token"]
    post["staking_id"] = agent_post["staking_id"]
    post["network"] = agent_post["network"]
    post["tags"] = agent_post["tags"]
    if post["content_locked"]:
        if post["visibility_mode"] == "paid_full_text":
            post["html_content"] = markdown.markdown(
                "PAYMENT_REQUIRED: Full text is reserved for a future CAW pay-to-read flow."
            )
        else:
            post["html_content"] = markdown.markdown(
                "LOGIN_REQUIRED: Sign in to read the full content. The public feed only exposes the discovery layer."
            )
    elif post.get("content"):
        post["html_content"] = markdown.markdown(post["content"], extensions=['fenced_code', 'tables'])
        
    return render_template("post_detail.html", user=user, post=post)



@app.route("/post/<int:post_id>.md")
def post_detail_md(post_id):
    user = session.get("user")
    post = None
    if supabase:
        try:
            response = supabase.table("staking_posts") \
                .select("*") \
                .eq("delete", False) \
                .eq("id", post_id) \
                .single() \
                .execute()
            post = response.data
            
            # Render markdown
            if post and post.get("content"):
                post["html_content"] = markdown.markdown(post["content"], extensions=['fenced_code', 'tables'])
        except Exception as e:
            logger.error(f"Error fetching post {post_id}: {str(e)}")
            
    if not post:
        return "DECRYPT_ERROR: DATA_PACKET_NOT_FOUND", 404
        
    agent_post = normalize_post_for_agent(post, user)
    md_content = f"# {post.get('title') or '[UNTITLED_TRANSMISSION]'}\n\n"
    md_content += f"- **ID**: {post['id']}\n"
    md_content += f"- **CATEGORY**: {(post.get('category') or 'GENERAL').upper()}\n"
    md_content += f"- **TYPE**: {agent_post['type']}\n"
    md_content += f"- **STATUS**: {agent_post['status']}\n"
    md_content += f"- **ACTOR_TYPE**: {agent_post['actor_type']}\n"
    md_content += f"- **VISIBILITY**: {agent_post['visibility_mode']}\n"
    md_content += f"- **ACCESS_REQUIREMENT**: {agent_post['access_requirement']}\n"
    md_content += f"- **AGENT_WALLET**: {agent_post['agent_wallet'] or 'N/A'}\n"
    md_content += f"- **STAKE**: {agent_post['stake_amount'] or 'N/A'} {agent_post['stake_token']}\n"
    md_content += f"- **STAKING_ID**: {agent_post['staking_id'] or 'N/A'}\n"
    md_content += f"- **TAGS**: {', '.join(agent_post['tags']) if agent_post['tags'] else 'N/A'}\n"
    md_content += f"- **ACCEPTANCE_CRITERIA**: {agent_post['acceptance_criteria'] or 'N/A'}\n"
    md_content += f"- **TIMESTAMP**: {post.get('created_at', 'N/A')}\n"
    # md_content += f"- **STAKING**: {post.get('staking', 'N/A')}\n\n"
    md_content += "---\n\n"
    if agent_post["content_locked"]:
        md_content += f"[LOCKED_CONTENT] ACCESS_REQUIREMENT={agent_post['access_requirement']}\n\n"
        md_content += agent_post["content_excerpt"] or "[NO_PUBLIC_EXCERPT]"
    else:
        md_content += post.get('content') or "[EMPTY_DATA_PACKET]"
    
    md_content += f"\n\n---\n&copy; 2026 STAKING_BBS // SELF_CLEANING_PROTOCOL"
    
    return Response(md_content, mimetype='text/markdown')


@app.route("/login/twitter")
def login_twitter():
    if not supabase:
        return "Supabase client not configured. Check your .env file.", 500
    
    # The callback URI that Supabase redirects back to after X login
    # For X OAuth 2.0, ensure this is in your X Redirection whitelist
    redirect_url = url_for("auth_callback", _external=True)
    logger.info(f"Initiating login, redirect_url: {redirect_url}")
    
    try:
        # Use PKCE flow to sign in with OAuth provider
        # Note: 'twitter' is the provider name for X even for OAuth 2.0
        # Adding scopes can help with OAuth 2.0 requirements
        auth_response = supabase.auth.sign_in_with_oauth({
            "provider": "x",
            "options": {
                "redirect_to": redirect_url,
                "scopes": "tweet.read users.read offline.access" # Valid for X OAuth 2.0
            }
        })
        
        if not auth_response or not auth_response.url:
            logger.error(f"Failed to get OAuth URL from Supabase. Response: {auth_response}")
            return "Failed to initiate login. Make sure Twitter/X is enabled in Supabase Dashboard.", 500
            
        return redirect(auth_response.url)
    except Exception as e:
        logger.error(f"Error in login_twitter: {str(e)}")
        return f"Authentication initiation failed: {str(e)}", 500

@app.route("/auth/callback")
def auth_callback():
    code = request.args.get("code")
    error = request.args.get("error")
    error_desc = request.args.get("error_description")
    
    if error:
        logger.error(f"Auth callback error: {error} - {error_desc}")
        return f"Authentication failed: {error_desc}", 401
        
    if not code:
        logger.warning("Auth code missing from callback URL")
        return "Auth code missing from redirect", 400
    
    try:
        # Exchange the code for a session
        session_data = supabase.auth.exchange_code_for_session({
            "auth_code": code
        })
        
        # Store user info in Flask session
        user = session_data.user
        session["user"] = {
            "id": user.id,
            "email": user.email,
            "name": user.user_metadata.get("full_name") or user.user_metadata.get("name") or "User",
            "avatar": user.user_metadata.get("avatar_url"),
            "username": user.user_metadata.get("user_name"), # X handle
            "login_type": "twitter"
        }
        
        logger.info(f"User {session['user']['name']} successfully logged in.")
        return redirect(url_for("category", category="signals"))
    except Exception as e:
        logger.error(f"Failed to exchange code for session: {str(e)}")
        return f"Authentication failed during session exchange: {str(e)}", 401

@app.route("/my_posts")
def my_posts():
    user = session.get("user")
    if not user or "id" not in user or "login_type" not in user:
        session.pop("user", None)
        return redirect(url_for("category", category="signals"))
    
    user_posts = []
    if supabase:
        try:
            response = supabase.table("staking_posts") \
                .select("*") \
                .eq("user", user["id"]) \
                .eq("delete", False) \
                .order("id", desc=True) \
                .execute()
            user_posts = response.data
        except Exception as e:
            logger.error(f"Error fetching user posts: {str(e)}")

    first_staking_id = ""
    for post in user_posts:
        staking_value = post.get("staking")
        staking_id = ""

        if isinstance(staking_value, str) and ":" in staking_value:
            _, staking_suffix = staking_value.split(":", 1)
            staking_id = staking_suffix.strip()

        post["staking_label"] = staking_value or "NO_STAKING_LINK"
        post["staking_id"] = staking_id

        if not first_staking_id and staking_id:
            first_staking_id = staking_id

    return render_template("my_posts.html", user=user, posts=user_posts, first_staking_id=first_staking_id)


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("category", category="signals"))

if __name__ == "__main__":
    app.run(debug=True, port=3000)











