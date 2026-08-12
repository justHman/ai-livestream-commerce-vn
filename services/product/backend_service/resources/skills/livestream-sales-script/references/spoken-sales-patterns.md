# Spoken sales patterns: writing Vietnamese speech a host can say

This file is a reference for the `GENERATE_SCRIPT_SEGMENT` operation in
`SKILL.md`. It is packaged with the skill and shipped with the backend;
it is never fetched at runtime.

## How spoken Vietnamese differs from written Vietnamese

- Short sentences. One idea per sentence; a comma splice means split it.
- Spoken number forms: "hai trăm chín mươi chín nghìn đồng" instead of
  "299.000đ"; "hai mươi phần trăm" instead of "20%"; "nửa giờ" instead of
  "30 phút" when natural.
- No markup, no emojis, no URLs, no email addresses. Anything a TTS engine
  would stumble on is forbidden.
- Punctuation only for rhythm: comma, period, question mark. No em-dash,
  no parentheses, no repeated punctuation, no hidden control characters.

## Feature → benefit patterns

- Lead with the outcome, then the support: "Bạn sẽ hết lo hết pin giữa
  ngày — máy này sạc đầy trong nửa giờ." (For TTS, prefer: "Bạn sẽ không
  còn lo hết pin giữa ngày. Máy này sạc đầy chỉ trong nửa giờ.")
- A feature is a property; a benefit is the customer outcome. Never list
  specs without outcomes.
- Use the exact values from the context — specifics out-believe adjectives.

## Objection patterns

- Name the concern in the customer's words, then answer it with a fact
  from the context: "Nhiều bạn hỏi có nặng không. Máy này chỉ hai trăm
  gam, cầm cả buổi không mỏi tay."
- One objection per answer, answered completely, then move on.

## CTA patterns

- A CTA is an explicit instruction: "Bấm nút mua ngay bên dưới để đặt
  hàng." It belongs in the segment the plan assigned.
- Do not stack CTAs. Non-CTA segments end with a transition, not a plea.
- Never fabricate urgency ("chỉ còn hôm nay") unless the promotion context
  supplies it.

## Honesty patterns

- Only facts supplied in the authoritative context. No invented reviews,
  certifications, statistics, or testimonials.
- No unsupported superlatives ("tốt nhất", "số một"). "Rất" is fine when
  the fact supports it; "số một" is not.
- If a customer question would need a claim you do not have, redirect to a
  covered benefit instead of inventing.

## Segment boundaries

- Write only the assigned segment. Do not re-open the product, do not
  recap the whole script, do not preview later segments.
- Build on the continuity state: avoid repeated openings, repeated facts,
  repeated objections, repeated CTAs.
- End cleanly: a transition to the next topic, or the assigned CTA at the
  close. The full close belongs only to the final segment.

## Example

Assigned: segment 3 of 6, topic "Sạc nhanh", fact "sạc 67W, đầy pin trong
35 phút", previous topic "Thiết kế mỏng nhẹ".

"Tiếp theo, mình nói về cái thứ mà ai cũng quan tâm: thời gian sạc.
Nhiều bạn đi làm cả ngày, tối về mới sạc điện thoại. Với chiếc máy này,
bạn chỉ cần cắm sạc ba mươi lăm phút là pin đầy. Sạc nhanh sáu mươi bảy
oát, nên chỉ cần nghỉ trưa một chút là bạn đã có cả buổi chiều dùng thoải
mái. Còn thời lượng pin thì để lát nữa mình nói tiếp nhé."

- One topic, one fact, no re-opening, no CTA (not the assigned close), a
  clean transition to the next topic.
- Spoken number forms, short sentences, punctuation limited to comma,
  period, and question mark.
