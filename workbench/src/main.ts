/** Workbench — Stage 2 operator console (boot + event wiring).
 *
 * The modules below own responsibilities: api transport, ws transport, state,
 * session actions, resource discovery, diagnostics rendering, livekit join,
 * deterministic simulator. This file only boots and binds events.
 */

import { createApi } from "./api";
import { loadFixtures } from "./fixtures";
import { DEV_TOKENS } from "./dev_tokens";
import { connectLiveKit, disconnectRoom } from "./livekit";
import { renderDiagnostics, renderRuntimeInspectors, type RenderSink } from "./diagnostics";
import type { RuntimeInspectorsSnapshot } from "./api_types";
import { reducer, initialState, type Action, type RootState } from "./state";
import { ControlSocket } from "./websocket";
import { validateSimulatorInput } from "./simulator";
import { EventSimulator, defaultSources } from "./eventSimulator";
import type { SimEmission, SimSourceDefinition, SourcePlatform } from "./eventSimulator";
import type { PlatformEvent } from "./api_types";
import type { ArbiterStateName, LifecycleEvent, ProductEntity } from "./api_types";
import { validateProductCatalog, validateShopLimits, productJson } from "./validation";
import { clearDiagnostics } from "./diagnostics";
import { mountAuthoring } from "./authoringView";
import { mountDataStudio } from "./datastudioView";

import "./styles.css";

const $ = (id: string): HTMLElement => document.getElementById(id) ?? document.body;

const SHOP_PROFILE_PRESETS = new Map(loadFixtures().shop_profiles.map((p) => [p.id, { shop_name: p.shop_name, host_name: p.host_name, address: p.address, phone: p.phone, selling_style: p.selling_style }]));
type ShopKey = "shop_name" | "host_name" | "address" | "phone" | "selling_style";
const SHOP_FIELD_MAP: Record<string, ShopKey> = {
  shopName: "shop_name",
  hostName: "host_name",
  shopAddress: "address",
  shopPhone: "phone",
  sellingStyle: "selling_style",
};

let state: RootState = initialState();
let store: ReturnType<typeof createApi>;
let controlSocket: ControlSocket;
let livekitRoom: import("livekit-client").Room | null = null;
let simulator: EventSimulator | null = null;
let diagnosticTimer: ReturnType<typeof setTimeout> | null = null;
let previewObjectUrl: string | null = null;

const AUTO_DEMO_STATES = ["idle", "verifying", "attaching", "opening", "introducing", "answering", "generating", "synthesizing", "prepared", "playback", "advancing", "pivoting", "resuming", "stopped", "failed"];
const AUTO_STATE_LABELS: Record<string, string> = {
  idle: "đang chờ", verifying: "đang kiểm tra sandbox", attaching: "đang attach cấu hình",
  introducing: "đang giới thiệu sản phẩm", answering: "đang trả lời cụm bình luận",
  generating: "backend đang tạo nội dung", synthesizing: "backend đang tổng hợp giọng nói",
  playback: "avatar đang phát", advancing: "đang chờ quyết định tiếp theo",
  opening: "đang mở đầu livestream", stopped: "đã dừng", pivoting: "đang chuyển sản phẩm", resuming: "đang quay lại checkpoint", failed: "thất bại",
};
const AUTO_DEMO_COMMENT_COUNT = 20;
const LOCAL_DRAFT_KEY = "livento-stage2-draft-v1";
const LOCAL_DRAFT_VERSION = 1;

function dispatch(action: Action): void {
  state = reducer(state, action);
  if (["SHOP_FIELD", "PRODUCTS_SET", "PRODUCT_FIELD", "DRAFT_PATCH"].includes(action.type)) {
    persistLocalDraft();
  }
  render(action);
}

function safeMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Lỗi không xác định";
}

function backendUrl(): string {
  return ($("backend") as HTMLInputElement).value.replace(/\/$/, "");
}

function getViewerToken(): string {
  return ($("apiToken") as HTMLInputElement).value.trim() || DEV_TOKENS.viewerToken;
}

function getAdminToken(): string {
  return ($("adminToken") as HTMLInputElement).value.trim() || DEV_TOKENS.adminToken;
}

function setStatus(id: string, text: string, tone = ""): void {
  const element = $("status-" + id);
  element.textContent = text;
  element.setAttribute("data-tone", tone);
}

function addEvent(message: string, tone = "info"): void {
  dispatch({ type: "EVENT_ADD", value: { at: new Date().toISOString(), message: String(message), tone } });
}

const sink: RenderSink = {
  setText: (id: string, text: string) => {
    $(id).textContent = text;
  },
  setHtml: (id: string, html: string) => {
    $(id).innerHTML = html;
  },
};

function render(_action: Action): void {
  renderSession();
  renderResources();
  renderShop();
  renderProducts();
  renderAutoDemo();
  if (state.diagnostics) {
    const data = state.diagnostics as Parameters<typeof renderDiagnostics>[0];
    renderDiagnostics(data, sink);
    const inspectors = (data as unknown as { runtime_inspectors?: RuntimeInspectorsSnapshot }).runtime_inspectors;
    renderRuntimeInspectors(inspectors ?? null, sink);
  } else {
    clearDiagnostics(sink);
  }
  renderEvents();
}

