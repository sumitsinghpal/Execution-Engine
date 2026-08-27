"""
The "agentic" half of autonomous trading: LLM involvement that narrates and
orchestrates, never decides. Every buy/sell/size/exit call in
src/execution/autonomous_trader.py is made by deterministic strategy rules
(src/strategy/catalog.py) and a standardized risk:reward ratio
(src/execution/risk_reward.py) before an LLM is ever consulted — see
llm_narrator.py's module docstring for why, and for what happens when no
LLM is configured at all.
"""
