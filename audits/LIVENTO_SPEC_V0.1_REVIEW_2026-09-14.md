# Review Livento Product Spec v0.1 — từ repo AI backend

**Ngày:** 2026-09-14
**Người review:** Nam
**Trạng thái:** Review hoàn thành — **chưa sửa code theo spec**
**Loại tài liệu:** evidence report (không phải product truth)

---

## Hướng dẫn đọc cho agent

Tài liệu này là **evidence report**, không phải nguồn chân lý sản phẩm. Theo authority model của `SOURCE_OF_TRUTH.md`, nó thuộc mức 6 (agent reports).

Cấu trúc để máy đọc:

- Mỗi phát hiện có **ID ổn định** dạng `REV-AI-NNN`. ID không bao giờ được dùng lại.
- Mỗi phát hiện có khối metadata: `spec_ref` · `verdict` · `code_status` · `action`.
- Khối `json` bên dưới chứa toàn bộ phát hiện ở dạng máy đọc được. Agent nên parse khối đó thay vì đọc prose.
- Bảng từ vựng đóng ở mục "Từ vựng" — chỉ dùng các giá trị trong đó.

Nếu bạn là agent đang cập nhật spec: **chỉ sửa những gì `action` yêu cầu.** Không suy rộng từ một phát hiện sang vùng chưa được kiểm.

---

## Phạm vi (scope)

| Mục | Giá trị |
|---|---|
| Đã kiểm | repo AI backend `justHman/ai-livestream-commerce-vn` @ `90415d7` |
| Đã kiểm | `services/product/backend_service/` · `core/` · `openspec/` |
| **CHƯA kiểm** | repo Livento (Go) của team |
| **CHƯA kiểm** | trạng thái deploy, cấu hình môi trường thật |
| **CHƯA kiểm** | hành vi lúc chạy — không chạy test, không quan sát runtime |
| **CHƯA kiểm** | capability thật của TikTok / bên cung cấp avatar |
| Spec được review | `nhatanh-dev/livento-product-spec` @ `3d9cd94` |
| Mốc spec dùng để đối chiếu | `eead8f2` |

**Phương pháp:** đọc trực tiếp source; dùng `git ls-tree` để xác nhận file tồn tại ở mốc `eead8f2`; đếm số file bên ngoài mỗi package import nó để xác định mức độ nối vào hệ thống chạy.

**Giới hạn:** mọi kết luận là **sự kiện ở mức source**. Không có tuyên bố nào về hành vi production.

---

## Từ vựng

`verdict` — đối chiếu giữa spec và code:

| Giá trị | Nghĩa |
|---|---|
| `MATCHES` | spec mô tả đúng thực tế |
| `UNDERSTATES` | code đã làm **nhiều hơn** spec ghi |
| `OVERSTATES` | spec ghi có, thực tế **không có** |
| `SILENT` | spec không nhắc tới |

`code_status`:

| Giá trị | Nghĩa |
|---|---|
| `IMPLEMENTED` | có, đã nối, đang chạy |
| `PARTIAL` | có một phần |
| `BUILT_NOT_WIRED` | code viết xong nhưng không được gọi ở đâu |
| `MISSING` | không tìm thấy |
| `NOT_APPLICABLE` | thuộc repo khác |

`action`:

| Giá trị | Nghĩa |
|---|---|
| `UPDATE_GAP_REGISTER` | sửa dòng tương ứng trong `GAP_REGISTER.md` |
| `UPDATE_FEATURE_MATRIX` | sửa dòng tương ứng trong `FEATURE_MATRIX.md` |
| `UPDATE_CROSS_REPO_CONTRACTS` | sửa `CROSS_REPO_CONTRACTS.md` |
| `CLARIFY_SPEC` | thêm làm rõ trong spec |
| `TEAM_DECISION` | cần team quyết, không phải việc của spec |
| `NO_SPEC_ACTION` | không cần sửa gì |

---

## Kết luận ngắn

Hướng sản phẩm trong spec hợp lý. Không có bất đồng về nghiệp vụ.

Vấn đề nằm ở phần **"code hiện tại đang thế nào"**: spec đọc thiếu repo AI backend.

- `REV-AI-009` — spec ghi `MISSING`, thực tế **đã hoàn thành và đang chạy**
- `REV-AI-007` — spec ghi **có** `pause/resume`, thực tế **không tồn tại**

Vì bước lập kế hoạch triển khai dựa trên Gap Register, hai chỗ này làm sai thứ tự ưu tiên.

---

## Bảng trạng thái

| ID | Điểm | `verdict` | `code_status` | `action` |
|---|---|---|---|---|
| REV-AI-001 | Đổi thứ tự bán hàng theo nhu cầu | `UNDERSTATES` | `PARTIAL` | `UPDATE_GAP_REGISTER` |
| REV-AI-002 | "Đã nói đủ thì mãi là đủ" | `MATCHES` | `BUILT_NOT_WIRED` | `UPDATE_GAP_REGISTER` |
| REV-AI-003 | Spec để trống toàn bộ con số | `MATCHES` | `NOT_APPLICABLE` | `TEAM_DECISION` |
| REV-AI-004 | 19 tài liệu thiết kế trong repo | `SILENT` | `PARTIAL` | `CLARIFY_SPEC` |
| REV-AI-005 | "Team đã xác nhận" chỉ 4 mục, không ngày | `MATCHES` | `NOT_APPLICABLE` | `TEAM_DECISION` |
| REV-AI-006 | Phiên kết thúc nhưng không đóng sổ | `MATCHES` | `MISSING` | `UPDATE_GAP_REGISTER` |
| REV-AI-007 | Nút điều khiển: spec ghi thừa | `OVERSTATES` | `MISSING` | `UPDATE_GAP_REGISTER` |
| REV-AI-008 | Phát video ra platform ngoài | `MATCHES` | `NOT_APPLICABLE` | `UPDATE_CROSS_REPO_CONTRACTS` |
| REV-AI-009 | Quy trình duyệt nội dung | `UNDERSTATES` | `IMPLEMENTED` | `UPDATE_GAP_REGISTER` |
| REV-AI-010 | Hai bản Director + reducer không ai đọc | `MATCHES` | `PARTIAL` | `UPDATE_FEATURE_MATRIX` |
| REV-AI-011 | Bộ lọc bảo vệ viết xong không ai gọi | `MATCHES` | `BUILT_NOT_WIRED` | `TEAM_DECISION` |
| REV-AI-012 | Không gửi số phút về hệ thống tính tiền | `MATCHES` | `MISSING` | `UPDATE_CROSS_REPO_CONTRACTS` |
| REV-AI-013 | Không khôi phục phiên khi crash | `MATCHES` | `MISSING` | `UPDATE_GAP_REGISTER` |
| REV-AI-014 | Trạng thái phiên chỉ là chuỗi, không ai đọc | `MATCHES` | `MISSING` | `UPDATE_GAP_REGISTER` |

---

## Tóm tắt máy đọc

<details>
<summary>json</summary>