function renderSession(): void {
  const session = state.session;
  ($("startBtn") as HTMLButtonElement).disabled = Boolean(session.id);
  ($("stopBtn") as HTMLButtonElement).disabled = !session.id;
  ($("attachBtn") as HTMLButtonElement).disabled = !session.id;
  ($("speakBtn") as HTMLButtonElement).disabled = !session.id;
  ($("livekitConnectBtn") as HTMLButtonElement).disabled = !session.livekit;
  setStatus(
    "session",
    session.id
      ? `Trạng thái: đang chạy · session=${session.id} · mode=${session.mode ?? "unknown"} · attach=${session.attached ? "đã attach" : "chưa attach"}`
      : "Trạng thái: chưa có session.",
    session.id ? "success" : "",
  );
  setStatus(
    "livekit",
    livekitRoom ? "Trạng thái: LiveKit đã kết nối." : session.livekit ? "Trạng thái: đã nhận credentials, sẵn sàng kết nối." : "Trạng thái: chưa có LiveKit credentials.",
    livekitRoom ? "success" : "",
  );
}

function replaceOptions(select: HTMLSelectElement, items: Array<{ id: string; label?: string; name?: string; ready?: boolean }>, selected: string, fallbackLabel?: string): void {
  const fragment = document.createDocumentFragment();
  if (fallbackLabel) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = fallbackLabel;
    fragment.appendChild(option);
  }
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = `${item.label || item.name || item.id}${item.ready === false ? " — chưa sẵn sàng" : ""}`;
    option.disabled = item.ready === false;
    fragment.appendChild(option);
  }
  select.replaceChildren(fragment);
  if ([...select.options].some((o) => o.value === selected)) select.value = selected;
}

function renderResources(): void {
  const resources = state.resources;
  const engines = resources.engines as { llm?: { id: string }; tts?: { id: string }; available_llm_presets?: Array<{ id: string }>; available_tts_presets?: Array<{ id: string }>; voices?: Array<{ id: string; name: string }> } | null;
  if (engines) {
    replaceOptions($("llmSelect") as HTMLSelectElement, engines.available_llm_presets ?? [], resources.llmId, undefined);
    replaceOptions($("ttsSelect") as HTMLSelectElement, engines.available_tts_presets ?? [], resources.ttsId, undefined);
    replaceOptions($("voiceSelect") as HTMLSelectElement, engines.voices ?? [], resources.voiceId, undefined);
  }
  replaceOptions($("avatarSelect") as HTMLSelectElement, resources.avatars, resources.avatarId, "Avatar mặc định của backend");
  setStatus("resource", `Trạng thái: ${resources.status}`, engines ? "success" : "");
}

function renderShop(): void {
  const mapping: Record<string, keyof typeof state.draft.shop> = { shopName: "shop_name", hostName: "host_name", shopAddress: "address", shopPhone: "phone", sellingStyle: "selling_style" };
  for (const [id, field] of Object.entries(mapping)) {
    if (document.activeElement !== $(id)) ($(id) as HTMLInputElement).value = state.draft.shop[field] || "";
  }
}

function orderedProducts(): ProductEntity[] {
  return state.draft.productOrder
    .map((id) => state.draft.products.find((p) => p.id === id))
    .filter((p): p is ProductEntity => Boolean(p));
}

function renderProducts(): void {
  const list = $("productList");
  const template = $("productTemplate") as HTMLTemplateElement;
  list.replaceChildren();
  for (const product of orderedProducts()) {
    const node = template.content.firstElementChild?.cloneNode(true) as HTMLElement;
    if (!node) continue;
    node.dataset.productId = product.id;
    node.querySelector(".product-summary")!.textContent = `${product.id || "Chưa có ID"} · ${product.name || "Chưa có tên"}`;
    for (const input of node.querySelectorAll<HTMLInputElement>("[data-field]")) {
      const field = input.dataset.field!;
      const value = (product as unknown as Record<string, unknown>)[field];
      if (field === "selected") input.checked = state.draft.selectedProductIds.includes(product.id);
      else if (input.dataset.type === "boolean") input.checked = Boolean(value);
      else if (input.dataset.type === "array") input.value = Array.isArray(value) ? (value as string[]).join(", ") : "";
      else input.value = (value as string) ?? "";
    }
    list.appendChild(node);
  }
  ($("configErrors") as HTMLElement).textContent = state.draft.errors.join("\n");
}

function renderAutoDemo(): void {
  const demo = state.autoDemo;
  ($("demoStateText") as HTMLElement).textContent = `Trạng thái Auto Demo: ${demo.phase} — ${AUTO_STATE_LABELS[demo.phase] || "không xác định"}.`;
  ($("demoStateText") as HTMLElement).dataset.tone = demo.phase === "failed" ? "danger" : demo.active ? "warning" : "";
  ($("autoDemoBtn") as HTMLButtonElement).disabled = demo.active;
  ($("stopAutoBtn") as HTMLButtonElement).disabled = !demo.active;
  const fragment = document.createDocumentFragment();
  for (const phase of AUTO_DEMO_STATES) {
    const item = document.createElement("span");
    item.className = "demo-state";
    item.textContent = phase;
    if (phase === demo.phase) item.setAttribute("aria-current", "step");
    fragment.appendChild(item);
  }
  ($("demoStates") as HTMLElement).replaceChildren(fragment);
}

function renderEvents(): void {
  const container = $("eventLog");
  const fragment = document.createDocumentFragment();
  for (const event of state.events) {
    const row = document.createElement("div");
    row.className = "event-row";
    const time = document.createElement("time");
    time.dateTime = event.at;
    time.textContent = new Date(event.at).toLocaleTimeString("vi-VN");
    const text = document.createElement("span");
    text.textContent = event.message;
    row.append(time, text);
    fragment.appendChild(row);
  }
  container.replaceChildren(fragment);
}

