import os
import json
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

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from agent.calldata import encode_approve_calldata, encode_deposit_calldata
    from agent.caw_sdk_client import (
        contract_call,
        extract_pact_api_key,
        extract_pact_id,
        extract_tx_hash,
        get_pact,
        get_transaction_by_request_id,
        submit_pact,
        wait_for_pact_status,
    )
    from agent.config import load_caw_posting_config, token_units
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


AGENT_RUN_STATUSES = {"queued", "executing", "verified", "failed"}
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


def get_agent_run_steps(run):
    return [
        {"name": "Intent received", "status": "done" if run else "pending"},
        {"name": "Pact submitted", "status": "done" if has_agent_run_value(run, "pact_id") else "pending"},
        {"name": "Pact active", "status": "done" if has_agent_run_value(run, "pact_id") else "pending"},
        {"name": "Approve submitted", "status": "done" if has_agent_run_value(run, "approve_request_id") else "pending"},
        {"name": "Approve confirmed", "status": "done" if has_agent_run_value(run, "approve_tx") else "pending"},
        {"name": "Deposit submitted", "status": "done" if has_agent_run_value(run, "deposit_request_id") else "pending"},
        {"name": "Deposit confirmed", "status": "done" if has_agent_run_value(run, "deposit_tx") else "pending"},
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
    config = load_caw_posting_config()
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
        time.sleep(poll_seconds)
    tx_hash = extract_tx_hash(last_record) if last_record else None
    if tx_hash:
        return tx_hash, last_record
    raise TimeoutError(f"Timed out waiting for CAW transaction hash: {request_id}")


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
    pact = wait_for_pact_status(config, pact_id, timeout_seconds=180, poll_seconds=5)
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

    response = contract_call(config, {**call, "src_addr": config.src_address}, api_key_override=pact_api_key)
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


@app.route("/agent-post")
@app.route("/agent-demo")
def agent_demo():
    user = session.get("user")
    recent_posts = get_recent_agent_demo_posts()
    agent_runs = get_agent_runs()
    active_run = next((run for run in agent_runs if run.get("status") == "verified"), None) or (agent_runs[0] if agent_runs else get_recorded_agent_run())
    return render_template(
        "agent_demo.html",
        user=user,
        demo=get_caw_demo_state(),
        agent_runs=agent_runs,
        active_run=active_run,
        recent_posts=recent_posts,
        reader=get_reader_demo_state(user),
    )


@app.route("/api/agent-runs", methods=["POST"])
def api_create_agent_run():
    data = request.get_json(silent=True) or {}
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
        "agent_wallet": os.getenv("CAW_AGENT_WALLET_ADDRESS") or os.getenv("AGENT_WALLET_WALLET_ID"),
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
def category(category='signals', page = 1):
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
            posts.extend(response.data)
        except Exception as e:
            logger.error(f"Error fetching posts: {str(e)}")
            
    return render_template("index.html", user=user, posts=posts, page=page, category=category)


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


