```json
{
  "report": {
    "id": "livento-spec-v0.1-review-ai-backend",
    "date": "2026-09-14",
    "type": "evidence",
    "status": "review_complete_no_code_change",
    "reviewed_repo": {
      "name": "justHman/ai-livestream-commerce-vn",
      "commit": "90415d7",
      "subtree": ["services/product/backend_service/", "core/", "openspec/"]
    },
    "spec": { "repo": "nhatanh-dev/livento-product-spec", "commit": "3d9cd94" },
    "spec_reference_commit": "eead8f2",
    "not_reviewed": [
      "Livento Go repository",
      "deployment and environment configuration",
      "runtime behaviour (no tests executed, no runtime observation)",
      "TikTok and avatar-vendor real capability"
    ],
    "method": "direct source inspection; git ls-tree for baseline existence; external-importer count per package for wiring"
  },
  "findings": [
    {
      "id": "REV-AI-001",
      "title": "Đổi thứ tự bán hàng theo nhu cầu",
      "spec_ref": ["BR-DIRECTOR-003", "DR-DIRECTOR-003", "GAP-DIRECTOR-001"],
      "verdict": "UNDERSTATES",
      "code_status": "PARTIAL",
      "action": "UPDATE_GAP_REGISTER",
      "claim": "Spec đòi 6 tiêu chí xếp hạng; code có 1 tiêu chí (mức độ người xem nhắc tới).",
      "spec_gap_text": "target scoring/pressure planner absent",
      "evidence": [
        { "path": "services/product/backend_service/src/backend/application/director/pivot.py", "what": "should_enter_pivot / should_exit_pivot — demand-share pivot" },
        { "path": "services/product/backend_service/src/backend/application/director/config.py:59-62", "what": "min_comments=5, enter_share=0.60, exit_share=0.45, score_margin=0.15" },
        { "path": "services/product/backend_service/src/backend/application/director/decision.py:549-587", "what": "_start_pivot lưu checkpoint; _resume_checkpoint quay lại đúng chỗ dừng" }
      ],
      "correction": "GAP-DIRECTOR-001/002: không phải 'planner absent' mà là 'planner có 1/6 tiêu chí'.",
      "missing_criteria": ["merchant priority", "promotion urgency", "uncovered value", "commerce signals", "protected budget"]
    },
    {
      "id": "REV-AI-002",
      "title": "Đã nói đủ thì mãi là đủ (semantic coverage)",
      "spec_ref": ["BR-DIRECTOR-003", "GAP-COVERAGE-001"],
      "verdict": "MATCHES",
      "code_status": "BUILT_NOT_WIRED",
      "action": "UPDATE_GAP_REGISTER",
      "claim": "Cơ chế ghi coverage đã có và đúng tính monotonic (chỉ cộng, không xoá), nhưng không nơi nào đọc lại.",
      "spec_gap_text": "Director has cursor/talking-point/product state, not target coverage record",
      "evidence": [
        { "path": "services/product/backend_service/src/backend/application/director/scoring.py:58", "what": "mark_coverage — embedding match, threshold 0.75" },
        { "path": "services/product/backend_service/src/backend/application/director/coordinator.py:1074-1087", "what": "nơi duy nhất ghi covered_points" },
        { "path": "services/product/backend_service/src/backend/application/director/state.py:189-194", "what": "mark_product_covered — union, chỉ cộng" },
        { "path": "services/product/backend_service/src/backend/application/director/decision.py", "what": "KHÔNG có tham chiếu nào tới covered_points" }
      ],
      "correction": "GAP-COVERAGE-001: coverage record CÓ tồn tại; thiếu là (a) source/timestamp, (b) PARTIAL/SUFFICIENT levels, (c) revisit eligibility, (d) không được tiêu thụ."
    },
    {
      "id": "REV-AI-003",
      "title": "Spec để trống toàn bộ con số",
      "spec_ref": ["DECISION_LOG.md#explicitly-unapproved-numeric-decisions"],
      "verdict": "MATCHES",
      "code_status": "NOT_APPLICABLE",
      "action": "TEAM_DECISION",
      "claim": "~20 con số vận hành đang chạy trong config.py; spec cấm suy chúng thành luật sản phẩm.",
      "blocking": true,
      "evidence": [
        { "path": "services/product/backend_service/src/backend/application/director/config.py", "what": "StreamConfig — 360s product budget, 75s opening, 75s selection window, 4 clusters, 45s decay, pivot thresholds" }
      ],
      "action_detail": "Cần chốt: đo thực tế · quyết định kinh doanh · hay tạm dùng giá trị hiện tại."
    },
    {
      "id": "REV-AI-004",
      "title": "19 tài liệu OpenSpec trong repo",
      "spec_ref": [],
      "verdict": "SILENT",
      "code_status": "PARTIAL",
      "action": "CLARIFY_SPEC",
      "claim": "Repo có 19 capability spec; 9 mô tả trực tiếp hành vi sản phẩm. Spec sản phẩm không nhắc tới chúng lần nào.",
      "note": "19 tài liệu này thuộc sở hữu riêng của người review, không phải quyết định của team.",
      "evidence": [
        { "path": "openspec/specs/", "what": "19 capability" },
        { "path": "openspec/specs/script-qna-speech-arbitration/spec.md:32,41,89-90", "what": "trùng gần nguyên văn BR-QA-002" }
      ],
      "wiring_by_package": {
        "external_importers": {
          "text_chunker": 13, "entity": 11, "render": 10, "director": 7, "db": 7,
          "script_authoring": 6, "contracts": 5, "clients": 4, "publishing": 3,
          "platform_events": 2, "reducer": 2, "schemas": 1,
          "evidence": 0, "agentic_director": 2, "live_runtime": 0, "safety_gate": 0
        },
        "note": "evidence/agentic_director chỉ được import từ trong cụm live_runtime (chính nó chưa được nối) — xem REV-AI-010."
      }
    },
    {
      "id": "REV-AI-005",
      "title": "TEAM_CONFIRMED_FACTS chỉ có 4 mục, không có ngày",
      "spec_ref": ["TEAM_CONFIRMED_FACTS.md", "SOURCE_OF_TRUTH.md"],
      "verdict": "MATCHES",
      "code_status": "NOT_APPLICABLE",
      "action": "TEAM_DECISION",
      "claim": "4 mục, cả 4 ghi 'historical exact date unavailable'. Mục này xếp ưu tiên cao hơn Decision Log nhưng không có biên bản.",
      "code_cross_check": {
        "TCF-SESSION-001": "khớp",
        "TCF-SCOPE-001": "khớp",
        "TCF-COMMERCIAL-001": "khớp",
        "TCF-IDENTITY-001": "không kiểm được bằng code"
      },
      "action_detail": "Đề nghị team xác nhận lại 4 mục và bổ sung nếu còn điều đã chốt trước đây."
    },
    {
      "id": "REV-AI-006",
      "title": "Phiên kết thúc nhưng không được đóng sổ",
      "spec_ref": ["BR-SESSION-002", "BR-PRICING-002", "GAP-RECOVERY-001"],
      "verdict": "MATCHES",
      "code_status": "MISSING",
      "action": "UPDATE_GAP_REGISTER",
      "claim": "Không có trạng thái terminal nào cho phiên. Postgres giữ 'active' vĩnh viễn.",
      "evidence": [
        { "path": "services/product/backend_service/src/backend/api/v1/sessions.py:272", "what": "xoá session khỏi store khi stop" },
        { "path": "services/product/backend_service/src/backend/api/v1/sessions.py:56", "what": "upsert_session — gọi đúng 1 lần, lúc bắt đầu, status='active'" },
        { "path": "services/product/backend_service/src/backend/application/db/postgres_store.py", "what": "chỉ có upsert_session và get_session; không có lệnh đóng phiên" }
      ],
      "causal_chain": ["REV-AI-014", "REV-AI-012", "REV-AI-013"]
    },
    {
      "id": "REV-AI-007",
      "title": "Nút điều khiển: spec ghi thừa",
      "spec_ref": ["BR-SESSION-004", "BR-QA-002", "GAP-OPERATOR-001"],
      "verdict": "OVERSTATES",
      "code_status": "MISSING",
      "action": "UPDATE_GAP_REGISTER",
      "claim": "Spec ghi 'pause/resume/stop/interrupt present'. pause và resume KHÔNG tồn tại.",
      "spec_gap_text": "pause/resume/stop/interrupt present",
      "evidence": [
        { "path": "services/product/backend_service/src/backend/api/v1/sessions.py", "what": "routes: 31 start, 73 say, 225 interrupt, 250 stop, 283 attach, 355 config, 380 events, 408 plan/create, 452 script-set — không có pause/resume" },
        { "path": "services/product/backend_service/src/backend/api/v1/websockets.py:42-54", "what": "WS chỉ nhận 'interrupt' và 'ping'; lệnh khác bị bỏ qua im lặng (không có nhánh else)" },
        { "path": "services/product/backend_service/src/backend/application/director/state.py:129", "what": "qa_window_open chỉ mutate nội bộ; không có control surface" }
      ],
      "control_matrix": {
        "interrupt": true, "stop": true,
        "hold_resume": false, "skip_topic": false, "next_force_product": false,
        "mute_resume_qa": false, "promotion_confirm": false, "emergency_end": false
      },
      "correction": "GAP-OPERATOR-001: gap LỚN HƠN spec ghi (5/7 nút chưa có), không phải nhỏ hơn.",
      "separate_defect": {
        "id": "REV-AI-007b",
        "claim": "WS control bỏ qua im lặng mọi lệnh không nhận diện được — không lỗi, không phản hồi.",
        "evidence": "api/v1/websockets.py:42-54",
        "impact": "operator bấm nút chưa làm sẽ không biết vì sao không có gì xảy ra"
      }
    },
    {
      "id": "REV-AI-008",
      "title": "Phát video ra platform ngoài",
      "spec_ref": ["BR-TIKTOK-001", "C-MEDIA-001", "GAP-MEDIA-001"],
      "verdict": "MATCHES",
      "code_status": "NOT_APPLICABLE",
      "action": "UPDATE_CROSS_REPO_CONTRACTS",
      "claim": "Egress thuộc repo team. Cờ LIVEKIT_PUBLISH=0 là đúng cho chế độ hiện tại.",
      "evidence": [
        { "path": "services/product/backend_service/src/backend/config.py:454,546", "what": "RENDER_BACKEND mặc định 'cloud_liveavatar'" },
        { "path": "services/product/backend_service/src/backend/application/clients/avatar/liveavatar.py:31-32,80-81", "what": "bên thứ ba trả livekit_url + livekit_client_token" },
        { "path": "services/product/backend_service/src/backend/application/publishing/legacy.py:27", "what": "publish_enabled() cần LIVEKIT_PUBLISH=1 + 3 creds" },
        { "path": "services/product/backend_service/src/backend/config.py:519,602", "what": "livekit_publish mặc định False" }
      ],
      "correction": "C-MEDIA-001 mô tả kiến trúc self-host. Hiện tại là thuê ngoài. Đích là tự host. Spec nên ghi rõ HAI CHẾ ĐỘ.",
      "verified_absent": "không có code egress/rtmp/stream_key/ingest_url trong repo AI"
    },
    {
      "id": "REV-AI-009",
      "title": "Quy trình duyệt nội dung — spec ghi MISSING, thực tế đã xong",
      "spec_ref": ["BR-CONTENT-001", "BR-CONTENT-002", "BR-CONTENT-003", "GAP-CONTENT-001", "DR-CONTENT-001"],
      "verdict": "UNDERSTATES",
      "code_status": "IMPLEMENTED",
      "action": "UPDATE_GAP_REGISTER",
      "severity": "high",
      "claim": "Spec xếp 3 mục là MISSING. Thực tế repo có quy trình hoàn chỉnh, đã nối API, đang chạy.",
      "spec_gap_text": "No complete approved readiness workflow located / No target content treatment model found / complete target workflow/model not located",
      "evidence": [
        { "path": "services/product/backend_service/src/backend/application/script_authoring/gate/rules/", "what": "24 rules / 12 families: CLAIM(5) COVERAGE CTA REPETITION(3) TTS(4) SPEECH_DURATION VN_SPELLING FORMAT STYLE PROFANITY TONE TRANSITION" },
        { "path": "services/product/backend_service/src/backend/application/script_authoring/approval.py:85", "what": "approve_script — từ chối nếu không phải human / không authorized / gate toàn văn chưa pass" },
        { "path": "services/product/backend_service/src/backend/application/script_authoring/approval.py:110-125", "what": "approval hash gắn theo spoken_text, segment hashes, plan_version, rule_set, product_facts_version, promotion_version, persona_brief_version" },
        { "path": "services/product/backend_service/src/backend/application/script_authoring/duration.py:21", "what": "gate_duration_band 50%-150%" },
        { "path": "services/product/backend_service/src/backend/api/v1/scripts.py", "what": "REST API: create/get/patch script_set, put_draft, submit_for_gate, generate/fix/regenerate, approve, batch, SSE progress" },
        { "path": "services/product/backend_service/src/backend/api/v1/sessions.py:452", "what": "PUT /sessions/{id}/script-set — gắn vào phiên live" },
        { "path": "services/product/backend_service/src/backend/bootstrap/lifespan.py:190", "what": "_recover_authoring — phục hồi sau restart" }
      ],
      "three_layer_mapping": {
        "layer_1_approved_knowledge": { "status": "present", "evidence": "application/entity/ + application/evidence/; phân biệt stable vs volatile, giá/tồn kho/khuyến mãi revalidate 30s" },
        "layer_2_content_contract": { "status": "partial", "detail": "24 luật + approval hash có, nhưng gắn cứng vào từng bài viết, chưa tách dùng chung" },
        "layer_3_runtime_utterance": { "status": "present_without_enforcement", "detail": "câu sinh lúc chạy có, nhưng không có lớp kiểm nào chặn trước TTS" }
      },
      "correction": "GAP-CONTENT-001 phải là: tầng 1 và 3 đã có; gap nằm ở tầng 2 (tách ràng buộc dùng chung) + thiếu pre-TTS enforcement cho runtime utterance.",
      "unifying_insight": "Việc tách tầng-2 và việc 'kiểm tra lúc đang nói' là CÙNG MỘT VIỆC: đường Q&A sinh lúc chạy hiện là đường duy nhất phát ra tiếng mà không qua kiểm tra nào.",
      "perf_note": "24 luật là deterministic, không gọi LLM — chi phí thấp, nhưng phải đo trước khi bật."
    },
    {
      "id": "REV-AI-010",
      "title": "Hai bản Director + reducer chạy nền không ai đọc",
      "spec_ref": ["BR-DIRECTOR-001", "BR-DIRECTOR-003", "GAP-DIRECTOR-001", "GAP-DIRECTOR-002"],
      "verdict": "MATCHES",
      "code_status": "PARTIAL",
      "action": "UPDATE_FEATURE_MATRIX",
      "claim": "Bản director/ cũ đang chạy. Bản agentic_director/ + live_runtime/ + evidence/ viết xong nhưng chưa nối. reducer/ chạy nền nhưng đầu ra không ai đọc.",
      "baseline_note": "Tất cả package này ĐÃ tồn tại ở mốc eead8f2 mà spec dùng — xác nhận bằng git ls-tree. Không phải code mới thêm sau.",
      "evidence": [
        { "path": "services/product/backend_service/src/backend/application/director/state.py:28", "what": "Phase: OPENING / SELLING / CLOSING" },
        { "path": "services/product/backend_service/src/backend/application/director/config.py:26,28,42", "what": "360s hard budget, 4 clusters, 75s window" },
        { "path": "services/product/backend_service/src/backend/bootstrap/app_factory.py:53", "what": "reducer = FastReducer(embedder=...)" },
        { "path": "services/product/backend_service/src/backend/bootstrap/lifespan.py:47", "what": "reducer.run_loop() task" },
        { "path": "services/product/backend_service/src/backend/application/platform_events/ingestion.py:309", "what": "notify_new_events — comment đẩy vào reducer" },
        { "path": "services/product/backend_service/src/backend/application/live_runtime/", "what": "0 external importer" }
      ],
      "dead_output": "Consumer duy nhất của reducer output là agentic director — chưa nối. ClusterStore/build_envelope/ClusterEnvelope chỉ còn xuất hiện trong comment (director/coordinator.py:650, director/state.py:124, safety_gate/injection_patterns.py:5).",
      "decision_taken": "Nối tiếp bản Director mới."
    },
    {
      "id": "REV-AI-011",
      "title": "SafetyGate viết xong nhưng không ai gọi",
      "spec_ref": ["BR-SAFETY-001", "BR-MOD-001", "GAP-MOD-001"],
      "verdict": "MATCHES",
      "code_status": "BUILT_NOT_WIRED",
      "action": "TEAM_DECISION",
      "claim": "safety_gate/ đã viết xong và có test, nhưng không file nào import. Đường chạy thật chỉ kiểm comment quá cũ.",
      "evidence": [
        { "path": "services/product/backend_service/src/backend/application/safety_gate/engine.py:33-62", "what": "check(): malformed → replay_flood → spam → extra_checks" },
        { "path": "services/product/backend_service/src/backend/application/safety_gate/decision.py:34-51", "what": "ReasonCode: MALFORMED REPLAY_FLOOD SPAM PROFANITY TOXICITY HARASSMENT UNSAFE_CONTENT PROMPT_INJECTION" },
        { "path": "services/product/backend_service/src/backend/application/safety_gate/injection_patterns.py", "what": "prompt-injection detection" },
        { "path": "services/product/backend_service/src/backend/application/platform_events/ingestion.py:288-292", "what": "_reject_reason — chỉ kiểm staleness" }
      ],
      "repo_own_requirement": {
        "path": "openspec/specs/multi-platform-event-ingress/spec.md:47-52",
        "text": "Safety Gate runs before embedding"
      },
      "incorrect_comment": {
        "path": "services/product/backend_service/src/backend/application/platform_events/ingestion.py:301",
        "text": "The reducer only ever sees accepted comments; SafetyGate runs before this path.",
        "problem": "SafetyGate không chạy ở đâu cả. Comment khẳng định một tính chất an toàn không tồn tại."
      },
      "partial_mitigation": "Nội dung người xem khi vào prompt được bọc BOUNDARY_BEGIN/BOUNDARY_END — application/director/prompts/composer.py."
    },
    {
      "id": "REV-AI-012",
      "title": "Không có gì gửi số phút về hệ thống tính tiền",
      "spec_ref": ["BR-PRICING-001", "BR-PRICING-002", "GAP-BILLING-001", "C-USAGE-001"],
      "verdict": "MATCHES",
      "code_status": "MISSING",
      "action": "UPDATE_CROSS_REPO_CONTRACTS",
      "claim": "Không có sender, không có signed outbound webhook, không có billable clock.",
      "evidence": [
        { "path": "(repo-wide grep)", "what": "ai.usage | usage_reported | report_usage | billable | live_credit | callback_url | usage_webhook | emit_usage → 0 kết quả" },
        { "path": "services/product/backend_service/src/backend/application/clients/llm/openai_compatible.py:135-140", "what": "đọc token usage nhưng chỉ để telemetry nội bộ, không emit" }
      ],
      "counterparty_status": "Theo spec C-USAGE-001, phía repo team đã xong (receiver + ledger + dedup). Chỉ thiếu phía producer.",
      "causal_chain": ["REV-AI-014", "REV-AI-006"]
    },
    {
      "id": "REV-AI-013",
      "title": "Không khôi phục phiên khi crash",
      "spec_ref": ["BR-RELIABILITY-002", "GAP-RECOVERY-001", "DR-RELIABILITY-001"],
      "verdict": "MATCHES",
      "code_status": "MISSING",
      "action": "UPDATE_GAP_REGISTER",
      "claim": "Director state là process-local; không có rehydration cho session, chỉ có cho authoring.",
      "evidence": [
        { "path": "services/product/backend_service/src/backend/application/director/session_context.py:59,225", "what": "DirectorRuntime._sessions dict trong bộ nhớ" },
        { "path": "services/product/backend_service/src/backend/application/director/coordinator.py:144-145,577", "what": "_queues/_tasks; has() = session_id in self._tasks" },
        { "path": "services/product/backend_service/src/backend/bootstrap/lifespan.py:351-357", "what": "shutdown CÓ dọn tài nguyên: orchestrators, coordinator, reducer, livekit, render" },
        { "path": "services/product/backend_service/src/backend/bootstrap/lifespan.py:190", "what": "mẫu recovery có sẵn (chỉ cho authoring)" },
        { "path": "services/product/backend_service/src/backend/application/script_authoring/generation/driver.py:306", "what": "rehydrate FSM từ persisted rows" }
      ],
      "spec_accurate_note": "Shutdown đã dọn tài nguyên đúng. Thiếu chỉ là KHÔNG GHI việc phiên đã kết thúc. Kịch bản xấu còn lại là crash cứng.",
      "options": {
        "B_fail_safe": { "risk": "low", "size": "small", "recommended": true, "rationale": ["B là nền của A — recovery sẽ có lúc thất bại và vẫn cần đường lui", "với livestream, khôi phục sai tệ hơn kết thúc sạch: AI có thể nói lại khuyến mãi đã hứa", "phiên bên thứ ba không khôi phục được"] },
        "A_full_recovery": { "risk": "high", "size": "large", "recommended": false, "rationale": ["chỉ cần cho crash cứng — deploy đã được dọn sẵn", "trạng thái hơi sai nguy hiểm hơn không có"] }
      },
      "shared_work": "Bước 'ghi trạng thái kết thúc phiên' phục vụ REV-AI-006, REV-AI-012 và là nền cho REV-AI-013."
    },
    {
      "id": "REV-AI-014",
      "title": "Trạng thái phiên chỉ là chuỗi, không ai đọc",
      "spec_ref": ["BR-SESSION-002", "GAP-SESSION-001"],
      "verdict": "MATCHES",
      "code_status": "MISSING",
      "action": "UPDATE_GAP_REGISTER",
      "root_finding": true,
      "claim": "Trạng thái phiên là str tự do, chỉ 2 giá trị, một trong hai là code chết, và không ai đọc để rẽ nhánh.",
      "evidence": [
        { "path": "services/product/backend_service/src/backend/application/sessions.py:25", "what": "SessionInfo.status: str = 'created'" },
        { "path": "services/product/backend_service/src/backend/application/sessions.py:33", "what": "new_session() — định nghĩa nhưng KHÔNG nơi nào gọi" },
        { "path": "services/product/backend_service/src/backend/api/v1/sessions.py:51,58", "what": "ghi 'active'" },
        { "path": "services/product/backend_service/src/backend/application/db/postgres_store.py:152", "what": "upsert_session(status: str = 'created')" },
        { "path": "services/product/backend_service/src/backend/application/script_authoring/models.py:58-117", "what": "mẫu đúng: ScriptState(12) GenerationBatchStatus(6) GenerationJobStatus(5)" }
      ],
      "correction": "GAP-SESSION-001: không phải 'enum mapping differ' mà là 'không có enum nào cả, và trường trạng thái không được tiêu thụ'.",
      "root_of": ["REV-AI-006", "REV-AI-012", "REV-AI-013"]
    }
  ],
  "shared_conclusions": {
    "causal_chain": {
      "root": "REV-AI-014",
      "downstream": ["REV-AI-006", "REV-AI-012", "REV-AI-013"],
      "statement": "Không có trạng thái terminal → không đóng được phiên → không có mốc tính số phút; và không phân biệt được phiên sống/chết."
    },
    "single_work_multiple_findings": {
      "work": "Ghi trạng thái kết thúc phiên",
      "serves": ["REV-AI-006", "REV-AI-012", "REV-AI-013"]
    },
    "unified_work": {
      "work": "Tách tầng ràng buộc nội dung dùng chung",
      "serves": ["REV-AI-009"],
      "statement": "Tách 24 luật khỏi bài viết = cho phép kiểm câu trả lời Q&A sinh lúc chạy. Một việc, không phải hai."
    }
  },
  "spec_edits_required": [
    { "target": "04-implementation/GAP_REGISTER.md", "row": "GAP-CONTENT-001", "change": "MISSING → partial: tầng 1 và 3 đã có; gap còn lại là tách tầng-2 + pre-TTS enforcement", "finding": "REV-AI-009" },
    { "target": "04-implementation/GAP_REGISTER.md", "row": "GAP-OPERATOR-001", "change": "correct current-evidence text: pause/resume KHÔNG tồn tại; gap lớn hơn spec ghi", "finding": "REV-AI-007" },
    { "target": "04-implementation/GAP_REGISTER.md", "row": "GAP-DIRECTOR-001", "change": "planner không 'absent' mà có 1/6 tiêu chí (demand pivot)", "finding": "REV-AI-001" },
    { "target": "04-implementation/GAP_REGISTER.md", "row": "GAP-COVERAGE-001", "change": "coverage record có tồn tại nhưng không được tiêu thụ", "finding": "REV-AI-002" },
    { "target": "04-implementation/GAP_REGISTER.md", "row": "GAP-SESSION-001", "change": "không phải enum mapping — không có enum nào, và status không được đọc", "finding": "REV-AI-014" },
    { "target": "04-implementation/FEATURE_MATRIX.md", "row": "Adaptive product scheduler / Semantic facet coverage", "change": "phản ánh trạng thái thật của hai thế hệ Director", "finding": "REV-AI-010" },
    { "target": "04-implementation/CROSS_REPO_CONTRACTS.md", "row": "C-MEDIA-001", "change": "ghi rõ HAI CHẾ ĐỘ: thuê ngoài (hiện tại) và self-host (đích)", "finding": "REV-AI-008" },
    { "target": "04-implementation/VALIDATION_AND_ACCEPTANCE_GATES.md", "row": "VAL-BILLING-001", "change": "xác nhận lại: producer phía AI MISSING, receiver phía team có", "finding": "REV-AI-012" }
  ],
  "team_decisions_required": [
    { "id": "REV-AI-003", "question": "Các con số đang để OPEN chốt bằng gì — đo thực tế, quyết định kinh doanh, hay tạm dùng giá trị trong config.py?" },
    { "id": "REV-AI-005", "question": "4 mục TEAM_CONFIRMED có đúng là team đã xác nhận không, và còn điều gì đã chốt trước đây chưa ghi vào?" },
    { "id": "REV-AI-011", "question": "Nối SafetyGate vào ingestion, hay bỏ? Yêu cầu đã có trong openspec/specs/multi-platform-event-ingress." },
    { "id": "REV-AI-013", "question": "Recovery: chọn dừng an toàn (khuyến nghị) hay khôi phục thật?" },
    { "id": "REV-AI-010", "question": "Đã chốt: đi tiếp trên bản Director mới. Cần xác nhận lộ trình nối." }
  ],
  "no_code_changed": true
}
```