function persistLocalDraft(): void {
  try {
    const { shop, products, selectedProductIds, productOrder } = state.draft;
    localStorage.setItem(LOCAL_DRAFT_KEY, JSON.stringify({ version: LOCAL_DRAFT_VERSION, draft: { shop, products, selectedProductIds, productOrder } }));
  } catch (error) {
    addEvent(`Không lưu draft local: ${safeMessage(error)}`, "warning");
  }
}

function hydrateLocalDraft(): void {
  const fixtureSet = loadFixtures();
  const products = structuredClone(fixtureSet.products);
  const ids = products.map((p) => p.id);
  const shop = fixtureSet.shop_profiles[0] ?? { shop_name: "", host_name: "", address: "", phone: "", selling_style: "" };
  state.draft.products = products;
  state.draft.selectedProductIds = ids;
  state.draft.productOrder = ids;
  state.draft.jsonText = productJson(products);
  state.draft.shop = { shop_name: shop.shop_name, host_name: shop.host_name, address: shop.address, phone: shop.phone, selling_style: shop.selling_style };
  try {
    const raw = localStorage.getItem(LOCAL_DRAFT_KEY);
    if (raw) {
      const stored = JSON.parse(raw);
      if (stored?.version === LOCAL_DRAFT_VERSION && stored.draft && Array.isArray(stored.draft.products)) {
        state.draft = { ...state.draft, ...stored.draft, shop: { ...state.draft.shop, ...stored.draft.shop } };
      }
    }
  } catch {
    /* keep fixture draft */
  }
}

async function startSession(): Promise<void> {
  try {
    const body = await store.startSession({
      avatar_id: state.resources.avatarId || null,
      is_sandbox: ($("sandboxToggle") as HTMLInputElement).checked,
    });
    const livekit = body.livekit_url && body.livekit_client_token ? { url: body.livekit_url, token: body.livekit_client_token } : null;
    dispatch({ type: "SESSION_SET", value: { id: body.session_id, mode: body.mode, livekit, attached: false } });
    controlSocket.connect(body.session_id);
    if (livekit) {
      const room = await connectLiveKit(livekit.url, livekit.token, {
        onStatus: addEvent,
        videoEl: () => ($("avatarVideo") as HTMLVideoElement),
      });
      livekitRoom = room;
    }
    addEvent(`Session đã start: ${body.session_id}.`, "success");
  } catch (error) {
    addEvent(`Start session thất bại: ${safeMessage(error)}`, "danger");
    dispatch({ type: "AUTO_PHASE", phase: "failed", active: false });
  }
}

async function stopSession(): Promise<void> {
  if (!state.session.id) return;
  try {
    await store.stopSession(state.session.id);
    simulator?.stop();
    controlSocket.disconnect();
    await disconnectRoom(livekitRoom);
    livekitRoom = null;
    ($("avatarVideo") as HTMLVideoElement).srcObject = null;
    dispatch({ type: "SESSION_CLEAR" });
    dispatch({ type: "AUTO_PHASE", phase: "stopped", active: false });
    addEvent("Session đã stop và dọn tài nguyên local.", "success");
  } catch (error) {
    addEvent(`Stop session thất bại; giữ state để có thể thử lại: ${safeMessage(error)}`, "danger");
  }
}

async function handleLifecycleEvent(event: LifecycleEvent): Promise<void> {
  dispatch({ type: "LIFECYCLE_EVENT", value: event });
  addEvent(`WS ${event.type}${event.turn_id ? ` · turn=${event.turn_id}` : ""}${event.state ? ` · state=${event.state}` : ""}`);
  if (event.type === "director.decision" || event.type === "coordinator.speak_started" || event.type === "coordinator.speak_finished") {
    // Build the diagnostics snapshot from the canonical event payload.
    const queue: Record<string, unknown> = {
      received_total: event.received_total ?? 0,
      buffered_comments: event.buffered_comments ?? 0,
      active_comments: event.active_comments ?? 0,
      director_cycles: event.director_cycles ?? 0,
      completed_speeches: event.completed_speeches ?? 0,
      active_decision: event.active_decision ?? 0,
      queued_decisions: event.queued_decisions ?? 0,
      completed_speech_history: event.completed_speech_history ?? [],
      speech_queue: { current_product: event.product_id ? { product_id: event.product_id, name: "" } : null },
    };
    // C15 runtime inspectors: surfaced from lifecycle event fields where the
    // backend channel exists today; the remaining sections are omitted and
    // render placeholders until their backend channels carry the fields.
    const inspectors: RuntimeInspectorsSnapshot = {};
    if (event.selected_cluster?.length) {
      inspectors.envelope = { cluster_id: event.selected_cluster[0] ?? "" };
    }
    if (event.state) {
      // Lifecycle state names overlap the arbiter states; surface verbatim.
      inspectors.arbiter = { state: event.state as ArbiterStateName };
    }
    dispatch({
      type: "DIAGNOSTICS_SET",
      value: {
        clusters: [],
        singleton_clusters: 0,
        actionable_clusters: 0,
        total_clusters: 0,
        embedder_name: "unknown",
        embedder_status: "unknown",
        queue_stats: queue,
        runtime_inspectors: inspectors,
      },
    });
    renderDiagnostics(state.diagnostics as Parameters<typeof renderDiagnostics>[0], sink);
  }
  if (event.state && AUTO_DEMO_STATES.includes(event.state)) {
    dispatch({ type: "AUTO_PHASE", phase: event.state, active: state.autoDemo.active });
  }
}

