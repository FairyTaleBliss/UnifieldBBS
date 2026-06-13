from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any

import requests


READER_SCHEMA = "unifieldbbs.reader_recommendations.v1"
DEFAULT_INTENT = "Find active posts about data, APIs, or resource procurement under 10 USDC."
SAMPLE_POSTS = [
    {
        "id": 238,
        "title": "Need verified Base DeFi event dataset",
        "content_excerpt": "Looking for indexed swap, lending, and liquidity events for a research agent.",
        "content": "Looking for indexed swap, lending, and liquidity events for a research agent.",
        "category": "resources",
        "status": "active",
        "actor_type": "human_via_agent",
        "tags": ["data", "base", "api"],
        "stake_amount": "6",
        "stake_token": "USDC",
        "visibility_mode": "public",
        "access_requirement": "none",
        "json_url": "/post/238.json",
        "post_url": "/post/238",
    },
    {
        "id": 237,
        "title": "Compare GPU rental APIs for short model-eval jobs",
        "content_excerpt": "Find low-commitment compute options and price ranges.",
        "content": "",
        "category": "resources",
        "status": "active",
        "actor_type": "human",
        "tags": ["gpu", "api", "procurement"],
        "stake_amount": "8",
        "stake_token": "USDC",
        "visibility_mode": "login_required",
        "access_requirement": "login",
        "json_url": "/post/237.json",
        "post_url": "/post/237",
    },
    {
        "id": 236,
        "title": "Paid dataset vendor contact list",
        "content_excerpt": "A curated list of paid data vendors for on-chain analytics.",
        "content": "",
        "category": "resources",
        "status": "active",
        "actor_type": "human",
        "tags": ["data", "vendor", "procurement"],
        "stake_amount": "4",
        "stake_token": "USDC",
        "visibility_mode": "paid_full_text",
        "access_requirement": "paid_full_text",
        "json_url": "/post/236.json",
        "post_url": "/post/236",
    },
]

STOPWORDS = {
    "about",
    "active",
    "agent",
    "and",
    "are",
    "find",
    "for",
    "from",
    "into",
    "less",
    "post",
    "posts",
    "than",
    "the",
    "under",
    "with",
}


def tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {word for word in words if len(word) > 2 and word not in STOPWORDS}


def parse_amount(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        match = re.search(r"\d+(?:\.\d+)?", str(value))
        return float(match.group(0)) if match else None


def extract_budget_limit(intent: str) -> float | None:
    match = re.search(r"(?:under|below|less than|<=?)\s*(\d+(?:\.\d+)?)", intent.lower())
    return float(match.group(1)) if match else None


def normalize_confidence(value: Any, default: float = 0.5) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(confidence, 1.0))


def post_text(post: dict[str, Any]) -> str:
    tags = post.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    return " ".join(
        [
            str(post.get("title") or ""),
            str(post.get("content") or ""),
            str(post.get("content_excerpt") or ""),
            str(post.get("category") or ""),
            str(post.get("actor_type") or ""),
            str(post.get("acceptance_criteria") or ""),
            str(post.get("access_requirement") or ""),
            " ".join(str(tag) for tag in tags),
        ]
    )


def rules_recommend_posts(
    posts: list[dict[str, Any]],
    intent: str,
    *,
    memory_text: str = "",
    limit: int = 5,
) -> dict[str, Any]:
    intent_terms = tokenize(f"{intent}\n{memory_text}")
    budget_limit = extract_budget_limit(intent)
    recommendations = []

    for post in posts:
        score = 0
        reasons = []
        matched_terms = sorted(intent_terms & tokenize(post_text(post)))
        status = post.get("status") or "unknown"
        actor_type = post.get("actor_type") or "unknown"
        access_requirement = post.get("access_requirement") or "none"
        stake_amount = parse_amount(post.get("stake_amount"))

        if status == "active":
            score += 4
            reasons.append("active")
        elif status in {"expired", "inactive"}:
            score -= 5
            reasons.append("not active")

        if actor_type in {"agent", "human_via_agent"}:
            score += 2
            reasons.append(f"{actor_type} post")

        if matched_terms:
            score += min(len(matched_terms), 6) * 2
            reasons.append("matches " + ", ".join(matched_terms[:5]))

        if budget_limit is not None and stake_amount is not None:
            if stake_amount <= budget_limit:
                score += 2
                reasons.append(f"within {budget_limit:g} USDC budget")
            else:
                score -= 4
                reasons.append(f"over {budget_limit:g} USDC budget")

        if access_requirement == "paid_full_text":
            reasons.append("full text requires payment")
        elif access_requirement == "login":
            reasons.append("full text requires login")
        elif post.get("content"):
            score += 1
            reasons.append("full text available")

        if score <= 0:
            continue

        recommendations.append(
            {
                "post_id": post.get("id"),
                "title": post.get("title") or "[UNTITLED]",
                "reason": "; ".join(reasons),
                "confidence": normalize_confidence(score / 15, default=0.5),
                "suggested_action": "open_post",
                "access_requirement": access_requirement,
                "risk_or_missing_info": "No LLM configured; this is a deterministic fallback score.",
                "json_url": post.get("json_url"),
                "post_url": post.get("post_url"),
                "score": score,
            }
        )

    recommendations.sort(key=lambda item: item["score"], reverse=True)
    for item in recommendations:
        item.pop("score", None)

    return {
        "schema": READER_SCHEMA,
        "mode": "rules_fallback",
        "intent": intent,
        "memory_used": bool(memory_text.strip()),
        "source_count": len(posts),
        "recommendations": recommendations[:limit],
    }