</details>

---

## Phát hiện chi tiết

### REV-AI-001 — Đổi thứ tự bán hàng theo nhu cầu

```
spec_ref    BR-DIRECTOR-003 · DR-DIRECTOR-003 · GAP-DIRECTOR-001
verdict     UNDERSTATES
code_status PARTIAL
action      UPDATE_GAP_REGISTER
```

Code **đã có** cơ chế nhảy sản phẩm theo nhu cầu: đang bán A, nếu có ≥5 câu hỏi về B và B chiếm ≥60% số câu hỏi và B nóng hơn A đủ ngưỡng → nhảy sang B. Lưu chỗ đang bán dở A, bán xong B và khi nhu cầu B giảm dưới 45% → quay lại A **đúng chỗ đã dừng**.

Spec đòi **6 tiêu chí**, code mới có 1:

| Tiêu chí xếp hạng | Code có? |
|---|---|
| Người xem hỏi nhiều | có |
| Ưu tiên merchant đặt trước | không |
| Khuyến mãi sắp tới | không |
| Sản phẩm nào chưa được nói tới | không |
| Tín hiệu mua hàng thật | không |
| Ngân sách còn lại | không |

**Sửa spec:** `GAP-DIRECTOR-001` — không phải *"planner absent"* mà là *"planner có 1/6 tiêu chí"*.