async function discoverResources(): Promise<void> {
  dispatch({ type: "RESOURCES_SET", value: { status: "Đang discovery..." } });
  try {
    const result = await import("./resources").then((m) =>
      m.discoverResources({
        api: store,
        viewerToken: getViewerToken,
        adminToken: getAdminToken,
      }),
    );
    dispatch({
      type: "RESOURCES_SET",
      value: {
        engines: result.engines,
        avatars: result.avatars,
        llmId: result.llmId,
        ttsId: result.ttsId,
        voiceId: result.voiceId,
        status: result.engines ? `Discovery hoàn tất: LLM=${result.engines.llm.id}, TTS=${result.engines.tts.id}, avatars=${result.avatars.length}.` : "Nhập token để discovery tài nguyên được bảo vệ.",
      },
    });
  } catch (error) {
    dispatch({ type: "RESOURCES_SET", value: { status: `Discovery thất bại: ${safeMessage(error)}` } });
    addEvent(`Discovery thất bại: ${safeMessage(error)}`, "danger");
  }
}

async function loadProtectedResources(): Promise<void> {
  if (getAdminToken()) await discoverResources();
  if (getViewerToken()) await discoverResources();
}

async function speak(): Promise<void> {
  const text = ($("speakText") as HTMLTextAreaElement).value.trim();
  if (!state.session.id || !text) {
    addEvent("Cần session và nội dung manual speech.", "warning");
    return;
  }
  dispatch({ type: "AUTO_PHASE", phase: "generating", active: state.autoDemo.active });
  try {
    await store.say(state.session.id, text, false);
    dispatch({ type: "AUTO_PHASE", phase: "advancing", active: state.autoDemo.active });
    addEvent("Manual speech nguyên văn đã hoàn tất playback.", "success");
  } catch (error) {
    dispatch({ type: "AUTO_PHASE", phase: "failed", active: false });
    addEvent(`Manual speech thất bại: ${safeMessage(error)}`, "danger");
  }
}

async function attachDraft(): Promise<void> {
  if (!state.session.id) {
    addEvent("Hãy start session trước.", "warning");
    return;
  }
  const errors = validateProductCatalog(state.draft.products);
  errors.push(...validateShopLimits(state.draft.shop as unknown as Record<string, unknown>));
  if (state.draft.selectedProductIds.length === 0) errors.push("products: chọn ít nhất một sản phẩm.");
  dispatch({ type: "DRAFT_PATCH", value: { errors } });
  if (errors.length) {
    addEvent(`Không attach: ${errors.length} lỗi cấu hình.`, "danger");
    return;
  }
  dispatch({ type: "AUTO_PHASE", phase: "attaching", active: state.autoDemo.active });
  const selected = new Set(state.draft.selectedProductIds);
  const products = orderedProducts().filter((p) => selected.has(p.id));
  try {
    const body = await store.attach(state.session.id, { shop_profile: state.draft.shop, products });
    dispatch({ type: "SESSION_SET", value: { attached: true } });
    dispatch({ type: "AUTO_PHASE", phase: "advancing", active: state.autoDemo.active });
    addEvent(`Attach thành công: ${body.products ?? products.length} sản phẩm; đầu tiên=${products[0]?.id ?? ""}.`, "success");
    scheduleDiagnostics(0);
  } catch (error) {
    dispatch({ type: "AUTO_PHASE", phase: "failed", active: false });
    addEvent(`Attach thất bại: ${safeMessage(error)}`, "danger");
  }
}

function startAutoDemo(): void {
  if (!state.session.id || !state.session.attached) {
    dispatch({ type: "AUTO_PHASE", phase: "failed", active: false });
    addEvent("Auto Demo yêu cầu Start session và Attach cấu hình trước.", "warning");
    return;
  }
  if (state.autoDemo.active) return;
  const messages = loadFixtures().viewer_messages;
  const issues = validateSimulatorInput(messages);
  if (issues.length) {
    dispatch({ type: "AUTO_PHASE", phase: "failed", active: false });
    addEvent(`Auto Demo fixtures invalid: ${issues.join("; ")}`, "danger");
    return;
  }
  const rate = Math.min(5, Math.max(0.2, Number(($("autoDemoRate") as HTMLInputElement).value) || 0.67));
  const batchMode = ($("autoDemoMode") as HTMLSelectElement).value === "batch";
  // Chế độ gửi: batch = gộp mọi nguồn thành 1 request /events; single = mỗi
  // event một request. Không còn là initial_ingest_mode của backend (19.2).
  const sources = defaultSources(messages);
  for (const source of sources) applySourceControls(source);
  dispatch({ type: "AUTO_PHASE", phase: "advancing", active: true });
  simulator = new EventSimulator(
    sources,
    {
      onEmit: (emission) => {
        appendSourceEvent(emission);
        if (batchMode) queueBatchEmission(emission);
        else void postEvents(emission.emitted.event);
      },
      onError: (error) => addEvent(`Simulator error: ${safeMessage(error)}`, "danger"),
    },
    { seed: 42 },
  );
  simulator.start();
  addEvent(`Auto Demo chạy 4 nguồn mô phỏng (${sources.map((s) => s.platform).join(", ")}) ở tốc độ ${rate} event/s/nguồn.`, "success");
  scheduleDiagnostics(0);
}

