"""Anti-drift module — strategies to prevent long-horizon drift in block-wise autoregressive streaming.

Implements the 4 anti-drift techniques from LiveAvatar (arXiv 2512.04677):
1. History Corrupt (noisy KV cache)
2. Adaptive Attention Sink (AAS — replace sink with first latent)
3. Rolling RoPE (dynamic position reassignment)
4. Rolling KV Cache (fixed-size window, FIFO eviction)
"""

from .rolling_kv_cache import RollingKVCache, KVEntry
from .history_corrupt import corrupt_kv_cache
from .adaptive_attention_sink import AdaptiveAttentionSink
from .rolling_rope import RollingRoPE

__all__ = [
    "RollingKVCache",
    "KVEntry",
    "corrupt_kv_cache",
    "AdaptiveAttentionSink",
    "RollingRoPE",
]