```
application/director/pivot.py
application/director/config.py:59-62
application/director/decision.py:549-587
```

### REV-AI-002 — "Đã nói đủ thì mãi là đủ"

```
spec_ref    BR-DIRECTOR-003 · GAP-COVERAGE-001
verdict     MATCHES
code_status BUILT_NOT_WIRED
action      UPDATE_GAP_REGISTER
```

Code **có** ghi lại ý nào đã nói: so câu vừa nói với danh sách ý chính bằng embedding, ngưỡng 0.75, **chỉ cộng thêm không bao giờ xoá** — đúng tính monotonic mà `BR-DIRECTOR-003` yêu cầu.

Nhưng ghi xong **không ai đọc**. `covered_points` chỉ có 1 nơi ghi và 1 nơi tự đọc lại chính nó để cộng dồn; `decision.py` không có tham chiếu nào tới nó.

| Tình huống | Hiện tại |
|---|---|
| Q&A đã trả lời ý "bảo hành 12 tháng" | phần bán hàng vẫn nói lại y ý đó |
| Đã nói hết các ý của sản phẩm | con trỏ vẫn chạy tiếp, không nhận ra là hết |

**Sửa spec:** `GAP-COVERAGE-001` — coverage record **có tồn tại**; thiếu là (a) source/timestamp, (b) mức PARTIAL/SUFFICIENT, (c) revisit eligibility, (d) **không được tiêu thụ**.

