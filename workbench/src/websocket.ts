/** WebSocket transport — canonical control channel.
 *
 * - control: /api/v1/ws/control/{session_id}?token=... (lifecycle events)
 */

import type { ControlWsMessage, LifecycleEvent } from "./api_types";

export interface WsDeps {
  backendUrl: string;
  getViewerToken: () => string;
  onLifecycle: (event: LifecycleEvent) => void;
  onStatus: (message: string, tone?: string) => void;
}

export function controlSocketUrl(backend: string, sessionId: string, token: string): string {
  const url = new URL(backend);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `/api/v1/ws/control/${encodeURIComponent(sessionId)}`;
  url.search = token ? `token=${encodeURIComponent(token)}` : "";
  return url.toString();
}

export class ControlSocket {
  private ws: WebSocket | null = null;

  constructor(private deps: WsDeps) {}

  connect(sessionId: string) {
    this.disconnect();
    const url = controlSocketUrl(
      this.deps.backendUrl,
      sessionId,
      this.deps.getViewerToken(),
    );
    const ws = new WebSocket(url);
    this.ws = ws;
    ws.addEventListener("open", () => this.deps.onStatus("Control WebSocket đã kết nối.", "success"));
    ws.addEventListener("message", (event) => {
      try {
        const data = JSON.parse(event.data as string) as ControlWsMessage;
        if ("type" in data) this.deps.onLifecycle(data as LifecycleEvent);
      } catch {
        this.deps.onStatus("Không đọc được WebSocket event.", "danger");
      }
    });
    ws.addEventListener("error", () => this.deps.onStatus("Control WebSocket gặp lỗi kết nối.", "danger"));
    ws.addEventListener("close", () => this.deps.onStatus("Control WebSocket đã đóng.", "warning"));
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  get connected(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }
}