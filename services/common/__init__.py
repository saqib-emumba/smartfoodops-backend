"""Shared service chassis for SmartFoodOps.

This package holds *infrastructure* only — connection pooling, logging, HTTP error
translation, and cross-service transport. It deliberately contains no domain models,
business rules, or table knowledge: services stay decoupled at the domain level while
sharing the plumbing that would otherwise be copy-pasted into every new service.

Rule of thumb: if adding something here would let one service reason about another's
data, it belongs in that service, not in `common`.
"""