```
application/director/scoring.py:58
application/director/coordinator.py:1074-1087
application/director/state.py:189-194
application/director/decision.py          ← không đọc
```

### REV-AI-003 — Spec để trống toàn bộ con số

```
spec_ref    DECISION_LOG.md#explicitly-unapproved-numeric-decisions
verdict     MATCHES
code_status NOT_APPLICABLE
action      TEAM_DECISION
blocking    true
```

`config.py` chứa khoảng **20 con số** điều khiển hành vi khi live — 360 giây mỗi sản phẩm, 75 giây mở đầu, 5 người xem, 75 giây cửa sổ comment, 4 cụm Q&A, 45 giây ngừng tương tác, pivot 60%/45%/0.15.

Spec có mục riêng cấm suy chúng thành luật sản phẩm. Đây là kỷ luật tốt, nhưng là **điều kiện chặn**.

**Việc cần làm:** chốt bằng đo thực tế · quyết định kinh doanh · hay tạm dùng giá trị hiện tại.

```
application/director/config.py
```

### REV-AI-004 — 19 tài liệu OpenSpec trong repo

```
spec_ref    (không có)
verdict     SILENT
code_status PARTIAL
action      CLARIFY_SPEC
```

Repo có 19 capability spec; 9 mô tả trực tiếp hành vi sản phẩm. Spec sản phẩm **không nhắc tới chúng lần nào**.

