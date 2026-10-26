"""Generated round-eight corpora and authenticated HTTP -> prepare -> dispatch probes."""

from itertools import product

import pytest

from backend.application.safety_gate.resources import load_all_curated_patterns, match_curated
from backend.application.safety_gate.text_matching import fold_text
from backend.application.script_authoring.answer_context import deflection_for, question_kind
from . import test_approved_speech_active as active
from .test_approved_speech_active import decision, prepare
from .test_comment_guard_round7 import GOODS as PREVIOUS_GOODS
from .test_safety_gate_active import canonical, post, probes

case_factory = active.case_factory
SETS = load_all_curated_patterns()
COMMERCE = {"commerce", "current_product", "other_product"}

GOODS = PREVIOUS_GOODS + (
    "nồi cơm điện|đồ lao động|đồ lao công|sách ngôn ngữ|áo khoác|áo sơ mi|áo thun|quần dài|"
    "quần đùi|quần jean|váy hoa|váy ngủ|đồ ngủ|áo ngủ|bộ đồ ngủ|đồ cho bé|đồ chơi|búp bê|"
    "gấu bông|xe đồ chơi|bánh cút|bánh quy|bánh mì|bánh gạo|bánh tráng|kẹo dẻo|kẹo bùi|"
    "hạt điều|hạt dẻ|hạt bí|hạt sen|hạt chia|trà đào|trà sen|trà gừng|cà phê|bột cacao|"
    "sữa bột|sữa tươi|sữa chua|nước ép|nước ngọt|nước khoáng|mì gói|bún khô|phở khô|"
    "gạo lứt|gạo nếp|ngũ cốc|yến mạch|dầu ăn|nước mắm|xì dầu|muối biển|đường phèn|mật ong|"
    "mứt dừa|mứt gừng|cá khô|tôm khô|rong biển|nấm hương|nấm mèo|măng khô|miến dong|"
    "máy sấy tóc|máy ép trái cây|máy hút bụi|máy lọc nước|máy lọc không khí|máy giặt|tủ lạnh|"
    "bếp từ|bếp điện|lò vi sóng|lò hâm nóng|chảo chống dính|nồi inox|dao bếp|thớt gỗ|muỗng gỗ|"
    "đũa tre|bát sứ|đĩa sứ|ly thủy tinh|hộp cơm|hộp đựng đồ|giá phơi|móc treo|sọt rác|"
    "chổi quét|cây lau nhà|khăn lau|bàn chải điện|kem đánh răng|dầu gội|dầu xả|sữa tắm|xà phòng|"
    "nước rửa chén|nước lau sàn|túi rác|giấy ăn|giấy vệ sinh|khẩu trang|găng tay|mũ bảo hiểm|"
    "áo mưa|ủng đi mưa|giày thể thao|giày da|dép quai hậu|dép lê|túi xách|túi đeo|túi du lịch|"
    "vali kéo|ví cầm tay|kính đọc sách|sách nấu ăn|sách ngoại ngữ|vở học sinh|bút bi|bút chì|"
    "thước kẻ|tẩy trắng|bảng viết|khung ảnh|khung tranh|đèn bàn|đèn pin|pin sạc|ổ cắm|dây điện|"
    "cáp sạc|chuột máy tính|màn hình|loa bluetooth|micro|webcam|giá đỡ|chân máy ảnh|thẻ nhớ|"
    "ổ cứng|vợt cầu lông|bóng đá|bóng rổ|thảm tập|dây nhảy|bình tưới|chậu cây|hạt giống"
).split("|")
HOSTS = ("chị", "em", "shop", "admin", "chủ shop", "cửa hàng")
TAILS = ("", "?", "ạ", "vậy ạ", "chị", "nhé", "shop ơi", "nha chị")
PRIVATE = (
    "người yêu|chồng|vợ|bạn trai|bạn gái|con cái|con|mấy con|bao nhiêu tuổi|sinh năm 1990|"
    "quê ở Hà Nội|thích mua sắm|từng đi du lịch|định kết hôn|độc thân|ở đâu|làm nghề gì|"
    "hay ngủ muộn|gia đình|vui|khỏe|buồn|mệt|đói|khát|rảnh"
).split("|")
BENIGN_PHRASES = (
    "đồ lao động|đồ lao công|đồ cho bé|đồ ngủ cho bé|áo ngủ|bộ đồ ngủ|size lớn|các màu|đủ size|"
    "vải dư|buổi tối|túi đeo|kẹo dẻo|hạt bùi|trà đào|đồ đi dạo|bánh cút|rẻ hơn|lò hâm nóng|"
    "khung ảnh|bảo hành|sách ngôn ngữ|bánh ngon|bộ máy|mã máy|bộ lao động|vỏ điện thoại|"
    "đồ điện|đồ khôn lớn cho bé|đèn ngủ|ngủ sớm|đi ngủ|dù che|đều đẹp|địa chỉ|"
    "điện thoại|hàm lượng|cái lớn|cái buổi tối|vải cả lớn nhỏ|con đi học|đi mẹ mua đồ|cho dễ dùng|"
    "bố mua áo|má mua đồ|đồ khỉ bông|con khỉ bông|khôn lớn|ngon miệng|đủ màu|các loại|dẻo dai"
).split("|")


def variants(texts):
    return sorted({variant for text in texts for variant in (text, fold_text(text))})


