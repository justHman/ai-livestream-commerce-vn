"""Deterministic fixture scenarios + generator for the legacy Director.

Scenario modules live under ``benchmarks/fixtures/scenarios/``; the
``generate_fixtures`` entrypoint (``__main__.py``) runs every scenario
against the canonical director (aliased as core.director by verify_parity) and writes input/output JSON under
``benchmarks/fixtures/out/``.
"""