Ghi chú: 19 tài liệu này thuộc sở hữu riêng của người review, không phải quyết định của team — nên đây là thông tin để spec biết, không phải "quyết định của team bị thiếu".

Kiểm mức độ nối vào hệ thống — đếm số file **bên ngoài** mỗi package import nó:

| Nhóm | Số nơi dùng | Trạng thái |
|---|---|---|
| `text_chunker` | 13 | đang chạy |
| `entity` | 11 | đang chạy |
| `render` | 10 | đang chạy |
| `director` · `db` | 7 | đang chạy |
| `script_authoring` | 6 | đang chạy |
| `contracts` | 5 | đang chạy |
| `clients` | 4 | đang chạy |
| `publishing` | 3 | đang chạy |
| `platform_events` · `reducer` | 2 | đang chạy |
| `schemas` | 1 | đang chạy |
| `agentic_director` | 2 | chỉ nội bộ cụm |
| `live_runtime` | 0 | không ai dùng |
| `safety_gate` | 0 | không ai dùng |

**Kết luận:** 8/9 tài liệu sản phẩm đã xong và đang chạy. Riêng `agentic-live-director` và `script-qna-speech-arbitration`: code xong nhưng **chưa cắm vào** (xem `REV-AI-010`).

Đáng chú ý: `script-qna-speech-arbitration` ghi **gần như y nguyên** điều `BR-QA-002` yêu cầu.

```
openspec/specs/
openspec/specs/script-qna-speech-arbitration/spec.md:32,41,89-90
```

### REV-AI-005 — "Team đã xác nhận" chỉ có 4 mục

```
spec_ref    TEAM_CONFIRMED_FACTS.md · SOURCE_OF_TRUTH.md
verdict     MATCHES
code_status NOT_APPLICABLE
action      TEAM_DECISION
```

4 mục, **cả 4 đều ghi "historical exact date unavailable"**. Trong spec, mục này xếp ưu tiên số 2 — **cao hơn cả Decision Log** — nhưng không có ngày, không có biên bản.

Đối chiếu code: `TCF-SESSION-001` khớp · `TCF-SCOPE-001` khớp · `TCF-COMMERCIAL-001` khớp · `TCF-IDENTITY-001` không kiểm được bằng code.

**Câu hỏi cho team:** *"Tôi đã xác nhận 4 điều này. Các bạn thì sao, và có điều gì khác đã chốt trước đây mà chưa ghi vào không?"*

### REV-AI-006 — Phiên kết thúc nhưng không được đóng sổ

```
spec_ref    BR-SESSION-002 · BR-PRICING-002 · GAP-RECOVERY-001
verdict     MATCHES
code_status MISSING
action      UPDATE_GAP_REGISTER
```

| Chỗ lưu | Khi dừng phiên |
|---|---|
| Bộ nhớ tạm | bị xoá |
| Postgres | **không xoá, cũng không cập nhật** → mãi ghi `"active"` |

`postgres_store.py` chỉ có `upsert_session` và `get_session`. **Không có lệnh đóng phiên.** Và `upsert_session` chỉ được gọi **đúng một lần, lúc bắt đầu**, với `status="active"`.

Spec chốt "lịch sử tiền không được sửa", nhưng điều đó dựa trên giả định **phiên có một trạng thái kết thúc được ghi lại**.

**Chuỗi nhân quả:** `REV-AI-014` → `REV-AI-006` → `REV-AI-012`.

```
api/v1/sessions.py:272,56
application/db/postgres_store.py
```

### REV-AI-007 — Nút điều khiển: spec ghi thừa

```
spec_ref    BR-SESSION-004 · BR-QA-002 · GAP-OPERATOR-001
verdict     OVERSTATES
code_status MISSING
action      UPDATE_GAP_REGISTER
```

Spec ghi hiện trạng là *"pause/resume/stop/interrupt present"*. **`pause` và `resume` không tồn tại** — không endpoint, không lệnh WS, không hàm nào.

| Nút `BR-SESSION-004` yêu cầu | Có? |
|---|---|
| Ngắt lời | có |
| Dừng phiên | có (dừng thường) |
| Giữ / Cho chạy lại (Hold/Resume) | không |
| Bỏ qua chủ đề | không |
| Chuyển / Ép sang sản phẩm khác | không |
| Tắt / Mở lại Q&A | không |
| Xác nhận khuyến mãi | không |
| Dừng khẩn cấp | không |

**Về Q&A:** `qa_window_open` chỉ mutate được từ bên trong code, không có control surface từ ngoài.

**Sửa spec:** `GAP-OPERATOR-001` — gap **lớn hơn** spec ghi, không phải nhỏ hơn.

#### REV-AI-007b — Kênh điều khiển im lặng với lệnh lạ

```
severity    medium
```

Kênh WS chỉ hiểu 2 lệnh: `interrupt` và `ping`. Lệnh khác **không báo lỗi, không phản hồi** — không có nhánh `else`. Người vận hành bấm nút chưa làm sẽ không biết vì sao không có gì xảy ra.

```
api/v1/sessions.py:31,73,225,250,283,355,380,408,452
api/v1/websockets.py:42-54
application/director/state.py:129
```

### REV-AI-008 — Phát video ra platform ngoài

```
spec_ref    BR-TIKTOK-001 · C-MEDIA-001 · GAP-MEDIA-001
verdict     MATCHES
code_status NOT_APPLICABLE
action      UPDATE_CROSS_REPO_CONTRACTS
```

| Đoạn đường | Trạng thái |
|---|---|
| AI tạo tiếng nói | có |
| Đẩy tiếng vào LiveKit | có code — mặc định tắt |
| Cấp token cho trình duyệt | có |
| Đẩy từ LiveKit ra TikTok | **thuộc repo team** |

**Vì sao tắt mặc định là đúng:** hiện dùng avatar bên thứ ba (`RENDER_BACKEND = "cloud_liveavatar"`). Bên đó dựng LiveKit server và cấp key; repo AI chỉ nhận `livekit_url` + `livekit_client_token` rồi chuyển tiếp. Cờ `LIVEKIT_PUBLISH=0` là **công tắc chế độ**, không phải lỗi.

**Đích là tự host** — khi traffic và chi phí đủ lớn, và model ngày càng nhẹ hơn.

**Sửa spec:** `C-MEDIA-001` nên ghi rõ **hai chế độ**. Ai đọc spec rồi mở code dễ tưởng phần này hỏng.

Đã xác nhận **không có** code `egress` / `rtmp` / `stream_key` / `ingest_url` trong repo AI.

```
application/publishing/legacy.py:27,73
config.py:454,519,602
application/clients/avatar/liveavatar.py:31-32,80-81
```

### REV-AI-009 — Quy trình duyệt nội dung: spec ghi MISSING, thực tế đã xong

```
spec_ref    BR-CONTENT-001..003 · GAP-CONTENT-001 · DR-CONTENT-001
verdict     UNDERSTATES
code_status IMPLEMENTED
action      UPDATE_GAP_REGISTER
severity    high
```

Spec xếp 3 mục là MISSING. Thực tế repo có quy trình hoàn chỉnh, đã nối API, đang chạy.

**24 luật / 12 nhóm:**

