from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any


DEFAULT_CAW_CLI = "~/.cobo-agentic-wallet/bin/caw"
DEFAULT_PROFILE_ROOT = "~/.cobo-agentic-wallet/profiles"


def _last_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    objects = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, consumed = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append((value, consumed))
    if not objects:
        raise RuntimeError("CAW onboard did not return a JSON object.")
    for value, _ in objects:
        if value.get("agent_id") and (
            value.get("wallet_uuid") or value.get("wallet_id")
        ):
            return value
    return max(objects, key=lambda item: item[1])[0]


def _run(command: list[str], *, timeout_seconds: int) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "CAW CLI runtime was not found. Install caw on the Flask host or configure CAW_CLI_PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"CAW MPC onboarding did not finish within {timeout_seconds} seconds."
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"CAW MPC onboarding failed: {detail[:500]}")
    return result.stdout


def _wsl_command(script: str, *, timeout_seconds: int) -> str:
    return _run(
        ["wsl", "bash", "-lc", script],
        timeout_seconds=timeout_seconds,
    )


def _onboard_with_wsl(
    *,
    agent_name: str,
    api_url: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    configured_cli_path = os.getenv("CAW_CLI_WSL_PATH", "").strip()
    cli_command = (
        shlex.quote(configured_cli_path)
        if configured_cli_path
        else '"$HOME/.cobo-agentic-wallet/bin/caw"'
    )
    command = " ".join(
        [
            cli_command,
            "onboard",
            "--agent-name",
            shlex.quote(agent_name),
            "--api-url",
            shlex.quote(api_url),
            "--wait",
        ]
    )
    output = _wsl_command(command, timeout_seconds=timeout_seconds)
    onboard = _last_json_object(output)
    agent_id = str(onboard.get("agent_id") or "").strip()
    if not agent_id:
        raise RuntimeError("CAW onboard response did not include agent_id.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
        raise RuntimeError("CAW onboard returned an invalid agent_id.")

    profile_name = f"profile_{agent_id}"
    credentials_path = f"$HOME/.cobo-agentic-wallet/profiles/{profile_name}/credentials"
    credentials_text = _wsl_command(
        f'cat "{credentials_path}"',
        timeout_seconds=30,
    )
    credentials = json.loads(credentials_text)
    return _normalize_onboarding_result(
        onboard,
        credentials,
        profile_name=profile_name,
        profile_path=credentials_path.rsplit("/", 1)[0],
        runtime="wsl",
    )


def _onboard_native(
    *,
    agent_name: str,
    api_url: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    cli_path = os.path.expanduser(os.getenv("CAW_CLI_PATH", DEFAULT_CAW_CLI))
    output = _run(
        [
            cli_path,
            "onboard",
            "--agent-name",
            agent_name,
            "--api-url",
            api_url,
            "--wait",
        ],
        timeout_seconds=timeout_seconds,
    )
    onboard = _last_json_object(output)
    agent_id = str(onboard.get("agent_id") or "").strip()
    if not agent_id:
        raise RuntimeError("CAW onboard response did not include agent_id.")

    profile_name = f"profile_{agent_id}"
    profile_path = Path(os.path.expanduser(DEFAULT_PROFILE_ROOT)) / profile_name
    credentials = json.loads((profile_path / "credentials").read_text(encoding="utf-8"))
    return _normalize_onboarding_result(
        onboard,
        credentials,
        profile_name=profile_name,
        profile_path=str(profile_path),
        runtime="native",
    )


def _normalize_onboarding_result(
    onboard: dict[str, Any],
    credentials: dict[str, Any],
    *,
    profile_name: str,
    profile_path: str,
    runtime: str,
) -> dict[str, Any]:
    api_key = str(credentials.get("api_key") or onboard.get("api_key") or "").strip()
    wallet_id = str(
        credentials.get("wallet_uuid")
        or onboard.get("wallet_uuid")
        or onboard.get("wallet_id")
        or ""
    ).strip()
    agent_id = str(credentials.get("agent_id") or onboard.get("agent_id") or "").strip()
    if not api_key or not wallet_id or not agent_id:
        raise RuntimeError("CAW onboard profile is missing API key, wallet UUID, or agent ID.")
    if str(credentials.get("status") or onboard.get("wallet_status") or "").lower() != "active":
        raise RuntimeError("CAW onboard returned before the MPC wallet became active.")

    return {
        "api_key": api_key,
        "agent_id": agent_id,
        "wallet_id": wallet_id,
        "wallet_name": credentials.get("wallet_name") or onboard.get("wallet_name"),
        "agent_name": credentials.get("agent_name") or onboard.get("agent_name"),
        "api_url": credentials.get("api_url") or api_url_from_onboard(onboard),
        "profile_name": profile_name,
        "profile_path": profile_path,
        "runtime": runtime,
        "wallet_type": "MPC",
    }


def api_url_from_onboard(onboard: dict[str, Any]) -> str:
    return str(onboard.get("api_url") or "").strip()


def onboard_mpc_wallet(
    *,
    agent_name: str,
    api_url: str,
    timeout_seconds: int = 240,
) -> dict[str, Any]:
    if not api_url:
        raise RuntimeError("Missing AGENT_WALLET_API_URL for CAW onboarding.")
    if os.name == "nt":
        return _onboard_with_wsl(
            agent_name=agent_name,
            api_url=api_url,
            timeout_seconds=timeout_seconds,
        )
    return _onboard_native(
        agent_name=agent_name,
        api_url=api_url,
        timeout_seconds=timeout_seconds,
    )
