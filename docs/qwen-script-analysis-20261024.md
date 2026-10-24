# Qwen tạo kịch bản — phân tích ngày 24/10/2026

Phạm vi: Runtime tại `316ed46`, nhánh `fix/script-qwen-20261024`. Nguồn: log `023-prep/SCRIPT_GEN_LOG_2026-10-24.txt`, code thật và test offline/fake LLM. Không SSH, gọi Qwen thật, deploy hoặc đổi flag production. Log chỉ có 120 ký tự đầu của ứng viên; không chứa JSON facts, toàn bộ response hay finish_reason. Các kết luận về phần không được ghi lại đều là suy luận.

## 1. CLAIM_FACTUAL và extractive fallback

- **Đã xác nhận:** `gate/rules/commerce_claims.py::check_factual_claims` chỉ xét mệnh đề có `_BENEFIT_SIGNALS`, rồi đòi ít nhất 2 từ nội dung trùng một allowed claim. Tín hiệu gồm cả `bền bỉ`, `thiết kế`, `dễ dàng`, `an toàn`...; giá/thông số có kiểm tra riêng. Đây là heuristic, không phải bộ hiểu ngữ nghĩa hoàn chỉnh.
- Test câu `Chào mọi người, mình xin giới thiệu ngay mẫu áo khoác denim thời trang từ Icon Denim.` **qua ngay trên code cũ**. Riêng `thời trang` không phải trigger. Không thể quy lỗi gate cho tiền tố trong log; mệnh đề bị chặn có thể nằm sau 120 ký tự. Bổ sung log `detail` chứa thông báo và mệnh đề thực sự bị chặn để lần sau xác định được.
- Test `Chất liệu denim dày dặn, chắc chắn bền bỉ.` với nguồn chỉ có `Chất liệu denim.` **bị chặn đúng** vì độ bền chưa được xác nhận. Không có snapshot facts thật để kết luận `dày dặn`/`bền bỉ` của sản phẩm này đã được chủ shop duyệt hay chưa. `dày dặn` riêng lẻ chưa có trigger chuyên biệt trong gate cũ.
- **Lỗi prompt chắc chắn:** `_TRUTH_RULES` mời mở rộng “cảm nhận khi dùng”, “lợi ích suy ra hợp lý”; highlight mời cảm nhận; mục tiêu độ dài mời thêm 1–2 chuyện. Những chỉ dẫn này xung đột với nguồn sự thật hạn chế. Đã bỏ các chỉ dẫn đó, cấm bịa trải nghiệm, thêm guard cho dạng `mình đã mặc thử`/`sờ vào thấy`.
- **Không nới CLAIM_FACTUAL hoặc các gate an toàn.** Các tính từ phong cách vốn không có trigger vẫn qua; không biến độ bền, đặc tính vật liệu, chính sách thành ý kiến tùy ý.
- Log liệt kê **8** unit fallback (1,2,3,5,7,8,9,10), không phải 7. Hai lần gọi cũ dùng chung budget nhưng trần chỉ 2; guard đã tiêu lượt thì gate không còn cơ hội sửa. Nay trần chung tối đa 3, không lồng 3×3.

## 2. Truncated, ký tự Trung và tham số

- `ordered_units.py::check_unit_text`: `truncated` nghĩa là không kết thúc bằng `. ! ? …` (có hỗ trợ dấu đóng ngoặc/ngoặc kép), **không phải bằng chứng provider hết token**; `too_long` khi hơn 900 ký tự sau làm sạch. Không có guard CJK trên code cũ, nên mẫu `ai cũng能找到... bạn nam` bị ghi `address` và có thể để lọt CJK nếu không có lỗi khác.
- Nay phát hiện CJK/Japanese/Korean/ký tự replacement trước salvage; trả `foreign_script`, retry với chính chuỗi ký tự sai. Không xóa ký tự rồi giả định phần còn lại đúng nghĩa. Empty, name_repeat và trải nghiệm cá nhân cũng có hướng sửa riêng.
- Đường `EngineManager.get_llm_fn` → `to_llm_fn` → compatible client: unit ban đầu ít nhất 900 max_tokens, `stop` mặc định rỗng, không có response_format ép JSON. Chưa có bằng chứng stop/custom cấu hình là nguyên nhân thật.
- Lượt sửa dùng ít nhất 1400 max_tokens; lần đầu giữ 900, giới hạn ký tự/thời lượng giữ nguyên, chỉ dùng thêm budget khi trước đó đã thất bại. Deadline, reservation trước dispatch và nguyên tắc không retry chồng request timeout vẫn giữ.
- Thêm log incomplete cho non-stream và stream: finish_reason, số ký tự content/reasoning; non-stream thêm completion_tokens. Không log nội dung reasoning, không dùng reasoning làm lời thoại.

## 3. Coverage, giao hàng và thời lượng

