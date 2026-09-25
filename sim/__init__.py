"""Synthetic user simulator and load-test harness for the newsletter API.

Validates *behavior* (cold-start coverage, click reactivity, drift adaptation,
fallback, data flow) and generates load. It is not an accuracy benchmark -
see docs/adr/0019-user-simulator-design-and-claim-scope.md.
"""