function stopAutoDemo(): void {
  simulator?.stop();
  simulator = null;
  flushBatch();
  dispatch({ type: "AUTO_PHASE", phase: "stopped", active: false });
  if (state.session.id) {
    store.interrupt(state.session.id).catch(() => addEvent("Interrupt Auto Demo thất bại.", "danger"));
  }
}

/** Canonical viewer ingress for simulated messages: POST the normalized
 * platform event to /api/v1/sessions/{id}/events via the API client
 * (Decision 22 removed the platform WS + ingest/chat routes). */
function postEvents(event: PlatformEvent): void {
  if (!state.session.id || !state.session.attached) return;
  showRequestPayload({ events: [structuredClone(event)] });
  store
    .postEvents(state.session.id, { events: [event] })
    .then((body) => addEvent(`/events: ${body.accepted} accepted, ${body.duplicate} duplicate, ${body.rejected} rejected.`, body.rejected ? "warning" : "success"))
    .catch((error) => addEvent(`/events bị từ chối: ${safeMessage(error)}`, "warning"));
}

/** Batch gửi: gộp event các nguồn trong 1 cửa sổ nhỏ thành một request. */
const batchWindowMs = 500;
let batchTimer: ReturnType<typeof setTimeout> | null = null;
let batchEvents: PlatformEvent[] = [];

function queueBatchEmission(emission: SimEmission): void {
  batchEvents.push(emission.emitted.event);
  if (batchTimer) return;
  batchTimer = setTimeout(() => {
    batchTimer = null;
    if (!batchEvents.length) return;
    const events = batchEvents.splice(0);
    const body = { events };
    showRequestPayload(body);
    if (!state.session.id || !state.session.attached) return;
    store
      .postEvents(state.session.id, body)
      .then((resp) => addEvent(`/events batch: ${resp.accepted} accepted, ${resp.duplicate} duplicate, ${resp.rejected} rejected.`, resp.rejected ? "warning" : "success"))
      .catch((error) => addEvent(`/events batch bị từ chối: ${safeMessage(error)}`, "warning"));
  }, batchWindowMs);
}

function flushBatch(): void {
  if (batchTimer) {
    clearTimeout(batchTimer);
    batchTimer = null;
  }
  batchEvents = [];
}

function appendSourceEvent(emission: SimEmission): void {
  const feed = $("sourceFeed");
  const row = document.createElement("div");
  row.className = "feed-row";
  const tag = document.createElement("span");
  tag.className = "sim-source-tag";
  tag.textContent = emission.source.platform;
  const text = document.createTextNode(
    `${emission.emitted.source.streamId} · ${emission.emitted.source.displayName}: ${emission.emitted.source.text ?? (emission.emitted.malformed ? "(malformed — thiếu payload.text)" : "")}`,
  );
  row.append(tag, text);
  feed.appendChild(row);
  feed.scrollTop = feed.scrollHeight;
  while (feed.children.length > AUTO_DEMO_COMMENT_COUNT) feed.firstElementChild?.remove();
}

/** Hiển thị JSON chính xác của request /events sắp gửi (16.6). */
function showRequestPayload(body: { events: PlatformEvent[] }): void {
  ($("eventsRequestJson") as HTMLElement).textContent = JSON.stringify(body, null, 2);
}

/** Đọc per-source controls (đã bind ở bindEvents) và áp vào source config. */
function applySourceControls(source: SimSourceDefinition): void {
  const rate = Number(($(`rate-${source.platform}`) as HTMLInputElement).value);
  if (rate > 0) source.config.ratePerSecond = Math.min(5, Math.max(0.2, rate));
  const batch = Number(($(`burst-${source.platform}`) as HTMLInputElement).value);
  if (batch > 0) source.config.batchSize = Math.min(10, Math.floor(batch));
  source.config.jitterProbability = ($(`jitter-${source.platform}`) as HTMLInputElement).checked ? 0.3 : 0;
  source.config.outOfOrder = ($(`ooo-${source.platform}`) as HTMLInputElement).checked;
  source.config.paused = ($(`outage-${source.platform}`) as HTMLInputElement).checked;
}

/** Nút điều khiển per-source khi Auto Demo đang chạy (16.4/16.5). */
function handleSourceControl(platform: string, action: string): void {
  if (!simulator) return;
  const source = simulator.sources.find((s) => s.platform === platform as SourcePlatform);
  if (!source) return;
  if (action === "retry") {
    const emission = simulator.retry(source.streamId);
    if (emission) {
      appendSourceEvent(emission);
      void postEvents(emission.emitted.event);
    }
  } else if (action === "malformed") {
    const emission = simulator.emitMalformedOnce(source.streamId);
    if (emission) {
      appendSourceEvent(emission);
      void postEvents(emission.emitted.event);
    }
  } else if (action === "pause") {
    const paused = ($(`outage-${platform}`) as HTMLInputElement).checked;
    simulator.setPaused(source.streamId, paused);
    addEvent(`Nguồn ${platform} ${paused ? "ngừng (outage)" : "khôi phục"} (${source.streamId}).`, paused ? "warning" : "success");
  }
}

