# B0 — Bộ QA chất lượng kịch bản (`scripts/qa/`)

Bộ QA lặp lại được cho chất lượng kịch bản live (nhiệm vụ **B0**, xem
`livento-product-spec/04-implementation/handover-2026-11-13/TASK_B_SCRIPT_QUALITY.md`
mục 1, 6, 7). Gồm hai phần:

- **Phân tích offline thuần** — `script_quality_metrics.py`: không mạng, không
  LLM, không import code `services/` (hằng số Runtime được **chép kèm nguồn**,
  xem dưới). Nhận một kịch bản (danh sách phần + role tùy chọn) + danh sách
  claims đã duyệt + thông tin sản phẩm, trả về report metrics.
- **CLI** — `script_quality_qa.py`: `analyze` (offline trên file JSON đã lưu)
  và `run` (chạy tay, gọi API Runtime thật).

Chỉ **thêm file mới**, không sửa file nào trong `services/`, không đổi hành vi
production.

## Cách dùng

### 1. `analyze` — phân tích offline

```bash
uv run python scripts/qa/script_quality_qa.py analyze --input results.json
# tùy chọn: --report out.json (mặc định <input>_report.json), --floor-s, --ceiling-s
```

Đầu vào `--input` chấp nhận:

- file kết quả của chế độ `run` (schema `script_quality_qa_run/v1`) — phân tích
  từng lượt của từng sản phẩm, kèm trạng thái gate/fallback đã lưu;
- một "script doc" đơn (schema `script_quality_doc/v1` hoặc bất kỳ JSON có
  `sections`):

```json
{
  "product": {"id": "...", "name": "...", "target_floor_s": 180, "target_ceiling_s": 240},
  "claims": ["câu claim đã duyệt 1", "..."],
  "sections": [{"text": "lời đọc của một phần", "role": "intro|highlights|recap|..."}]
}
```

In bảng tóm tắt + chi tiết lỗi, ghi report JSON
(schema `script_quality_qa_report/v1`). Exit code: `0` khi mọi document PASS,
`1` khi có FAIL, `2` lỗi đầu vào.

### 2. `run` — CHẠY THẬT, chỉ khi chạy tay

```bash
export LIVENTO_QA_TOKEN=...            # bearer token, chỉ đọc từ biến môi trường
uv run python scripts/qa/script_quality_qa.py run \
  --base-url http://127.0.0.1:8000 \
  --workspace ZZ-TEST-qa-20261114 \
  --products scripts/qa/fixtures/products.json \
  --rounds 3 --out zz_test_results.json
# tùy chọn: --dry-run (in kế hoạch gọi API, KHÔNG gọi mạng), --only <productId>,
#           --target-duration-s, --poll-interval-s, --poll-timeout-s,
#           --regen-attempts, --regen-trials
```

Luồng cho mỗi sản phẩm × lượt (đọc từ `services/product/backend_service/src/
backend/api/v1/scripts.py`): tạo script-set (`POST /api/v1/script-sets`) →
sinh (`POST .../generate-batch` với `ordered_units`, poll
`GET .../generation-batches/{id}` tới `completed/failed/…`) → đọc phần +
gate/fallback (`GET /api/v1/script-sets/{id}`, trường
`items.{pid}.current_version.parts` và `items.{pid}.gate`) → với phần
`needs_regeneration`, tạo lại (`POST .../segments/{idx}/regenerate`, tối đa
`--regen-attempts` lần) → tính metrics offline ngay và ghi JSON.

**Đo "tạo lại phần" (regen) — hai cơ chế, mỗi lần chạy ghi một bản ghi:**

- *Attempts* (mặc định `--regen-attempts 3`): tạo lại các phần mà gate đánh
  dấu `needs_regeneration`, dừng khi sạch hoặc hết lượt.
- *Trials* (`--regen-trials N`, mặc định `0` = tắt; khuyến nghị `18` cho tiêu
  chí TASK_B mục 6-7 "≥ 18 lần tạo lại/sản phẩm, tỉ lệ ≥ 90%"): sau lượt sinh
  **đầu tiên** hoàn tất của mỗi sản phẩm, tạo lại lần lượt **từng phần theo
  vòng xoay `segment_index`** (không chỉ phần bị flag) đúng N lần liên tiếp,
  dùng cùng endpoint `POST .../segments/{idx}/regenerate` (payload có
  `part_id` khi phần có — `RegenerateReq` ưu tiên định danh ổn định này).

