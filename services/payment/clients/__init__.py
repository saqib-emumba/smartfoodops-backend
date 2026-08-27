"""Outbound calls to sibling services, one module per sibling called.

Payment calls exactly one today — the Order Service, to verify the order it settles — but
the directory shape is kept uniform with services that call more than one.

Docstring only: no re-exports.
"""