function scheduleDiagnostics(delay = 0): void {
  if (diagnosticTimer) clearTimeout(diagnosticTimer);
  if (!state.session.id || !state.session.attached) return;
  diagnosticTimer = setTimeout(pollDiagnostics, delay);
}

function pollDiagnostics(): void {
  // Diagnostics now render from canonical control-WS lifecycle events only
  // (no /api/v1/debug/* polling). The last event carries the snapshot fields.
  if (!state.diagnostics) return;
  const data = state.diagnostics as Parameters<typeof renderDiagnostics>[0];
  renderDiagnostics(data, sink);
}

function applyRuntimeConfig(): void {
  if (!state.session.id || !state.session.attached) {
    addEvent("Runtime config yêu cầu session đã Attach.", "warning");
    return;
  }
  const payload: Record<string, unknown> = {
    comment_rate: Number(($("autoDemoRate") as HTMLInputElement).value),
    max_qa_clusters_per_window: Number(($("qaMaxClusters") as HTMLInputElement).value),
    qa_window_hard_timeout_sec: Number(($("qaTimeout") as HTMLInputElement).value),
    qa_topic_cooldown_sec: Number(($("qaCooldown") as HTMLInputElement).value),
    answer_cache_variants: Number(($("answerVariants") as HTMLInputElement).value),
    prepared_turn_depth: Number(($("preparedDepth") as HTMLInputElement).value),
    transient_retry_count: Number(($("retryCount") as HTMLInputElement).value),
    demand_pivot_enter_share: Number(($("pivotEnter") as HTMLInputElement).value),
    demand_pivot_exit_share: Number(($("pivotExit") as HTMLInputElement).value),
  };
  store
    .applyRuntimeConfig(state.session.id, payload)
    .then((body) => addEvent(`Runtime config accepted revision=${body.config_revision}.`, "success"))
    .catch((error) => addEvent(`Runtime config bị từ chối: ${safeMessage(error)}`, "danger"));
}

async function verifySandbox(): Promise<void> {
  dispatch({ type: "AUTO_PHASE", phase: "verifying", active: state.autoDemo.active });
  dispatch({ type: "RESOURCES_SET", value: { status: "Đang chạy sandbox verification 3 lớp..." } });
  try {
    const body = await store.sandboxVerify({ avatar_id: state.resources.avatarId || null, speech_text: "Xin chào, đây là phiên kiểm tra Stage 2." });
    dispatch({ type: "VERIFICATION_SET", value: body });
    dispatch({ type: "RESOURCES_SET", value: { status: `Sandbox verification: ${body.ready ? "PASS" : "FAIL"}. ${body.layers.map((l) => `${l.name}=${l.status}(${l.latency_ms}ms)`).join(" · ")}` } });
    addEvent(`Sandbox verification ${body.ready ? "PASS" : "FAIL"}.`, body.ready ? "success" : "danger");
    if (!body.ready) dispatch({ type: "AUTO_PHASE", phase: "failed", active: false });
  } catch (error) {
    dispatch({ type: "VERIFICATION_SET", value: { ready: false, layers: [] } });
    dispatch({ type: "RESOURCES_SET", value: { status: `Sandbox verification thất bại: ${safeMessage(error)}` } });
    dispatch({ type: "AUTO_PHASE", phase: "failed", active: false });
    addEvent(`Sandbox verification thất bại: ${safeMessage(error)}`, "danger");
  }
}

async function loadProtected(): Promise<void> {
  await loadProtectedResources();
}

function replaceProductId(productId: string, nextId: string): void {
  const products = state.draft.products.map((p) => (p.id === productId ? { ...p, id: nextId } : p));
  dispatch({ type: "PRODUCT_ID_CHANGE", productId, nextId, jsonText: productJson(products) });
}

function moveProduct(productId: string, offset: number): void {
  const order = [...state.draft.productOrder];
  const from = order.indexOf(productId);
  const to = from + offset;
  if (from < 0 || to < 0 || to >= order.length) return;
  const fromItem = order[from];
  const toItem = order[to];
  if (fromItem === undefined || toItem === undefined) return;
  order[from] = toItem;
  order[to] = fromItem;
  dispatch({ type: "DRAFT_PATCH", value: { productOrder: order } });
}