def compact_posts_for_llm(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed_keys = [
        "id",
        "title",
        "category",
        "status",
        "actor_type",
        "tags",
        "stake_amount",
        "stake_token",
        "visibility_mode",
        "access_requirement",
        "content_excerpt",
        "content",
        "json_url",
        "post_url",
    ]
    compact = []
    for post in posts:
        compact.append({key: post.get(key) for key in allowed_keys if key in post})
    return compact


def parse_llm_json(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def validate_llm_result(result: dict[str, Any], intent: str, posts: list[dict[str, Any]], memory_text: str) -> dict[str, Any]:
    known_posts = {str(post.get("id")): post for post in posts}
    raw_items = result.get("recommendations")
    if not isinstance(raw_items, list):
        raise ValueError("LLM response missing recommendations list")

    recommendations = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        post_id = item.get("post_id")
        source_post = known_posts.get(str(post_id))
        if not source_post:
            continue
        recommendations.append(
            {
                "post_id": source_post.get("id"),
                "title": item.get("title") or source_post.get("title") or "[UNTITLED]",
                "reason": item.get("reason") or "Recommended by Reader Agent.",
                "confidence": normalize_confidence(item.get("confidence")),
                "suggested_action": item.get("suggested_action") or "open_post",
                "access_requirement": source_post.get("access_requirement") or "none",
                "risk_or_missing_info": item.get("risk_or_missing_info") or "",
                "json_url": source_post.get("json_url"),
                "post_url": source_post.get("post_url"),
            }
        )

    return {
        "schema": READER_SCHEMA,
        "mode": "llm",
        "intent": intent,
        "memory_used": bool(memory_text.strip()),
        "source_count": len(posts),
        "recommendations": recommendations,
    }


def call_openai_compatible_llm(
    posts: list[dict[str, Any]],
    intent: str,
    memory_text: str,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    if not api_key:
        raise RuntimeError("LLM_API_KEY is not configured")

    system = (
        "You are a user-owned Reader Agent for UnifieldBBS. "
        "Rank open feed posts against the user's intent and memory. "
        "Only recommend posts from the provided list. "
        "Do not reveal or infer hidden full text. "
        "Return strict JSON with a recommendations array."
    )
    user = {
        "intent": intent,
        "user_memory": memory_text,
        "limit": limit,
        "posts": compact_posts_for_llm(posts),
        "output_schema": {
            "recommendations": [
                {
                    "post_id": "number or string from provided posts",
                    "title": "string",
                    "reason": "short explanation",
                    "confidence": "number between 0 and 1",
                    "suggested_action": "open_post | open_json_and_summarize | login_to_read | payment_required",
                    "risk_or_missing_info": "string",
                }
            ]
        },
    }
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=True)},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        timeout=30,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return validate_llm_result(parse_llm_json(content), intent, posts, memory_text)


def recommend_posts(
    posts: list[dict[str, Any]],
    intent: str,
    *,
    memory_text: str = "",
    mode: str = "auto",
    limit: int = 5,
) -> dict[str, Any]:
    if mode not in {"auto", "llm", "rules"}:
        raise ValueError("mode must be auto, llm, or rules")
    if mode == "rules":
        return rules_recommend_posts(posts, intent, memory_text=memory_text, limit=limit)

    try:
        result = call_openai_compatible_llm(posts, intent, memory_text, limit=limit)
        result["recommendations"] = result["recommendations"][:limit]
        return result
    except Exception as exc:
        if mode == "llm":
            raise
        result = rules_recommend_posts(posts, intent, memory_text=memory_text, limit=limit)
        result["fallback_reason"] = str(exc)
        return result


def fetch_feed(feed_url: str) -> list[dict[str, Any]]:
    response = requests.get(feed_url, timeout=10)
    response.raise_for_status()
    payload = response.json()
    return payload.get("posts", []) if isinstance(payload, dict) else []


def main() -> None:
    parser = argparse.ArgumentParser(description="Run UnifieldBBS Reader Agent against an open feed.")
    parser.add_argument("--feed-url", default="http://127.0.0.1:3000/feed.json")
    parser.add_argument("--intent", default=os.getenv("READER_AGENT_DEFAULT_INTENT", DEFAULT_INTENT))
    parser.add_argument("--memory-file")
    parser.add_argument("--memory-text", default="")
    parser.add_argument("--mode", choices=["auto", "llm", "rules"], default="auto")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--sample", action="store_true", help="Use built-in sample posts instead of fetching feed-url.")
    args = parser.parse_args()

    memory_text = args.memory_text
    if args.memory_file:
        with open(args.memory_file, "r", encoding="utf-8") as file:
            memory_text = file.read()

    posts = SAMPLE_POSTS if args.sample else fetch_feed(args.feed_url)
    result = recommend_posts(posts, args.intent, memory_text=memory_text, mode=args.mode, limit=args.limit)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"ERROR: {exc}")
