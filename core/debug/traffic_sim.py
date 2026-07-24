"""core.debug.traffic_sim — mock viewer traffic simulator for debug mode.

Feeds random mock viewer comments to the Director at a configurable interval,
simulating realistic livestream traffic patterns. The Director clusters +
scores + decides → the avatar speaks. This lets you test the FULL pipeline
(Director → LLM → TTS → avatar) without real viewers.

Traffic modes:
  "random"  — random viewer count + random msg rate each cycle
  "low"     — 2-5 viewers, 0.1-0.3 msg/sec (quiet stream)
  "medium"  — 10-30 viewers, 0.5-1.5 msg/sec (normal stream)
  "high"    — 50-200 viewers, 2.0-5.0 msg/sec (viral stream)
  "ramp"    — starts low, ramps to high over time (simulates growing audience)
"""

from __future__ import annotations

import random
import threading
from typing import Optional

from .mock_data import MOCK_VIEWER_MSGS


class TrafficSimulator:
    """Runs in a background thread, feeding mock comments to the Director."""

    def __init__(
        self,
        director,
        hub,
        session_id: str,
        interval_sec: float = 5.0,
        mode: str = "random",
    ) -> None:
        self.director = director
        self.hub = hub
        self.session_id = session_id
        self.interval_sec = interval_sec
        self.mode = mode
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.msgs_sent = 0
        self.cycles = 0
        self._rng = random.Random(42)  # deterministic for reproducibility

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._running

    def _get_traffic(self) -> tuple[int, float]:
        """Return (viewer_count, msg_rate) for the current mode + cycle."""
        c = self.cycles
        if self.mode == "low":
            return self._rng.randint(2, 5), round(self._rng.uniform(0.1, 0.3), 2)
        if self.mode == "medium":
            return self._rng.randint(10, 30), round(self._rng.uniform(0.5, 1.5), 2)
        if self.mode == "high":
            return self._rng.randint(50, 200), round(self._rng.uniform(2.0, 5.0), 2)
        if self.mode == "ramp":
            # Ramp from low to high over ~50 cycles
            progress = min(c / 50.0, 1.0)
            viewers = int(2 + progress * 198)
            rate = round(0.1 + progress * 4.9, 2)
            return viewers, rate
        # "random" — pick a random mode each cycle
        pick = self._rng.choice(["low", "medium", "high"])
        if pick == "low":
            return self._rng.randint(2, 5), round(self._rng.uniform(0.1, 0.3), 2)
        if pick == "medium":
            return self._rng.randint(10, 30), round(self._rng.uniform(0.5, 1.5), 2)
        return self._rng.randint(50, 200), round(self._rng.uniform(2.0, 5.0), 2)

    def _pick_comments(self, msg_rate: float) -> list[dict]:
        """Pick 1-5 random mock comments per cycle based on msg_rate."""
        # Scale: higher msg_rate → more comments per cycle
        n = max(1, min(int(msg_rate * self.interval_sec), 5))
        chosen = self._rng.sample(MOCK_VIEWER_MSGS, min(n, len(MOCK_VIEWER_MSGS)))
        return [{"text": msg} for msg in chosen]

    def _run_loop(self) -> None:
        import time

        while self._running:
            viewers, msg_rate = self._get_traffic()
            comments = self._pick_comments(msg_rate)

            try:
                result = self.director.ingest(
                    self.session_id, comments,
                    traffic_viewer_count=viewers,
                    traffic_msg_rate=msg_rate,
                )
                self.msgs_sent += len(comments)
                self.cycles += 1

                # Emit the debug cycle event to the frontend via the hub.
                # Use asyncio.run_coroutine_threadsafe because we're in a thread,
                # not the event loop.
                try:
                    import asyncio
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self.hub.emit(self.session_id, {
                                "type": "debug.cycle",
                                "cycle": self.cycles,
                                "viewers": viewers,
                                "msg_rate": msg_rate,
                                "comments_sent": len(comments),
                                "director_action": result.get("action"),
                                "director_phase": result.get("phase"),
                                "spoken": (result.get("spoken") or "")[:80],
                            }),
                            loop,
                        )
                except Exception:
                    pass  # hub emit is best-effort
            except Exception as exc:
                # Director error — log but keep running
                print(f"[debug] director.ingest error: {exc}")

            time.sleep(self.interval_sec)
