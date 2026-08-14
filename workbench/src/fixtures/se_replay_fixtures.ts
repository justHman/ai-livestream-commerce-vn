/** SE adapter replay fixtures (OpenSpec 16.7).
 *
 * Versioned, self-contained deterministic scenario scripts an SE can replay
 * against POST /api/v1/sessions/{session_id}/events without any backend
 * knowledge: every event_id, viewer_id, source_stream_id, text, and
 * timestamp (relative to t0 seconds) is explicit.
 *
 * Contract: POST body is ALWAYS `{"events": [...]}` (1..100 events); a
 * 1-event request is the same schema as a batch. `occurred_at` values are
 * `t0 + offset` — replace t0 with the actual session start epoch.
 */

export interface SeReplayEvent {
  event_id: string;
  platform: "tiktok" | "shopee" | "facebook" | "youtube";
  source_stream_id: string;
  /** Seconds after t0. */
  offset: number;
  type: "viewer.comment" | "viewer.join" | "viewer.follow" | "viewer.like";
  viewer_id: string;
  display_name: string;
  /** For viewer.comment; omitted (or `{}`) for malformed/other events. */
  text?: string;
  /** True when this event intentionally repeats an earlier event_id (retry). */
  retry_of?: string;
  /** True when the event is intentionally malformed (missing payload.text). */
  malformed?: boolean;
}

export interface SeReplayScenario {
  id: string;
  version: string;
  description: string;
  events: SeReplayEvent[];
}

/** Scenario library — versioned; adding scenarios must bump `version` here
 * and in any snapshot test asserting the exact fixture contents. */
export const SE_REPLAY_FIXTURES_VERSION = "1.0.0";