Mỗi lần tạo lại (attempt lẫn trial) ghi một bản ghi trong
`round.regen.attempts[]` / `round.regen_trials.trials[]`:
`segment_index`, `part_id`, `workflow_id`, `status` (trạng thái workflow,
hoặc mã HTTP khi request bị từ chối — khi đó `accepted: false`),
`error_code`/`error_message` (nếu body có), và **sau** lần tạo lại: re-read
set → `gate_state_after` + `gate_error_count_after` (số violation mức
"error"). Token không bao giờ xuất hiện trong bản ghi.

Tóm tắt mỗi sản phẩm (`products[].summary`) có:

- `regen_attempt_success_rate` = completed / (tổng số lần tạo lại, gộp
  attempts + trials) — đo trực tiếp "lần tạo lại có chạy xong không";
- `regen_clean_success_rate` = (completed **và** gate sau lần tạo lại không
  còn violation "error") / tổng — tiệm cận "tạo lại ra phần đạt";
- `regen_attempts_total/completed`, `regen_trials_total/completed`,
  `regen_clean_total` — số liệu thô để tự tính lại;
- `regen_no_part_left_flagged` — **đổi tên** từ `regen_success_all_rounds` cũ
  vì tên cũ gây hiểu nhầm: trường này chỉ nói "không còn phần nào bị flag",
  KHÔNG phản ánh từng lần tạo lại thành công/bại (baseline thật 2026-11-14:
  12 lần tạo lại, 3 completed / 9 failed mà trường cũ vẫn `true` 15/15 lượt).

`analyze` đọc lại các tỉ lệ này từ file run: hai cột `regen` (attempt
success) và `clean` (clean-gate) trong bảng tóm tắt từng lượt, cùng
`summary.regen_attempt_success_rate` / `summary.regen_clean_success_rate`
trong report JSON (tổng across các lượt; `null` khi không có lần tạo lại nào).

Kết quả `run` nạp thẳng lại được bằng `analyze --input`.

**An toàn (bắt buộc):**

- token **chỉ** lấy từ biến môi trường `LIVENTO_QA_TOKEN`, chỉ gửi trong header
  `Authorization`, **không bao giờ** in/ghi ra log hoặc file kết quả
  (dry-run in `<LIVENTO_QA_TOKEN, redacted>`);
- từ chối chạy nếu tên workspace (tiền tố tên script-set) không bắt đầu bằng
  `ZZ-TEST` — dữ liệu QA không bao giờ đè lên khách thật;
- `--dry-run` không phát sinh bất kỳ lưu lượng mạng nào;
- chỉ dùng thư viện chuẩn (`urllib`), không thêm dependency.

Exit code của `run`: `0` thành công, `2` bị guard chặn, `3` chạy xong nhưng có
lỗi API (được ghi lại trong JSON).

## Dữ liệu thử

- `fixtures/products.json` — 5 sản phẩm ZZ-TEST: 3 sản phẩm mẫu đã duyệt
  (Serum Mộc Lạc Dewmist, Áo khoác Hanoé Breeze, Nồi chiên Bếp Nhà Vui
  AirChef — claims tách từ TASK_B mục 6) + 1 sản phẩm 6 ý (Trà gạo lứt Rừng
  Xanh, thương hiệu tự chế) + 1 sản phẩm 20 ý (Máy xay Casa Blend, tự chế).
- `tests/ci/fixtures/script_quality/` — 4 kịch bản nhỏ cho test:
  `good_script.json` (đạt hết), `number_fraud_script.json` (bịa số bằng chữ
  "tám trăm bốn mươi kg" cho claim "0,84 kg"; "0,38 kg" viết bằng số CÓ trong
  claims nên không bị báo oan), `repetition_script.json` (lặp nguyên văn
  main↔recap + mở đầu giống nhau "Mình quay lại với phần…"),
  `size_readings_baseline.json` (câu đọc size trích nguyên văn từ baseline
  thật 2026-11-14: đọc từng chữ số phải khớp khoảng claims, "Bây giờ" không
  bị đọc thành "7 giờ", run không tách được thì chỉ "uncertain").
