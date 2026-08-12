# Planning guidance: sustaining 10–60 minutes without repetition

This file is a reference for the `PLAN_PRODUCT_SCRIPT` operation in
`SKILL.md`. It is packaged with the skill and shipped with the backend;
it is never fetched at runtime. The model receives the operation
guidance in `SKILL.md`; this file documents the design rationale for the
project team and future editors.

## Why a 10–60 minute script needs an architecture, not a monologue

A single response cannot safely carry 10–60 minutes of spoken content
(Decision 7 — plan + fixed K segments). Repetition is the failure mode of
long scripts: the model reopens the hook, restates facts, and begs the
close because it has nothing new to say. The plan exists to make every
minute structurally different.

## Content architecture

| Section | Purpose | Facts/objections |
|---|---|---|
| Hook / problem framing | Name the buyer's problem in their words; earn the next 10 minutes | 0 |
| Product introduction | What it is, what it is for, in plain language | 1 intro fact |
| Feature → benefit | One feature per segment, always tied to a customer outcome | 1 fact each |
| Usage / demonstration guidance | Show the product in a real situation | 1–2 facts |
| Objection handling | Answer the real concern first, then the proof | 1 objection each |
| Use cases / customer scenarios | The same product, different situations | 1 fact per scenario |
| Offer / promotion | Price, discount, and terms exactly as supplied | promotion facts |
| Recap / CTA | Sum up the reasons to buy, then one clear CTA | 0 new facts |

Rules of distribution:

- Each fact appears at most once as a primary topic. Facts already used are
  never restated in a later segment.
- Each objection is handled at most once, in its own segment.
- CTAs are counted and placed — typically one mid-script (soft) and one at
  the close, or only at the close for shorter targets. A script that asks
  in every segment reads as begging.
- Openings are unique. The hook opens once; later segments open with a
  transition, never a second hook.

## Segment sizing

Segments of roughly 1–5 minutes keep the script varied and TTS-friendly
and bound each generation call. The segment durations must sum exactly to
the requested total; extend the middle (more use cases, more objections)
rather than padding the hook or the close.

## Transition policy

- `ORDER_AWARE`: plan an entry bridge and an exit bridge so the script can
  hand off to the previous/next product.
- `ORDER_AGNOSTIC`: keep the core self-contained with generic entry/exit
  language so the Director can reorder products at runtime (Decision 15).

## Constraints that protect the gate

The plan may reference only authoritative fact/objection IDs supplied by
the backend (task 7.6). An invented fact will fail the commerce-claims
gate; an invented objection cannot be answered honestly. The recap/CTA
segment never introduces new facts — it only sums up what was already said.
