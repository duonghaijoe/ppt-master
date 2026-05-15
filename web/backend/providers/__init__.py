"""Provider wrappers.

Each module in this package wraps one outbound billable provider call and
records a ``UsageEvent`` for it. The wrappers read their keys from the
platform secrets layer (env-backed for v1); tenants never see provider
keys.
"""