function bindEvents(): void {
  ($("healthBtn") as HTMLButtonElement).addEventListener("click", async () => {
    try {
      const body = await store.healthReady();
      addEvent(`Health: ${body.status ?? "ready"} · render=${body.render_backend} · LLM=${body.llm_engine} · TTS=${body.tts_engine}`, body.ok ? "success" : "warning");
    } catch (error) {
      addEvent(`Health thất bại: ${safeMessage(error)}`, "danger");
    }
  });
  ($("discoverBtn") as HTMLButtonElement).addEventListener("click", () => void discoverResources());
  ($("startBtn") as HTMLButtonElement).addEventListener("click", () => void startSession());
  ($("stopBtn") as HTMLButtonElement).addEventListener("click", () => void stopSession());
  ($("livekitConnectBtn") as HTMLButtonElement).addEventListener("click", async () => {
    if (state.session.id && state.session.livekit) {
      const room = await connectLiveKit(state.session.livekit.url, state.session.livekit.token, { onStatus: addEvent, videoEl: () => ($("avatarVideo") as HTMLVideoElement) });
      livekitRoom = room;
      renderSession();
    }
  });
  ($("speakBtn") as HTMLButtonElement).addEventListener("click", () => void speak());
  ($("attachBtn") as HTMLButtonElement).addEventListener("click", () => void attachDraft());
  ($("autoDemoBtn") as HTMLButtonElement).addEventListener("click", startAutoDemo);
  ($("stopAutoBtn") as HTMLButtonElement).addEventListener("click", stopAutoDemo);
  ($("applyRuntimeConfigBtn") as HTMLButtonElement).addEventListener("click", applyRuntimeConfig);
  for (const platform of ["tiktok", "shopee", "facebook", "youtube"] as const) {
    ($(`retry-${platform}`) as HTMLButtonElement).addEventListener("click", () => handleSourceControl(platform, "retry"));
    ($(`malformed-${platform}`) as HTMLButtonElement).addEventListener("click", () => handleSourceControl(platform, "malformed"));
    ($(`outage-${platform}`) as HTMLInputElement).addEventListener("change", () => handleSourceControl(platform, "pause"));
  }
  ($("verifyBtn") as HTMLButtonElement).addEventListener("click", () => void verifySandbox());
  ($("apiToken") as HTMLInputElement).addEventListener("change", () => void loadProtected());
  ($("adminToken") as HTMLInputElement).addEventListener("change", () => void loadProtected());
  ($("avatarSelect") as HTMLSelectElement).addEventListener("change", (event) => dispatch({ type: "RESOURCE_SELECT", field: "avatarId", value: (event.target as HTMLSelectElement).value }));
  ($("llmSelect") as HTMLSelectElement).addEventListener("change", (event) => dispatch({ type: "RESOURCE_SELECT", field: "llmId", value: (event.target as HTMLSelectElement).value }));
  ($("ttsSelect") as HTMLSelectElement).addEventListener("change", (event) => dispatch({ type: "RESOURCE_SELECT", field: "ttsId", value: (event.target as HTMLSelectElement).value }));
  ($("voiceSelect") as HTMLSelectElement).addEventListener("change", (event) => dispatch({ type: "RESOURCE_SELECT", field: "voiceId", value: (event.target as HTMLSelectElement).value }));
  ($("productList") as HTMLElement).addEventListener("click", (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-action]");
    if (!button) return;
    const card = button.closest<HTMLElement>(".product-card");
    if (!card?.dataset.productId) return;
    moveProduct(card.dataset.productId, button.dataset.action === "move-up" ? -1 : 1);
  });
  ($("productList") as HTMLElement).addEventListener("change", (event) => {
    const input = event.target as HTMLInputElement;
    const card = input.closest<HTMLElement>(".product-card");
    if (!card?.dataset.productId) return;
    const field = input.dataset.field;
    if (!field) return;
    const productId = card.dataset.productId;
    if (field === "selected") {
      const selected = new Set(state.draft.selectedProductIds);
      if (input.checked) selected.add(productId);
      else selected.delete(productId);
      dispatch({ type: "DRAFT_PATCH", value: { selectedProductIds: [...selected], errors: [] } });
      return;
    }
    if (field === "id") {
      replaceProductId(productId, input.value);
      return;
    }
    let value: unknown = input.value;
    if (input.dataset.type === "integer") value = input.value === "" ? null : Number(input.value);
    else if (input.dataset.type === "boolean") value = input.checked;
    else if (input.dataset.type === "array") value = input.value.split(",").map((s) => s.trim()).filter(Boolean);
    const products = state.draft.products.map((p) => (p.id === productId ? { ...p, [field]: value } : p));
    dispatch({ type: "PRODUCTS_SET", value: { products, jsonText: productJson(products), jsonDirty: false, errors: [] } });
  });
  ($("shopProfileSelect") as HTMLSelectElement).addEventListener("change", (event) => {
    const value = (event.target as HTMLSelectElement).value;
    const preset = SHOP_PROFILE_PRESETS.get(value);
    if (preset) {
      dispatch({ type: "DRAFT_PATCH", value: { shop: { ...preset } } });
    } else {
      ($("shopProfileSelect") as HTMLSelectElement).value = "custom";
    }
  });
  for (const [id, field] of Object.entries(SHOP_FIELD_MAP)) {
    ($(id) as HTMLInputElement).addEventListener("input", (event) => {
      ($("shopProfileSelect") as HTMLSelectElement).value = "custom";
      dispatch({ type: "SHOP_FIELD", field, value: (event.target as HTMLInputElement).value });
    });
  }
  ($("applyLlmBtn") as HTMLButtonElement).addEventListener("click", () => void applyEngine("llm"));
  ($("applyTtsBtn") as HTMLButtonElement).addEventListener("click", () => void applyEngine("tts"));
  ($("previewBtn") as HTMLButtonElement).addEventListener("click", () => void previewVoice());
  ($("addProductBtn") as HTMLButtonElement).addEventListener("click", () => {
    let number = state.draft.products.length + 1;
    let id = `NEW-${number}`;
    while (state.draft.products.some((p) => p.id === id)) {
      number += 1;
      id = `NEW-${number}`;
    }
    const products = [...state.draft.products, { ...emptyProduct(id, `Sản phẩm ${number}`) }];
    dispatch({
      type: "PRODUCTS_SET",
      value: { products, selectedProductIds: [...state.draft.selectedProductIds, id], productOrder: [...state.draft.productOrder, id], jsonText: productJson(products), jsonDirty: false, errors: [] },
    });
  });
  ($("applyJsonBtn") as HTMLButtonElement).addEventListener("click", () => {
    const text = ($("productsJson") as HTMLTextAreaElement).value;
    let products: ProductEntity[];
    try {
      products = JSON.parse(text) as ProductEntity[];
    } catch (error) {
      dispatch({ type: "DRAFT_PATCH", value: { jsonText: text, jsonDirty: true, errors: [`JSON: ${safeMessage(error)}`] } });
      return;
    }
    const errors = validateProductCatalog(products);
    if (errors.length) {
      dispatch({ type: "DRAFT_PATCH", value: { jsonText: text, jsonDirty: true, errors } });
      return;
    }
    const ids = products.map((p) => p.id);
    dispatch({ type: "PRODUCTS_SET", value: { products, selectedProductIds: ids, productOrder: ids, jsonText: productJson(products), jsonDirty: false, errors: [] } });
    addEvent(`Advanced JSON hợp lệ và đã áp dụng ${products.length} sản phẩm.`, "success");
  });
  ($("productsJson") as HTMLTextAreaElement).addEventListener("input", (event) =>
    dispatch({ type: "DRAFT_PATCH", value: { jsonText: (event.target as HTMLTextAreaElement).value, jsonDirty: true, errors: ["Advanced JSON chưa được validate; product form đang khóa để bảo toàn draft hợp lệ."] } }),
  );
  window.addEventListener("pagehide", () => {
    if (state.session.id) {
      // keepalive: unload may cancel the request mid-flight otherwise, orphaning the session
      fetch(`/api/v1/sessions/${encodeURIComponent(state.session.id)}/stop`, {
        method: "POST",
        headers: store.viewerHeaders(),
        keepalive: true,
      }).catch(() => undefined);
    }
  });
}