| Nhóm | Kiểm gì |
|---|---|
| `CLAIM` (5 luật) | giá, giảm giá, mâu thuẫn, sai sự thật, sai nhận dạng |
| `COVERAGE` | có nói đủ các ý bắt buộc không |
| `CTA` | nhịp kêu gọi mua |
| `REPETITION` (3) | lặp trong câu · giữa đoạn · CTA |
| `TTS` (4) | viết tắt, ký tự điều khiển, markup, số → máy đọc không sai |
| `SPEECH_DURATION` | độ dài đọc từng đoạn và toàn bài (band 50–150%) |
| `VN_SPELLING` | chính tả tiếng Việt |
| `FORMAT` · `STYLE` | dấu câu, khoảng trắng, gạch ngang |
| `PROFANITY` | từ ngữ thô tục |
| `TONE` · `TRANSITION` | giọng điệu, thứ tự chuyển đoạn |

**Điều kiện duyệt** từ chối nếu: không phải người thật · không có quyền · chưa vượt gate toàn văn · gate không phải loại "toàn văn".

**Chữ ký duyệt** gắn theo `spoken_text`, `segment_hashes`, `plan_version`, `rule_set`, `product_facts_version`, **`promotion_version`**, `persona_brief_version`. Một thứ đổi → chữ ký mất hiệu lực.

**Đối chiếu mô hình 3 tầng của `BR-CONTENT-002`:**

| Tầng | Trạng thái |
|---|---|
| 1. Approved Knowledge | **có** — `entity/` + `evidence/`, còn kỹ hơn spec: phân biệt stable/volatile, giá làm mới 30s |
| 2. Approved Content Contract | **có một phần** — 24 luật + approval hash, nhưng gắn cứng vào từng bài viết |
| 3. Runtime Utterance | **có** — nhưng chưa có lớp kiểm nào chặn trước TTS |

**Kết luận quan trọng:** đây không phải "3 tầng hay 1 khối". **Chỗ khác nhau thật chỉ nằm ở tầng 2.**

**Việc cần làm không phải viết lại quy trình**, mà là tách 24 luật ra khỏi bài viết để áp được cho cả câu trả lời Q&A sinh lúc chạy.

**Vì sao ưu tiên đường Q&A:** câu trả lời Q&A sinh ra lúc chạy, không nằm trong bài viết đã duyệt → hiện là **đường duy nhất phát ra tiếng mà không qua kiểm tra nào**. Bài viết soạn sẵn đã qua 24 luật + người duyệt.

**Về độ trễ:** 24 luật là deterministic, không gọi LLM → chi phí thấp. Nhưng phải đo trước khi bật.

**Sửa spec:** `GAP-CONTENT-001` phải là — tầng 1 và 3 đã có; gap nằm ở **tách tầng-2 dùng chung** + **pre-TTS enforcement cho runtime utterance**.

```
application/script_authoring/gate/rules/
application/script_authoring/approval.py:85,110-125
application/script_authoring/duration.py:21
api/v1/scripts.py
api/v1/sessions.py:452
bootstrap/lifespan.py:190
application/entity/ · application/evidence/
```

### REV-AI-010 — Hai bản Director + reducer chạy nền không ai đọc

```
spec_ref    BR-DIRECTOR-001 · BR-DIRECTOR-003 · GAP-DIRECTOR-001,002
verdict     MATCHES
code_status PARTIAL
action      UPDATE_FEATURE_MATRIX
```

| Bản | Trạng thái |
|---|---|
| `director/` (cũ) | **đang chạy thật** — 360s cứng, cửa sổ 75s, 4 cụm Q&A |
| `agentic_director/` + `live_runtime/` + `evidence/` | viết xong, **chưa nối vào đâu** |

**Cả hai bản đã tồn tại ở đúng mốc `eead8f2`** mà spec dùng — xác nhận bằng `git ls-tree`. Nghĩa là spec đọc thiếu, không phải code mới thêm.

**Phát hiện thêm — `reducer/` chạy nền mà đầu ra không ai đọc:** tạo lúc khởi động, chạy vòng lặp nền suốt thời gian, nhận mọi comment đã duyệt, xây cụm câu hỏi theo sản phẩm. Nhưng **đầu ra không ai đọc** — consumer duy nhất là bản Director mới, chưa nối.

→ Một tác vụ nền chạy liên tục, tốn CPU, ăn hết comment, xây cụm — rồi bỏ đó.

**Quyết định:** nối tiếp bản Director mới.

```
application/director/state.py:28 · config.py:26,28,42
bootstrap/app_factory.py:53 · bootstrap/lifespan.py:47
application/platform_events/ingestion.py:309
application/live_runtime/  ← 0 external importer
```

### REV-AI-011 — SafetyGate viết xong nhưng không ai gọi

```
spec_ref    BR-SAFETY-001 · BR-MOD-001 · GAP-MOD-001
verdict     MATCHES
code_status BUILT_NOT_WIRED
action      TEAM_DECISION
```

`safety_gate/` viết khá đầy đủ: 3 kiểm tra chạy sẵn (dữ liệu hỏng, gửi lặp, spam), 5 kiểm tra cắm thêm được (thô tục, độc hại, quấy rối, nội dung nguy hiểm, **câu lệnh gài để lừa AI**). Mỗi lần từ chối trả về một mã lý do.

**Không file nào import nó.**

**Đường chạy thật đang bảo vệ bằng gì:** đúng **một hàng** — kiểm comment có quá cũ không. Chính ghi chú của hàm thừa nhận: *"full SafetyGate is a later cluster"*.

**Ghi chú sai cần sửa** tại `ingestion.py:301`: *"The reducer only ever sees accepted comments; SafetyGate runs before this path"* — **không đúng**, SafetyGate không chạy ở đâu cả. Một ghi chú khẳng định tính chất an toàn không tồn tại thì nguy hiểm hơn không có ghi chú.

**Lớp bảo vệ khác đang hoạt động:** nội dung người xem khi vào prompt được bọc `BOUNDARY_BEGIN`/`BOUNDARY_END`.

**Spec của chính repo đã yêu cầu việc này:** `openspec/specs/multi-platform-event-ingress/spec.md:47-52` — *"Safety Gate runs before embedding"*.

```
application/safety_gate/engine.py:33-62 · decision.py:34-51 · injection_patterns.py
application/platform_events/ingestion.py:288-292,301
application/director/prompts/composer.py
```

### REV-AI-012 — Không có gì gửi số phút về hệ thống tính tiền

```
spec_ref    BR-PRICING-001,002 · GAP-BILLING-001 · C-USAGE-001
verdict     MATCHES
code_status MISSING
action      UPDATE_CROSS_REPO_CONTRACTS
```

Tìm `ai.usage` · `usage_reported` · `report_usage` · `billable` · `live_credit` · `callback_url` · `usage_webhook` · `emit_usage`: **0 kết quả**.

Chữ "usage" chỉ xuất hiện với nghĩa "cách dùng sản phẩm", và đọc số token LLM để ghi log nội bộ (`openai_compatible.py:135-140`, không gửi đi đâu).

| Thiếu | Có? |
|---|---|
| Đếm thời gian AI thực sự lên sóng | không |
| Gửi con số đó sang repo team | không |
| Ký xác thực gói tin gửi đi | không |

**Tin tốt:** theo spec `C-USAGE-001`, **phía repo team đã xong** — có receiver, ledger, dedup. Chỉ thiếu phía producer.

**Chuỗi nhân quả:** `REV-AI-014` → `REV-AI-006` → `REV-AI-012`.

### REV-AI-013 — Không khôi phục phiên khi crash

```
spec_ref    BR-RELIABILITY-002 · GAP-RECOVERY-001 · DR-RELIABILITY-001
verdict     MATCHES
code_status MISSING
action      UPDATE_GAP_REGISTER
```

