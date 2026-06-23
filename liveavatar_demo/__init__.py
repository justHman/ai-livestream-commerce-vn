"""LiveAvatar Demo — block-wise autoregressive streaming proof-of-concept.

Implements the full pipeline from LiveAvatar (arXiv 2512.04677) using
mock DiT inference with real anti-drift strategies:
- History Corrupt (noisy KV cache)
- Adaptive Attention Sink (AAS)
- Rolling RoPE
- Rolling KV Cache

When a powerful GPU is available, swap mock components with real
Wan2.2-S2V-14B + LoRA weights.
"""

__version__ = "0.1.0"