async function applyEngine(kind: "llm" | "tts"): Promise<void> {
  const engines = state.resources.engines as {
    llm: { id: string };
    tts: { id: string };
    available_llm_presets?: Array<{ id: string; label?: string }>;
    available_tts_presets?: Array<{ id: string; label?: string }>;
  } | null;
  if (!engines) {
    addEvent("Hãy discovery trước khi nạp engine.", "warning");
    return;
  }
  const presets = kind === "llm" ? engines.available_llm_presets ?? [] : engines.available_tts_presets ?? [];
  const id = kind === "llm" ? state.resources.llmId : state.resources.ttsId;
  const preset = presets.find((p) => p.id === id);
  if (!preset) {
    addEvent(`Không tìm thấy ${kind} đã chọn.`, "danger");
    return;
  }
  const payload = await import("./resources").then((m) =>
    m.enginePayload({ ...preset, engine: preset.id.split("-")[0] ?? "" } as import("./api_types").EnginePreset, kind),
  );
  try {
    await store.applyEngine(kind, payload);
    addEvent(`Đã nạp ${kind.toUpperCase()} ${preset.label ?? preset.id}.`, "success");
    await discoverResources();
  } catch (error) {
    dispatch({ type: "RESOURCES_SET", value: { status: `Nạp ${kind} thất bại: ${safeMessage(error)}` } });
    addEvent(`Nạp ${kind} thất bại: ${safeMessage(error)}`, "danger");
  }
}

async function previewVoice(): Promise<void> {
  const engines = state.resources.engines as { tts: { id: string } } | null;
  if (!engines) {
    addEvent("Hãy discovery TTS trước.", "warning");
    return;
  }
  try {
    const response = await store.previewTts(
      ($("previewText") as HTMLInputElement).value.trim(),
      state.resources.ttsId,
      state.resources.voiceId,
    );
    const blob = await response.blob();
    if (previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
    previewObjectUrl = URL.createObjectURL(blob);
    ($("previewAudio") as HTMLAudioElement).src = previewObjectUrl;
    addEvent(`Preview TTS ${response.headers.get("X-TTS-Id") || state.resources.ttsId} thành công.`, "success");
  } catch (error) {
    addEvent(`Preview TTS thất bại: ${safeMessage(error)}`, "danger");
  }
}

function emptyProduct(id: string, name: string): ProductEntity {
  const colors: string[] = [];
  const sizes: string[] = [];
  const features: string[] = [];
  return {
    id, name, description: "", price: null, original_price: null, promotion: "",
    colors, sizes, material: "", shipping: "", warranty: "", in_stock: true,
    stock_total: null, ref_image: "", features,
  };
}

function boot(): void {
  store = createApi({
    backendUrl: backendUrl(),
    viewerToken: getViewerToken,
    adminToken: getAdminToken,
  });
  controlSocket = new ControlSocket({
    backendUrl: backendUrl(),
    getViewerToken,
    onLifecycle: (event) => void handleLifecycleEvent(event),
    onStatus: addEvent,
  });
  hydrateLocalDraft();
  mountAuthoring({
    backendUrl: () => backendUrl(),
    adminToken: getAdminToken,
    api: store,
    onEvent: addEvent,
  });
  mountDataStudio({
    backendUrl: () => backendUrl(),
    viewerToken: getViewerToken,
    api: store,
    onEvent: addEvent,
  });
  // Prefill dev token fixtures into page-memory form state (never persisted).
  ($("apiToken") as HTMLInputElement).value = DEV_TOKENS.viewerToken;
  ($("adminToken") as HTMLInputElement).value = DEV_TOKENS.adminToken;
  bindEvents();
  render({ type: "DRAFT_PATCH", value: {} });
  addEvent("Stage 2 operator console đã sẵn sàng.");
  void loadProtected();
}

document.addEventListener("DOMContentLoaded", boot);