export const SE_REPLAY_SCENARIOS: SeReplayScenario[] = [
  {
    id: "concurrent-sources",
    version: SE_REPLAY_FIXTURES_VERSION,
    description:
      "Four platforms streaming simultaneously; each event carries its own source_stream_id, viewer ids, and text — the shape an SE integration harness must reproduce.",
    events: [
      { event_id: "ev-0001", platform: "tiktok", source_stream_id: "tiktok-live-1", offset: 1.0, type: "viewer.comment", viewer_id: "v-101", display_name: "Minh", text: "Kem chống nắng giá bao nhiêu shop?" },
      { event_id: "ev-0002", platform: "shopee", source_stream_id: "shopee-live-1", offset: 1.2, type: "viewer.comment", viewer_id: "v-201", display_name: "Lan", text: "Mua 2 có giảm giá không?" },
      { event_id: "ev-0003", platform: "facebook", source_stream_id: "facebook-live-1", offset: 1.4, type: "viewer.join", viewer_id: "v-301", display_name: "Hùng", text: undefined },
      { event_id: "ev-0004", platform: "youtube", source_stream_id: "youtube-live-1", offset: 1.6, type: "viewer.comment", viewer_id: "v-401", display_name: "Trang", text: "Ship bao nhiêu tiền?" },
      { event_id: "ev-0005", platform: "tiktok", source_stream_id: "tiktok-live-1", offset: 2.0, type: "viewer.follow", viewer_id: "v-102", display_name: "Nhi", text: undefined },
    ],
  },
  {
    id: "burst",
    version: SE_REPLAY_FIXTURES_VERSION,
    description:
      "A burst window: many events arrive in one /events request (batch). Backend accepts 1..100 per request; all ids unique.",
    events: [
      { event_id: "ev-1001", platform: "tiktok", source_stream_id: "tiktok-live-1", offset: 10.0, type: "viewer.comment", viewer_id: "v-101", display_name: "Minh", text: "Chốt 1 chai serum shop ơi!" },
      { event_id: "ev-1002", platform: "tiktok", source_stream_id: "tiktok-live-1", offset: 10.01, type: "viewer.comment", viewer_id: "v-102", display_name: "Nhi", text: "Cho em đặt 2 áo thun đen size L" },
      { event_id: "ev-1003", platform: "tiktok", source_stream_id: "tiktok-live-1", offset: 10.02, type: "viewer.comment", viewer_id: "v-103", display_name: "Hoa", text: "Mua 1 kem chống nắng, ship về HCM" },
      { event_id: "ev-1004", platform: "tiktok", source_stream_id: "tiktok-live-1", offset: 10.03, type: "viewer.like", viewer_id: "v-104", display_name: "Khoa", text: undefined },
      { event_id: "ev-1005", platform: "tiktok", source_stream_id: "tiktok-live-1", offset: 10.04, type: "viewer.comment", viewer_id: "v-105", display_name: "Vy", text: "Đặt hàng COD được không shop?" },
    ],
  },
  {
    id: "retry-same-event-id",
    version: SE_REPLAY_FIXTURES_VERSION,
    description:
      "Retry resends the SAME event_id; the backend dedupes by event_id and answers `duplicate` (idempotent) instead of a second acceptance.",
    events: [
      { event_id: "ev-2001", platform: "shopee", source_stream_id: "shopee-live-1", offset: 20.0, type: "viewer.comment", viewer_id: "v-201", display_name: "Lan", text: "Serum vitamin C dùng ban ngày hay ban đêm?" },
      { event_id: "ev-2002", platform: "shopee", source_stream_id: "shopee-live-1", offset: 20.5, type: "viewer.comment", viewer_id: "v-201", display_name: "Lan", text: "Serum vitamin C dùng ban ngày hay ban đêm?", retry_of: "ev-2001" },
      { event_id: "ev-2003", platform: "shopee", source_stream_id: "shopee-live-1", offset: 21.0, type: "viewer.comment", viewer_id: "v-202", display_name: "Tuấn", text: "Kem này có phù hợp da dầu không?" },
    ],
  },
  {
    id: "reordered",
    version: SE_REPLAY_FIXTURES_VERSION,
    description:
      "Delivery reorder: event B arrives before event A even though A occurred earlier. The backend does not assume arrival order.",
    events: [
      { event_id: "ev-3002", platform: "facebook", source_stream_id: "facebook-live-1", offset: 30.2, type: "viewer.comment", viewer_id: "v-301", display_name: "Hùng", text: "Kem chống nắng có để lại vệt trắng không?" },
      { event_id: "ev-3001", platform: "facebook", source_stream_id: "facebook-live-1", offset: 30.1, type: "viewer.comment", viewer_id: "v-302", display_name: "Hà", text: "Áo có size L không? Giá bao nhiêu?" },
      { event_id: "ev-3003", platform: "facebook", source_stream_id: "facebook-live-1", offset: 30.3, type: "viewer.comment", viewer_id: "v-303", display_name: "Bình", text: "Ship về miền Tây bao nhiêu ngày? Phí ship?" },
    ],
  },
  {
    id: "malformed",
    version: SE_REPLAY_FIXTURES_VERSION,
    description:
      "Malformed event: a viewer.comment without payload.text. The backend rejects it with 422 at the HTTP boundary.",
    events: [
      { event_id: "ev-4001", platform: "youtube", source_stream_id: "youtube-live-1", offset: 40.0, type: "viewer.comment", viewer_id: "v-401", display_name: "Trang", text: "Kem SPF50 có chống được UVA không?" },
      { event_id: "ev-4002", platform: "youtube", source_stream_id: "youtube-live-1", offset: 40.5, type: "viewer.comment", viewer_id: "v-402", display_name: "An", text: undefined, malformed: true },
      { event_id: "ev-4003", platform: "youtube", source_stream_id: "youtube-live-1", offset: 41.0, type: "viewer.comment", viewer_id: "v-403", display_name: "Phúc", text: "Kem chống nắng water resistant bao lâu?" },
    ],
  },
  {
    id: "outage-recovery",
    version: SE_REPLAY_FIXTURES_VERSION,
    description:
      "Source outage: shopee-live-1 emits nothing between offset 50.0 and 60.0 (silence), then resumes with fresh events.",
    events: [
      { event_id: "ev-5001", platform: "shopee", source_stream_id: "shopee-live-1", offset: 49.0, type: "viewer.comment", viewer_id: "v-201", display_name: "Lan", text: "Serum có gây kích ứng da nhạy cảm không?" },
      // outage: no shopee events until offset 60.0
      { event_id: "ev-5002", platform: "tiktok", source_stream_id: "tiktok-live-1", offset: 52.0, type: "viewer.comment", viewer_id: "v-101", display_name: "Minh", text: "Áo cotton có bị phai màu không?" },
      { event_id: "ev-5003", platform: "shopee", source_stream_id: "shopee-live-1", offset: 60.0, type: "viewer.comment", viewer_id: "v-202", display_name: "Tuấn", text: "Kem này có chiết xuất gì đặc biệt?" },
      { event_id: "ev-5004", platform: "shopee", source_stream_id: "shopee-live-1", offset: 60.5, type: "viewer.like", viewer_id: "v-203", display_name: "My", text: undefined },
    ],
  },
];

/** Build the canonical POST body for one scenario at a given t0 (epoch sec). */
export function replayRequestBody(
  scenarioId: string,
  t0: number,
): { events: Array<Record<string, unknown>> } {
  const scenario = SE_REPLAY_SCENARIOS.find((s) => s.id === scenarioId);
  if (!scenario) throw new Error(`replay scenario not found: ${scenarioId}`);
  return {
    events: scenario.events.map((e) => {
      const event: Record<string, unknown> = {
        event_id: e.event_id,
        platform: e.platform,
        source_stream_id: e.source_stream_id,
        occurred_at: t0 + e.offset,
        type: e.type,
        viewer: { viewer_id: e.viewer_id, display_name: e.display_name },
        payload: e.type === "viewer.comment" && e.text !== undefined && !e.malformed
          ? { text: e.text }
          : {},
      };
      return event;
    }),
  };
}