- **Sửa lại giả thuyết ban đầu:** code cũ đã compile số thành chữ, so khớp maximal number runs, giữ từ phủ định/điều kiện, ngày/tháng và size; ngưỡng từ còn lại là 60%. Test nguyên câu `2-3 ngày ... HCM và HN ... 1 ngày` với `hai đến ba ngày ... Hà Nội và TP.HCM ... một ngày` **đã qua code cũ**. Vì log regenerate-part không lưu ứng viên, không thể khẳng định chỉ việc viết số thành chữ gây lỗi coverage đó.
- **Lỗi coverage tái hiện được:** code cũ chấp nhận thay nội thành bằng ngoại thành, thay hai thành phố bằng Đà Nẵng, hoặc tráo thời gian toàn quốc/nội thành. Nay chuẩn hóa HN/Hà Nội và HCM/TP.HCM/Thành phố Hồ Chí Minh, giữ cả hai thành phố và nội/ngoại thành; tách phạm vi để kiểm từng thời gian rồi vẫn kiểm điều kiện/số/từ của cả claim. Không tự coi Sài Gòn là alias mới.
- Câu giao hàng đầy đủ tự nó **qua gate thời lượng 1–60 giây hiện có** trong test; không có xung đột bất khả thi bắt buộc phải bỏ điều kiện. Chưa đủ toàn văn ứng viên để đo chính xác các lần duration-fail trong log. Estimator canonical tính cả số/acronym và dấu câu nên phần thêm câu đệm có thể vượt trần.
- Đã giảm highlight/assurance còn 2–4 câu, mục tiêu ký tự chỉ là gợi ý, ưu tiên điều kiện rồi bỏ câu đệm; nêu rõ cấm CTA ở vai trò không được phép. Giữ nguyên hard-sell/CTA và duration gate, không bỏ claim giao hàng, không tăng trần thời lượng.
- Đã xử lý chỉ dẫn duration repair cũ mời giữ giá dạng số để tăng ước lượng: riêng prompt ordered-units giữ thông tin ước lượng/trần thật, yêu cầu giá bằng chữ, sửa câu đệm thay vì đổi cách viết số. Quy tắc/estimator dùng chung không đổi.

## 4. Gate/guard khác và regenerate-part

- `VN_SPELLING_GI_D` là **warning**, bao gồm cả từ hợp lệ như `da`, `di`; `TTS_ACRONYM` (HCM/HN/TP...) cũng **warning**, không tự làm gate fail. `VN_SPELLING_TONE`: hai nguyên âm mang dấu liên tiếp là error; sai vị trí dấu là warning. Log part 3/10 liệt kê cả warning và error; không thể quy mọi rule được in ra là blocker.
- `address` bắt `bạn nam`, `bạn nữ`, `anh chị`... theo style xưng hô; không phải lỗi factual. `name_repeat` chặn tên đầy đủ ngoài intro/offer khi đã có 3 unit khác dùng tên đó. Đã thêm hướng sửa name_repeat.
- `cta` bắt `đặt hàng`, `chốt đơn`, `nhắn mình`... ngoài phần được phép; hard_sell bắt `đặt ngay`, `không bỏ lỡ`... theo regex hiện hành. Salvage chỉ cắt cả câu vi phạm, rồi kiểm lại toàn bộ guard/coverage, sau đó vẫn qua gate. Không cho model lách bằng bỏ điều kiện.
- PR #120 đã đưa missing claims và gate messages vào retry, giữ bản hiện tại; **nhưng vòng lặp 3 không thể chạy đủ trên PostgreSQL**: cả SQL `writes<2` và CHECK `BETWEEN 1 AND 2` đều giới hạn 2. Đã sửa đồng bộ thành 3, gồm nâng CHECK cũ có điều kiện khi migration chạy. Không có migration nào được áp dụng vào production trong phiên này.
- Hết lượt vẫn giữ toàn bộ bản hiện tại. Lý do tiếng Việt, ví dụ `Phần này chưa đạt vì thiếu: Không phơi trực tiếp ngoài nắng. Giữ bản hiện tại.`, được lưu vào JSON snapshot hiện hữu và đọc lại qua error_message; giữ nguyên error_code và fencing lease.
- Fallback vẫn chỉ lấy facts được phép; revisit thêm câu dẫn khác để không giống nguyên văn toàn phần cũ. `fallback_units` vốn có trong batch snapshot; nay selected `current_version.parts[]` có `needs_regeneration=true`, tồn tại qua refresh.
- **Giới hạn Web:** đã đọc `Livento-Web origin/main`, component MerchantScriptReview/scriptParts chỉ nhận placeholder `<Phần này chưa soạn được...>` và dùng thông báo part repair chung; không tiêu thụ fallback_units/needs_regeneration. Backend đã có cờ nhưng Web/API trung gian cần chuyển tiếp và dùng cờ/thông báo để hiện nhãn cụ thể. Không sửa repo Web ngoài worktree Runtime được giao.

## 5. Natural answer rỗng khi LIVE

