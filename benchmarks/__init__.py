"""Benchmark fixtures + baselines for the legacy Director (pre-extraction safeguard).

Fixtures record deterministic input→output behavior of the legacy Director
under ``core/`` so the canonical backend's Director parity can be proven
after extraction. Baselines record machine-agnostic throughput/latency of the
key Director operations on a fixed corpus.
"""
