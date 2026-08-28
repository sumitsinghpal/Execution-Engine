"""
Outbound notifications — a single Slack/Discord-compatible webhook (see
webhook.py) fired on trading-relevant events: kill switch trips/clears
(any scope, any trigger) and autonomous trade entries/exits. Opt-in via
settings.notification_webhook_url; unset means every call silently no-ops.
"""
