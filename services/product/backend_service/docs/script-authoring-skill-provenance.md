# Script authoring skill provenance

Provenance record for the project-owned `livestream-sales-script` skill
(OpenSpec `approved-script-authoring-pipeline`, tasks 5.1–5.5, Decision 18).

## What ships with the backend

```
resources/skills/livestream-sales-script/
├── SKILL.md                    # runtime skill loaded only by Generate (task 5.1)
└── references/
    ├── planning-guidance.md    # 10-60 min non-repetitive architecture (task 5.5)
    └── spoken-sales-patterns.md # VN spoken patterns + worked example (task 5.5)
```

The runtime loads only the project-owned files above. No third-party skill
content is fetched, copied, or embedded at runtime (Design Decision 18:
"Runtime MUST load the project-owned file from the repository/package. It
MUST NOT fetch a mutable remote skill during a generation request").

## Inspiration sources (reference only, rewritten for VN spoken use)

The skill was authored from the principles of two externally reviewed
copywriting/product-marketing skill documents. These are reference
material only — they are not vendored, not loaded at runtime, and the
project-owned skill is an original rewrite for Vietnamese spoken
livestream selling:

| Source | URL (reference only) | Principles adapted |
|---|---|---|
| `coreyhaines31/marketingskills` — `skills/copywriting/SKILL.md` | https://github.com/coreyhaines31/marketingskills/blob/main/skills/copywriting/SKILL.md | Clarity over cleverness, feature→benefit framing, specificity, customer language, honest claims, CTA discipline |
| `coreyhaines31/marketingskills` — `skills/product-marketing/SKILL.md` | https://github.com/coreyhaines31/marketingskills/blob/main/skills/product-marketing/SKILL.md | Objection handling, use-case framing, promotion/offer presentation, audience-first structure |

License: the `coreyhaines31/marketingskills` repository is public
reference material. No code, prompt text, or data was copied; the adapted
principles are general writing guidance, and every section of the
project-owned skill was written from scratch for this project's domain
(Vietnamese spoken sales, long-form planning, segment continuity,
TTS-readiness, and the project's rule system — see Design Decision 18).

## Why the skill has two operations

Per Design Decision 18 the skill MUST include at least two operation
sections:

- `PLAN_PRODUCT_SCRIPT` — build a non-repetitive 10–60 minute content
  architecture with topic/fact/objection/CTA distribution (task 5.3).
- `GENERATE_SCRIPT_SEGMENT` — write only the assigned segment, respect
  continuity/remaining coverage, use natural spoken Vietnamese, and avoid
  prematurely recapping the whole product (task 5.4).

## Repair never loads this skill

`Fix with AI` uses only the failed rules' repair instructions and the
authoritative facts needed to prevent claim drift (Design Decision 5,
task 5.7). The sales skill is never part of a repair prompt.

## Versioning and fingerprinting

The skill content is versioned in the repository. `SkillLoader`
(`src/backend/application/script_authoring/generation/skill_loader.py`)
loads the packaged `SKILL.md` and exposes:

- `skill_version()` — the immutable version string carried in the file
  frontmatter (`1.0.0`);
- `content()` — the full skill text;
- `content_hash()` — a stable SHA-256 over the exact file bytes, recorded
  in `GenerationFingerprint.skill_version` for reproducibility (Decision
  13).

Any edit to `SKILL.md` changes the hash and therefore the fingerprint of
future generations; already-persisted generations keep their recorded
fingerprint.
