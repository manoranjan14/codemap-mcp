"""Fixture for module-level (top-level) call attribution.

Calls made at module scope - outside any def/class - are real calls: they
run at import time. They were previously dropped entirely, because calls
were only ever collected while visiting a function/method node. See
tests/test_module_level_calls.py.
"""
from repo_helper import Helper


def top_target():
    return 1


def nested_only_target():
    return 2


# Attributed to the MODULE node.
RESULT = top_target()
INSTANCE = Helper()
CHAINED = Helper().assist()  # both resolve: the receiver's type is written


def wrapper():
    # Attributed to `wrapper`, NOT to the module - the existing
    # nested-scope rule must keep holding in both directions.
    return nested_only_target()
