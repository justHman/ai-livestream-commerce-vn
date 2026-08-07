# Commerce clustering baseline

Deterministic 12-comment Vietnamese commerce fixture, partitioned by category, product, and intent.

```text
Embedder                                      Threshold  Clusters  Singleton ratio  Expected  Missed  Unexpected
hashing-fallback                              0.375      12        1.000            0         4       0
bkai-foundation-models/vietnamese-bi-encoder  0.375       8        0.500            4         0       0
bkai-foundation-models/vietnamese-bi-encoder  0.400       9        0.667            3         1       0
bkai-foundation-models/vietnamese-bi-encoder  0.550       9        0.667            3         1       0
bkai-foundation-models/vietnamese-bi-encoder  0.700      11        0.909            1         3       0
```

Selected threshold: `0.375`.

Rationale: all four required paraphrase pairs merge with zero prohibited cross-product or cross-intent merges. The routing partitions provide the separation boundary; lowering the within-partition semantic threshold does not permit cross-partition merges.

Reproduce:

Use a Python environment containing `torch` and `sentence-transformers`, then run:

```powershell
$env:PYTHONPATH = (Get-Location).Path
python scripts\benchmark_commerce_clustering.py
python -m pytest core\tests\test_commerce_clustering.py -q
```