- Test: `tests/ci/test_script_quality_qa.py` — chạy
  `uv run pytest tests/ci/test_script_quality_qa.py -q`.

## Metrics

| Nhóm | Trường | Nguồn xác định |
| --- | --- | --- |
| Tổng hợp | `totals.chars`, `sections`, `sentences` | đếm trực tiếp |
| Thời lượng | `totals.estimated_duration_s = chars / 16` | hằng số Runtime **chép kèm nguồn**: `_VOICE_CHARS_PER_S = 16` tại `services/product/backend_service/src/backend/application/script_authoring/generation/ordered_units.py:54` (đo live 2026-10-15: 3672 ký tự / 226 s). Sàn/trần mặc định 180/240 s là **tham số** của phân tích (TASK_B mục 6/7/8); so sánh "≥ 70% sàn" theo TASK_B mục 7.1 |
| Bịa số | `numbers.digit_violations`, `written_violations`, `written_uncertain` | mọi số + đơn vị trong lời phải có trong claims. Số viết bằng chữ tiếng Việt được chuẩn hóa sang số trước khi so khớp; **từ chỉ số so khớp CÓ DẤU** ("bảy" là 7, "bây"/"bay" không; "lăm" là 5, "làm" không; "tư" là 4, "từ" không). Đọc **từng chữ số** ("một sáu tám" → 168, "một sáu" → 1m60 qua đệm "160") và khoảng **"đến/tới"** được tách riêng; claims dạng khoảng ("1m65–1m74", "52–58kg") được nhận ra và giá trị nằm trong khoảng được tính là khớp. Vi phạm written phân theo `severity`: `error` (có từ ngữ pháp triệu/nghìn/trăm/mươi/linh/phẩy/phần trăm — đọc dứt khoát) hoặc `uncertain` (run chữ số trần không tách được — không làm FAIL) |
| Từ/cụm bịa | `buzzwords.hits` | danh sách cấu hình được (`DEFAULT_BUZZWORDS`); chỉ báo khi cụm KHÔNG có trong claims |
| Lặp | `repetition.verbatim_main_recap`, `similar_openings`, `quay_lai_openings` | nhận phần recap theo `role`/`kind` ("recap", "revisit", "summary", "tóm tắt", "nhắc lại"); nếu không có role thì coi câu trùng giữa ≥2 phần là lặp; mở đầu giống = 5 từ đầu (sau khi bỏ dấu) trùng nhau |
| Độ phẳng | `flatness.avg_sentence_chars`, `stdev_sentence_chars`, `interaction_ratio`, `max_interaction_gap_s`, `structure_types` | heuristic: câu hỏi / câu mời-mệnh lệnh / câu khẳng định ngắn (<60 ký tự) / câu dài; tương tác = chứa "?" hoặc từ khóa "bình luận", "bạn nào", "chốt ", "comment"; khoảng cách tương tác tính bằng ký tự/16, mục tiêu ≤ 90 s |

Verdict (`issues` + `passed`): FAIL khi thiếu thời lượng (<70% sàn), có số/không
đủ claim (vi phạm mức `error`), có buzzword, lặp nguyên văn main↔recap, trùng
mở đầu, không có tương tác, hoặc khoảng cách tương tác > 90 s. Vi phạm
written-number mức **`uncertain`** (run chữ số trần không tách được) chỉ được
liệt kê trong `numbers.written_uncertain` + `uncertain_count`, in kèm chi tiết
với nhãn "(uncertain)", **không** đưa vào `issues`/`violation_count` và không
làm FAIL.

## Giới hạn (metric nào là heuristic)