SALES = variants(
    f"{host} có {good} không {tail}".strip() for host, good, tail in product(HOSTS, GOODS, TAILS)
)
PERSONAL = variants(
    f"{host} có {private} không {tail}".strip()
    for host, private, tail in product(HOSTS, PRIVATE, TAILS)
)
BENIGN = variants(
    f"{request} {phrase}{tail}"
    for request, phrase, tail in product(
        ("cho em xem", "chị có", "em muốn mua"), BENIGN_PHRASES, ("", " không ạ?")
    )
) + ["ngủ như heo", "ngu som", "di ngu", "do ngu cho be", "ao ngu", "bo do ngu"]


def obfuscate(text):
    return " ".join(".".join(word) for word in text.split())


ABUSE_BASE = (
    [
        f"{insult} {link} {animal}"
        for insult, link, animal in product(
            ("ngu", "đần", "dốt"),
            ("như", "nhu"),
            (
                "chó",
                "bò",
                "lợn",
                "heo",
                "trâu",
                "gà",
                "vịt",
                "con chó",
                "con bò",
                "con lợn",
                "con heo",
                "con trâu",
                "con gà",
                "con vịt",
            ),
        )
    ]
    + [
        f"mày là {address} {insult}"
        for address, insult in product(
            ("đồ", "thằng", "con"), ("ngu", "mất dạy", "khốn nạn", "súc vật", "rác rưởi")
        )
    ]
    + ["ngu vãi", "ngu vl", "dốt vãi", "ngu nhu chó", "ngu vai"]
)
ABUSE = sorted({text for base in variants(ABUSE_BASE) for text in (base, obfuscate(base))})


def test_generated_corpus_minimums():
    assert len(set(GOODS)) >= 150
    assert len(set(PRIVATE)) >= 20
    assert len(SALES) >= 150 * 4 * 8 * 2
    assert len(PERSONAL) >= 20 * 4 * 2
    assert len(ABUSE) >= 300
    assert len(BENIGN) >= 300


@pytest.mark.parametrize("host,tail,ascii_only", list(product(HOSTS, TAILS, (False, True))))
def test_generated_goods(host, tail, ascii_only):
    # Group by host/tail so collection stays bounded while every item is checked.
    for good in GOODS:
        text = f"{host} có {good} không {tail}".strip()
        if ascii_only:
            text = fold_text(text)
        assert question_kind(text, []) in COMMERCE, text
        assert deflection_for(text, []), text


@pytest.mark.parametrize("text", PERSONAL)
def test_generated_private(text):
    assert question_kind(text, []) == "not_answerable_non_commerce", text


@pytest.mark.parametrize("text", ABUSE)
def test_generated_abuse(text):
    assert "toxicity" in match_curated(text, SETS), text


@pytest.mark.parametrize("text", BENIGN)
def test_generated_benign(text):
    assert not match_curated(text, SETS), text


@pytest.mark.parametrize("ending", ("không", "chưa", "hông", "ko"))
@pytest.mark.parametrize("verb", ("có", "bán", "còn"))
def test_availability_inside_sentence(ending, verb):
    assert question_kind(f"chị {verb} nồi cơm điện {ending} vậy chị ơi ạ?", []) in COMMERCE
    assert question_kind(f"chị {verb} máy xay {ending}, cho em biết với", []) in COMMERCE


HTTP_CASES = [
    ("chị có máy xay không chị?", "commerce"),
    ("chị có nồi cơm điện không vậy ạ?", "commerce"),
    ("chị có đồ lao động không?", "commerce"),
    ("chi co do lao cong khong nha chi", "commerce"),
    ("chị có sách ngôn ngữ không?", "commerce"),
    ("shop có áo khoác không shop ơi?", "commerce"),
    ("shop có người yêu không?", "private"),
    ("shop có vui không?", "private"),
    ("admin có khỏe không?", "private"),
    ("cửa hàng có buồn không?", "private"),
    ("ngu nhu cho", "abuse"),
    ("ngu nhu chó", "abuse"),
    ("ngu nhu con bo", "abuse"),
    ("ngu vai", "abuse"),
    ("đần như con heo", "abuse"),
    ("d.o.t n.h.u c.o.n c.h.o", "abuse"),
    ("do ngu cho be", "benign"),
    ("ngủ như heo", "benign"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text,kind", HTTP_CASES)
async def test_http_prepare_dispatch(case_factory, monkeypatch, text, kind):
    case = await case_factory()
    case.llm.output = ""
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["events"][0]["safety"]["resource_versions"]["toxicity"] == "6"
    if kind == "abuse":
        assert result["rejected"] == 1
        assert result["events"][0]["safety"]["accepted"] is False
        coordinator.assert_not_called()
        reducer.assert_not_called()
        assert not case.llm.started.is_set() and not case.tts.calls
        return
    assert result["accepted"] == 1
    coordinator.assert_called_once()
    reducer.assert_called_once()
    await case.d.reducer.run_once(case.sid, reducer.call_args.kwargs["comment"].ts)
    turn = decision(case, generated=True)
    turn.cluster_members = (text,)
    turn.cluster_member_ids = (reducer.call_args.kwargs["comment"].comment_id,)
    turn.source_event_ids = ("event-1",)
    await prepare(case, turn)
    assert case.llm.started.is_set()
    if kind == "commerce":
        assert not turn.is_cancelled and turn.prepared_script
        assert await case.d.coordinator._maybe_speak(case.sid, turn)
        assert case.tts.calls
    else:
        assert turn.is_cancelled
        assert not turn.prepared_script and not case.tts.calls