**Mất sạch khi restart:** đang bán sản phẩm nào, đã nói tới ý nào, con trỏ ở đâu, comment đang chờ, bộ nhớ đệm vector.

**Điểm sáng — lúc tắt hệ thống repo đã dọn khá tốt:** `lifespan.py:351-357` dừng lần lượt pipeline phiên, coordinator, vòng lặp reducer, LiveKit, render backend. Nghĩa là khi deploy, **tài nguyên có được giải phóng**. Cái thiếu chỉ là **không ghi lại việc phiên đã kết thúc**.

Còn đúng một kịch bản xấu: **crash cứng** (bị kill, hết bộ nhớ) — không hàm dọn nào chạy.

**Điểm sáng thứ hai:** phần soạn nội dung **đã có mẫu khôi phục hoàn chỉnh** để học theo.

| | B — Dừng an toàn | A — Khôi phục thật |
|---|---|---|
| Cần xây | quét lúc khởi động + đóng sổ phiên | lưu trạng thái liên tục + nạp lại |
| Rủi ro | thấp | **CAO** |
| Độ lớn | nhỏ | lớn |
| Cứu ca nào | crash cứng | crash cứng |
| Deploy có cần? | không — đã dọn sẵn | không — đã dọn sẵn |

**Ba lý do chọn B trước:**

1. **B là điều kiện của A**, không phải lựa chọn thay thế — khôi phục sẽ có lúc thất bại và vẫn cần đường lui.
2. **Với livestream, khôi phục sai tệ hơn kết thúc sạch** — AI chạy tiếp với trạng thái hơi sai có thể nói lại khuyến mãi đã hứa. Đó là lỗi thương mại, không phải lỗi kỹ thuật.
3. **Phiên bên thứ ba không khôi phục được** — avatar đang thuê ngoài giữ phiên riêng của họ.

**Việc B nhỏ hơn vẻ ngoài:** ghi trạng thái kết thúc khi tắt, và quét phiên mồ côi lúc khởi động. **Một việc, phục vụ `REV-AI-006`, `REV-AI-012` và là nền cho `REV-AI-013`.**

```
application/director/session_context.py:59,225
application/director/coordinator.py:144-145,577
bootstrap/lifespan.py:190,351-357
application/script_authoring/generation/driver.py:306
```

### REV-AI-014 — Trạng thái phiên chỉ là chuỗi, không ai đọc

```
spec_ref    BR-SESSION-002 · GAP-SESSION-001
verdict     MATCHES
code_status MISSING
action      UPDATE_GAP_REGISTER
root        true
```

**Spec yêu cầu** vòng đời đầy đủ, phân biệt rõ `READY` (chưa tính tiền) và `WARMING_UP` (bắt đầu tính tiền).

**Code có:** một chuỗi chữ tự do, đúng 2 giá trị — `"created"` định nghĩa ra nhưng **không bao giờ được dùng** (`new_session()` không nơi nào gọi), và `"active"` là giá trị duy nhất thực sự được ghi.

**Và không ai đọc:** tìm chỗ đọc trạng thái phiên để rẽ nhánh → **không có chỗ nào**. Trường này được ghi ra hai lần rồi không ai dùng; nó chỉ để hiển thị.

**Đối chiếu — repo ĐÃ có mẫu làm đúng:** phần soạn nội dung có vòng đời có kiểu dữ liệu hẳn hoi — `ScriptState` 12 trạng thái, `GenerationBatchStatus` 6, `GenerationJobStatus` 5.

**Đây là điểm GỐC:**

```
REV-AI-014  không có trạng thái terminal
   ├── REV-AI-006  không đóng được phiên
   ├── REV-AI-012  không tính được số phút → không tính được tiền
   └── REV-AI-013  không phát hiện được phiên mồ côi
```

**Sửa spec:** `GAP-SESSION-001` — không phải *"enum mapping differ"* mà là *"không có enum nào cả, và trường trạng thái không được tiêu thụ"*.

```
application/sessions.py:25,33
api/v1/sessions.py:51,58
application/db/postgres_store.py:152
application/script_authoring/models.py:58-117     ← mẫu đúng, có sẵn trong repo
```

---

## Việc nên chốt trước khi lập kế hoạch

| # | Việc | Trạng thái |
|---|---|---|
| 1 | Đi tiếp trên bản Director nào | **đã chốt: bản mới** (`REV-AI-010`) |
| 2 | Các con số để trống chốt bằng gì | cần quyết (`REV-AI-003`) |
| 3 | Rà lại Gap Register của spec | 8 dòng cần sửa — xem `spec_edits_required` |

---

## Phụ lục — bảng tra cứu code

Đường dẫn tính từ `services/product/backend_service/src/backend/`.

| Nội dung | Vị trí |
|---|---|
| Session bị xoá khỏi bộ nhớ tạm khi dừng | `api/v1/sessions.py:272` |
| Ghi phiên vào Postgres (chỉ 1 lần) | `api/v1/sessions.py:56` |
| Postgres store — không có lệnh đóng phiên | `application/db/postgres_store.py` |
| Kênh WS chỉ hiểu `interrupt` + `ping` | `api/v1/websockets.py:42,54` |
| Trạng thái phiên là chuỗi · `new_session()` không ai gọi | `application/sessions.py:25,33` |
| 3 giai đoạn cũ · 360 giây cứng | `application/director/state.py:28` · `application/director/config.py:26` |
| Cửa sổ 75s · 4 cụm Q&A · pivot 60%/45%/5 | `application/director/config.py:28,42,59-62` |
| Logic pivot + quay lại chỗ dừng | `application/director/pivot.py` · `application/director/decision.py:549-587` |
| Ghi coverage (không ai đọc lại) | `application/director/coordinator.py:1074-1087` · `application/director/state.py:189-194` |
| 24 luật kiểm nội dung | `application/script_authoring/gate/rules/` |
| Duyệt bắt buộc người thật · chữ ký theo phụ thuộc | `application/script_authoring/approval.py:85,110-125` |
| Vòng đời có kiểu dữ liệu (mẫu đúng) | `application/script_authoring/models.py:58-117` |
| SafetyGate | `application/safety_gate/engine.py` · `decision.py` |
| Ghi chú sai về SafetyGate | `application/platform_events/ingestion.py:301` |
| `reducer` chạy nền · comment đẩy vào | `bootstrap/lifespan.py:47` · `application/platform_events/ingestion.py:309` |
| Dọn dẹp khi tắt · mẫu khôi phục có sẵn | `bootstrap/lifespan.py:351-357,190` |
| Cờ phát LiveKit · chế độ render mặc định | `application/publishing/legacy.py:27` · `config.py:454,519,602` |
| Bên thứ ba cấp LiveKit key | `application/clients/avatar/liveavatar.py:31-32` |
| Tìm bằng chứng + làm mới giá 30s | `application/evidence/models.py` · `application/evidence/planner.py` |
| Spec repo: Safety Gate trước embed | `openspec/specs/multi-platform-event-ingress/spec.md:47-52` |
| Spec repo: Q&A không cắt lời | `openspec/specs/script-qna-speech-arbitration/spec.md:32,41,89-90` |

---

## Ghi chú cuối

- **Chưa sửa code** theo spec — đúng như yêu cầu của tác giả spec.
- Mọi kết luận đối chiếu trực tiếp với source tại `90415d7`, không dựa vào báo cáo của agent.
- Những chỗ spec ghi thiếu là **bất đồng với spec**, không phải spec sai về nghiệp vụ.
- Tài liệu này là **evidence**, không phải quyết định sản phẩm. Việc cập nhật `DECISION_LOG.md` hay business rules cần con người xác nhận.
