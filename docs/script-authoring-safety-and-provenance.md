# Script Authoring — Safety & Provenance

Status of third-party inspiration and moderation data for the
`livestream-sales-script` skill and the Vietnamese profanity/toxicity
policy. This document covers **provenance and licensing only** — no
external resource is fetched, bundled, or activated at runtime.

## 1. Copywriting / product-marketing inspiration (Decision 18)

The project-owned `livestream-sales-script` skill (task 5.x) adapts
**reviewed principles** from the following public reference materials.
These references are **inspiration only**; the runtime loads the
project-owned skill file from the repository and NEVER fetches a mutable
remote skill during a generation request.

| Source | File | Reference URL |
| --- | --- | --- |
| `coreyhaines31/marketingskills` | `skills/copywriting/SKILL.md` | https://github.com/coreyhaines31/marketingskills/blob/main/skills/copywriting/SKILL.md |
| `coreyhaines31/marketingskills` | `skills/product-marketing/SKILL.md` | https://github.com/coreyhaines31/marketingskills/blob/main/skills/product-marketing/SKILL.md |

Adapted principles (rewritten for Vietnamese spoken livestream selling,
long-form planning, segment continuity, TTS suitability, factual
constraints, and the `ScriptIntent`/rule system):

- clarity and specificity over vague claims;
- feature → benefit framing;
- customer language and objection handling;
- honest claims and CTA discipline;
- no unsupported evidence.

The adaptation is a new work owned by this project; upstream text is not
vendored verbatim.

## 2. Moderation / profanity data — curated policy input (Decision 19)

The Vietnamese profanity/offense lexicon and teencode/obfuscation patterns
used by the gate are **curated, versioned runtime resources owned by this
project** (task 3.5) — they are NOT a raw downloaded dataset, and the gate
makes no network lookup.

External corpora (e.g. Vietnamese offensive/hate-speech datasets) may be
used to **inform lexicon candidates and test fixtures** during curation,
but an external dataset-derived lexicon can be **activated as production
policy only after** all of the following are satisfied:

1. **License/provenance record** — dataset name, source URL, license,
   author/date, and any redistribution restrictions are recorded in this
   project's references (e.g. `references/datasets.yaml`) before use.
2. **Manual curation/versioning** — candidates are reviewed, deduplicated,
   normalized, and versioned by a human; the activated lexicon is a
   project-owned resource, not the raw corpus.
3. **False-positive tests** — including product/brand allowlist cases, so
   legitimate commerce terms are never blocked.
4. **Explicit severity/action semantics** — every entry maps to a
   deterministic gate severity (WARNING vs ERROR) and a repair
   instruction, per the rule registry.

## 3. Current activation status

- **No external dataset is activated in production policy as of this
  document.** The runtime lexicon is the project-curated resource; any
  future activation of dataset-derived entries is gated on the criteria in
  section 2.
- **No remote runtime dependency exists.** The gate is deterministic,
  offline, and content-private (design Decision 19, Decision 21).