- Client cũ gửi `reasoning_effort=none` nguyên dạng, **không gửi enable_thinking=false**. `QWEN_LLM` không có trong tracked tree; các khóa thực tế ở client là LLM_BASE_URL, LLM_MODEL, LLM_CHAT_PATH, LLM_REASONING_EFFORT, LLM_OMIT_PARAMS. Không đọc secret/cấu hình production để đoán override bên ngoài.
- Đã ánh xạ `none` → `enable_thinking=false` **chỉ khi model Qwen và base URL compatible-mode**; transport stream/non-stream dùng cùng serializer. Các model/provider khác giữ tham số cũ. Response `content=null` được chuẩn hóa thành chuỗi rỗng, reasoning_content không bao giờ được phát.
- **Chưa chứng minh nguyên nhân của dòng chars=0:** có thể budget bị reasoning tiêu hết, provider trả empty, hoặc `_NATURAL_SYSTEM` chủ động yêu cầu chuỗi rỗng khi facts không đủ. Log không có response/finish_reason để phân biệt. Thêm telemetry thay vì tự đem reasoning ra nói.
- approved_speech giữ validation/judge và đường chọn fact/deflect hiện hữu. Không thêm call/retry vào đường LIVE; thay đổi retry/token chỉ thuộc authoring trước live.
- Tham chiếu chính thức: [Alibaba Model Studio — deep thinking](https://www.alibabacloud.com/help/en/model-studio/deep-thinking), [OpenAI-compatible Chat](https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions). Tài liệu mô tả boolean tắt hybrid thinking và tách reasoning_content/content; không thay thế bằng chứng response thật của lần live này.

## Kiểm chứng và rủi ro

- Test mới dùng fake LLM/MockTransport, không gọi mạng. Lượt RED đầu: **9 fail, 14 pass** trên code cũ với nhóm hồi quy/client khi đó; xác nhận cả lỗi mới và những giả thuyết không tái hiện (intro, số/chữ).
- WSL bị E_ACCESSDENIED. Dùng Windows Python **3.12.13** sẵn có, thư mục tạm trong workspace; không tuyên bố đã chạy WSL Python 3.11.
- Baseline gốc 316ed46: toàn bộ `tests` theo cấu hình repo (loại integration/contract) có **3620 pass, 3 skip, 1 lỗi packaging do uv cache không được phép**; test packaging đó qua khi chạy lại offline với cache được phép, tương đương **3621 pass, 3 skip**. Không dùng con số 3616 được cung cấp thay cho kết quả thực đo.
- Suite đầy đủ cuối: **3645 pass, 3 skip, 0 fail**, 341,06 giây; tăng 24 pass so với baseline thực đo sau xử lý cache (3621), hoặc 29 so với mốc 3616 được cung cấp. Python/platform khác nên giữ cả số đo baseline và mốc lịch sử để đối chiếu. Log: `023-prep/qwen-verified-full.txt`.
- Bộ authoring/client cuối trên code đã chốt: **685 pass** (`023-prep/qwen-focused-final.txt`), gồm fake LLM trộn lỗi CJK → truncated → thành công ở lượt 3, giữ bản hiện tại khi thiếu điều kiện, serialization lý do qua repository có fencing, cờ fallback qua refresh và sửa duration prompt. `ruff check`, `ruff format --check` cho 11 file Python thay đổi và `git diff --check` đều qua.
- Lệnh full: từ `services/product/backend_service`, dùng Python tại `D:/LiventoWorkspace/wt-fix-rt/services/product/backend_service/.venv/Scripts/python.exe -m pytest tests -q --tb=short --basetemp=D:/LiventoWorkspace/023-prep/pytest-qwen-verified`; biến môi trường **chỉ của tiến trình test**: `UV_CACHE_DIR=C:/Users/ACER/codex-tools/cache/uv`, `UV_OFFLINE=true`, `UV_PYTHON` trỏ cùng interpreter. Không đổi file env/flag sản phẩm.
- Rủi ro: guard/coverage vẫn là heuristic; chưa chứng minh giảm tỷ lệ lỗi Qwen thật. Không nới gate giá, claim, y tế, hard-sell, số đo hoặc thời lượng. Coverage mới chặt hơn có thể từ chối một paraphrase hợp lệ khác; thông báo thiếu ý cho người dùng và fallback vẫn giữ facts. Cần chạy migration schema theo quy trình release để trần PostgreSQL 3 có hiệu lực; integration PostgreSQL chưa được chạy ở máy này.
- Chi phí: pre-live có thể thêm tối đa 1 lượt thất bại/unit so với trước và tăng max_tokens lượt sửa; không tăng max_tokens ban đầu hoặc thêm call live. Chưa đo latency/tokens thật nên không tuyên bố cải thiện định lượng.
- Lần live tiếp theo: lưu số attempt và lý do/đoạn vi phạm, tỷ lệ fallback/needs_regeneration, finish_reason + completion/reasoning token metadata khi provider có; kiểm câu shipping đủ địa danh/phạm vi/thời gian, tỷ lệ empty/CJK, độ dài âm thanh thật, và chi phí/độ trễ P50/P95 trước live và trả lời live riêng.