1. **Chuẩn hóa số bằng chữ là heuristic ngữ pháp, không phải NLU.** Từ chỉ số
   so khớp **có dấu** (NFC, chuẩn chữ thường): "tám trăm bốn mươi" → 840,
   "năm ba" → 53 (kèm ứng viên 5.3 để khớp claim "Bluetooth 5.3"), "một trăm
   linh một" → 101, "một triệu ba trăm chín mươi nghìn" → 1.390.000, "không
   phẩy tám tư" → 0,84, "hai mươi ba phần trăm" → 23%; đọc từng chữ số "một
   sáu tám" → 168 ("một sáu" → đệm "160" để khớp 1m60). Không xử lý: "rưỡi",
   "nửa", "chục", "hăm lăm", số đọc lệch chuẩn địa phương, và **toàn bộ kịch
   bản viết không dấu** (số bằng chữ không dấu sẽ không được nhận — đánh đổi
   có chủ ý sau vụ "Bây giờ" → "7 giờ": từ đồng âm không dấu ("bay", "bây",
   "làm", "từ") không thể phân biệt với "bảy", "lăm", "tư"). "Năm" cuối span
   sau một số đếm trọn là đơn vị năm ("hai mươi năm" → 20 năm, không phải 25);
   "năm" đơn lẻ giữa các chữ số vẫn là 5. Sai sót chỉ gây báo thừa/thiếu,
   không phá dữ liệu.
2. **Số viết bằng chữ chỉ bị báo** khi: span ≥ 2 từ, hoặc có đơn vị đo "nghiêm"
   (kg/g/ml/đ/%/°C/W/tháng/ngày…), hoặc giá trị ≥ 100 — để tránh báo oan các từ
   đệm "một", "hai" không mang số liệu. Số dạng chữ số **luôn** được kiểm.
   Vi phạm written thiếu từ ngữ pháp (chỉ toàn chữ số trần) được hạ xuống
   `uncertain` vì cách tách run chữ số là suy đoán (thử mọi tách 2–3 chữ số,
   chạy quá 6 chữ số không thử); giá bia có từ triệu/nghìn/trăm vẫn là
   `error` đúng như trước.
3. **Khớp claim là permissive**: số không kèm đơn vị khớp khi giá trị xuất hiện
   trong bất kỳ claim nào (đơn vị lạ/ước lượng "~" được nới). Có thể khớp lỏng
   quá — không thay thế được gate `CLAIM_FACTUAL` của Runtime, chỉ là lớp QA
   nhìn tổng thể.
4. **Recap được nhận qua role/kind**; nếu dữ liệu không có role, mọi câu trùng
   giữa các phần đều được tính là lặp (nhiều bộ phận cố ý nhắc lại giá/CTA có
   thể bị điểm lặp — đọc kèm `similar_openings` để phán).
5. **Độ phẳng**: cấu trúc câu và tương tác nhận bằng từ khóa/sau bỏ dấu — không
   hiểu ngữ nghĩa; `max_interaction_gap_s` tính cả đoạn từ đầu kịch bản đến
   lần tương tác đầu tiên.
6. **Thời lượng ước tính** tính trên số ký tự hiển thị (kể cả khoảng trắng),
   đúng theo mô hình ký tự/giây của Runtime — không thay cho nghe thử TTS
   (TASK_B mục 6 yêu cầu nghe thật để chốt cảm giác tự nhiên).
7. `run` chỉ tổng hợp được những gì API trả về (`parts`, `gate`, fallback,
   workflow status). Tỉ lệ regen được đo **trên từng lần tạo lại thật**:
   `regen_attempt_success_rate` (completed/tổng) và `regen_clean_success_rate`
   (completed + gate sạch error/tổng), gộp attempts (phần bị flag) và trials
   (`--regen-trials N`, xoay vòng mọi `segment_index` ở lượt đầu — đặt `18`
   để đáp ứng tiêu chí TASK_B mục 6-7). `regen_no_part_left_flagged` (tên cũ
   `regen_success_all_rounds`) chỉ là cờ "không còn phần bị flag", không phải
   tỉ lệ thành công. Nếu gate không flag phần nào và không đặt
   `--regen-trials` thì không có lần tạo lại nào được đo (hai tỉ lệ `null`).
   "Tạo lại tay" của chủ shop ngoài công cụ này vẫn không nằm trong phạm vi đo.
