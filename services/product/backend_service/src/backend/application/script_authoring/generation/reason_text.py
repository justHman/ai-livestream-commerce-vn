"""Plain Vietnamese for WHY a part fell back to the owner's own wording.

Pure and dependency-free so the API and the Web can both use it. Input is the raw per-attempt reason
list a unit recorded (gate rule ids such as ``SPEECH_DURATION_SEGMENT``, guard codes such as
``truncated`` or ``guard:truncated``, ``llm_failed:<Error>``, budget codes). Like
``unit_brief.fallback_reason_code`` the LAST recognised reason decides. Unknown input gives "".
"""

from __future__ import annotations

_FALLBACK = "đã dùng thông tin gốc của bạn"

REASON_TEXT: dict[str, str] = {
    "SPEECH_DURATION_SEGMENT": "Phần này dài quá giới hạn thời gian của một phần nên mình đã rút gọn",
    "too_long": "Phần này dài quá giới hạn thời gian của một phần nên mình đã rút gọn",
    "truncated": f"AI viết dở câu, {_FALLBACK}",
    "empty": f"AI trả về nội dung rỗng, {_FALLBACK}",
    "foreign_script": f"AI lẫn chữ ngoài tiếng Việt, {_FALLBACK}",
    "coverage": f"AI bỏ sót thông tin bắt buộc, {_FALLBACK}",
    "missing_info": f"AI nói như thể thiếu thông tin, {_FALLBACK}",
    "brand_repeat": f"AI nhắc tên thương hiệu quá nhiều, {_FALLBACK}",
    "name_repeat": f"AI nhắc tên sản phẩm quá nhiều, {_FALLBACK}",
    "hard_sell": f"AI dùng lời thúc giục mua hàng mà bạn chưa viết, {_FALLBACK}",
    "cta": f"AI chèn lời mời mua hoặc nhắn tin sai chỗ, {_FALLBACK}",
    "personal_experience": f"AI bịa trải nghiệm cá nhân, {_FALLBACK}",
    "bridge": f"AI nhắc sang sản phẩm khác sai chỗ, {_FALLBACK}",
    "address": f"AI chèn địa chỉ hoặc liên hệ, {_FALLBACK}",
    "position": f"AI nói 'đầu tiên' hoặc 'cuối cùng' sai chỗ, {_FALLBACK}",
    "mashup": f"AI ghép các ý không liên quan vào một câu, {_FALLBACK}",
    "opener": f"AI mở đầu bằng lời cam kết bạn chưa viết, {_FALLBACK}",
    "CLAIM_PRICE": f"AI nói giá không khớp giá đã duyệt, {_FALLBACK}",
    "CLAIM_FACTUAL": f"AI nói điều không có trong thông tin đã duyệt, {_FALLBACK}",
    "semantic_budget_exhausted": f"Đã hết lượt viết cho phần này, {_FALLBACK}",
    "time_budget_exhausted": f"Hết thời gian viết phần này, {_FALLBACK}",
    "provider_unavailable": f"Chưa kết nối được để viết lại, {_FALLBACK}",
}
_CLAIM_OTHER = f"Lời thoại chưa khớp thông tin sản phẩm, {_FALLBACK}"
_RULE_OTHER = f"Lời thoại chưa đạt kiểm tra, {_FALLBACK}"


def _key(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("llm_failed:"):
        error = raw.split(":", 1)[1]
        return (
            "time_budget_exhausted"
            if "Timeout" in error or "Deadline" in error
            else "provider_unavailable"
        )
    if raw in ("model_budget_unavailable", "segment generator returned no result"):
        return "provider_unavailable"
    name = raw.removeprefix("guard:")
    if name in REASON_TEXT:
        return name
    if name.startswith("CLAIM_"):
        return "CLAIM_OTHER"
    if name.isupper() and "_" in name:
        return "RULE_OTHER"
    return ""


def reason_text(reasons: list[str] | tuple[str, ...]) -> str:
    """One short sentence for the owner; the last recognised reason decides; "" when none is known."""
    for raw in reversed([str(r) for r in reasons]):
        key = _key(raw)
        if key == "CLAIM_OTHER":
            return _CLAIM_OTHER
        if key == "RULE_OTHER":
            return _RULE_OTHER
        if key:
            return REASON_TEXT[key]
    return ""
