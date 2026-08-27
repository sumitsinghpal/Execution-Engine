"""
LLM narration for autonomous trades — orchestration and rationale-writing
only, never a decision-maker. This was an explicit choice: "no FOMO, no
emotional exit or buy, pure strategy" means every buy/sell/size/exit call
has to be deterministic and reproducible, which an LLM output isn't. So the
boundary here is absolute — this module is handed a trade that has ALREADY
happened (a strategy rule fired, risk_reward.py already computed the exact
stop/target, the order has already been submitted) and its only job is to
write a plain-English explanation of what happened and why, for the audit
log and any dashboard/notification that reads it. It cannot change the
symbol, side, quantity, price, or anything else about the trade — there's
no code path from this module back into the execution flow at all.

Runs with zero configuration: if no API key is set (settings.llm_api_key),
narrate_entry/narrate_exit return a deterministic template string instead
of calling out to anything — same "opt-in, degrades gracefully" shape as
Schwab credentials elsewhere in this codebase. A configured call that
errors (bad key, timeout, rate limit) falls back to the same template
rather than raising — a narration failure must never be allowed to affect
whether a real (paper) trade already went through.
"""

from __future__ import annotations

from typing import Optional

import httpx

from src.config import Settings
from src.logging_config import get_logger

logger = get_logger(__name__)

ANTHROPIC_VERSION = "2023-06-01"

_SYSTEM_PROMPT = (
    "You write one- or two-sentence trade log entries for an autonomous, rule-based "
    "trading system. You are NOT a trader and have no authority over trades — every "
    "decision (symbol, side, quantity, entry, stop-loss, take-profit) has already been "
    "made by fixed strategy rules before you are called, and cannot be changed by you. "
    "Your only job is to explain, in plain factual English, why the stated rule fired "
    "and what the standardized exit levels are. Use ONLY the facts given to you. Do not "
    "suggest a different action, price, or size. Do not express doubt, excitement, or "
    "any opinion about whether the trade is a good idea — state the facts plainly."
)


async def _call_llm(settings: Settings, user_prompt: str) -> Optional[str]:
    if not settings.llm_narration_enabled or not settings.llm_api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                settings.llm_api_base_url,
                headers={
                    "x-api-key": settings.llm_api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": settings.llm_model,
                    "max_tokens": 200,
                    "system": _SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
            )
        if response.status_code >= 400:
            logger.warning("llm_narration_http_error", status_code=response.status_code, body=response.text[:500])
            return None
        body = response.json()
        parts = [block.get("text", "") for block in body.get("content", []) if block.get("type") == "text"]
        text = "".join(parts).strip()
        return text or None
    except Exception as exc:
        logger.warning("llm_narration_call_failed", error=str(exc))
        return None


def _entry_template(strategy_name: str, symbol: str, side: str, entry_price: float, stop_loss: float, take_profit: float, rule_rationale: str, reward_risk_ratio) -> str:
    return (
        f"{strategy_name} rule fired on {symbol}: {rule_rationale} "
        f"Entered {side} at ${entry_price:.2f} with a standardized 1:{reward_risk_ratio} stop/target "
        f"(stop ${stop_loss:.2f}, target ${take_profit:.2f})."
    )


def _exit_template(symbol: str, exit_reason: str, entry_price: float, exit_price: float, pnl_usd: float) -> str:
    direction = "profit" if pnl_usd >= 0 else "loss"
    return (
        f"Closed {symbol} on {exit_reason}: entered at ${entry_price:.2f}, exited at ${exit_price:.2f}, "
        f"a ${abs(pnl_usd):.2f} {direction}."
    )


async def narrate_entry(
    settings: Settings,
    *,
    strategy_name: str,
    symbol: str,
    side: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    rule_rationale: str,
    reward_risk_ratio,
) -> str:
    fallback = _entry_template(strategy_name, symbol, side, entry_price, stop_loss, take_profit, rule_rationale, reward_risk_ratio)
    prompt = (
        f"Strategy: {strategy_name}\nSymbol: {symbol}\nSide: {side}\n"
        f"Entry price: ${entry_price:.2f}\nStop-loss: ${stop_loss:.2f}\nTake-profit: ${take_profit:.2f}\n"
        f"Standardized risk:reward: 1:{reward_risk_ratio}\n"
        f"Rule that fired: {rule_rationale}\n\n"
        "Write the one- or two-sentence log entry for this trade."
    )
    return await _call_llm(settings, prompt) or fallback


async def narrate_exit(
    settings: Settings,
    *,
    symbol: str,
    exit_reason: str,
    entry_price: float,
    exit_price: float,
    pnl_usd: float,
) -> str:
    fallback = _exit_template(symbol, exit_reason, entry_price, exit_price, pnl_usd)
    prompt = (
        f"Symbol: {symbol}\nExit reason: {exit_reason}\nEntry price: ${entry_price:.2f}\n"
        f"Exit price: ${exit_price:.2f}\nP/L: ${pnl_usd:.2f}\n\n"
        "Write the one- or two-sentence log entry for this closed trade."
    )
    return await _call_llm(settings, prompt) or fallback


__all__ = ["narrate_entry", "narrate_exit"]
