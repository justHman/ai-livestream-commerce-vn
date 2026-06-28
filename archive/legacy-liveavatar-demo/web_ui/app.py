"""Gradio App — unified LiveAvatar demo with embedded WebRTC viewer.

Single-page UI with:
- Left panel: controls + chat (auto-mock loop)
- Center panel: live video preview (auto-refresh) + embedded WebRTC viewer
- Right panel: dashboard + stream log

When Start Stream is clicked, the pipeline runs continuously:
  Mock viewer messages loop → LLM → TTS → Audio encode → DiT + anti-drift → VAE decode → frames
  Video preview and dashboard auto-update every 0.5s.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

import gradio as gr
import numpy as np

from pipelines.orchestrator import BlockResult, StreamConfig, StreamingOrchestrator
from pipelines.streaming_output import StreamingOutput
from signaling.webrtc_track import FrameQueue


# ── Mock viewer messages for auto-loop ──────────────────────────────

MOCK_VIEWER_MESSAGES = [
    "Xin chào!",
    "Giá kem chống nắng bao nhiêu?",
    "Sản phẩm serum có khuyến mãi không?",
    "Hello mọi người!",
    "Cho mình hỏi mặt nạ Hada Labo",
    "Có sale gì hôm nay không?",
    "Sữa rửa mặt Senka dùng có tốt không?",
    "Mình muốn mua kem chống nắng",
    "Deal hôm nay là gì vậy?",
    "Tạm biệt nha!",
    "Serum vitamin C dùng cho da nhạy cảm không?",
    "Mua 2 được giảm không shop?",
    "Cho mình xem sản phẩm P003",
    "Có freeship không ạ?",
    "Mình follow kênh rồi nha!",
]


class LiveAvatarApp:
    """Gradio-based control panel for the LiveAvatar demo."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        device: str = "cpu",
        signaling_port: int = 8000,
        gradio_port: int = 7860,
    ) -> None:
        self.device = device
        self.signaling_port = signaling_port
        self.gradio_port = gradio_port

        # Load config
        if config_path and config_path.exists():
            self.orchestrator = StreamingOrchestrator.from_yaml(
                config_path, device=device
            )
        else:
            self.config = StreamConfig()
            self.orchestrator = StreamingOrchestrator(
                config=self.config, device=device
            )

        # Force Wav2Vec2 into mock mode (avoid CPU crash on some systems)
        self.orchestrator.audio_encoder._model = None

        # Frame queue for WebRTC track
        self.frame_queue = FrameQueue(maxsize=30)

        # State
        self._viewer_messages: queue.Queue = queue.Queue()
        self._latest_frame: Optional[np.ndarray] = None
        self._placeholder = self._generate_placeholder_image()
        self._block_results: list[BlockResult] = []
        self._pipeline_thread: Optional[threading.Thread] = None
        self._running = False
        self._mock_msg_idx = 0
        self._stream_log: list[str] = []
        self._last_response_text: str = ""

    @staticmethod
    def _generate_placeholder_image() -> np.ndarray:
        """Generate a default reference image (avatar silhouette)."""
        from PIL import Image, ImageDraw, ImageFont

        h, w = 400, 720
        img = Image.new("RGB", (w, h), (35, 30, 50))
        draw = ImageDraw.Draw(img)

        # Face
        cy, cx = int(h * 0.42), w // 2
        ry, rx = int(h * 0.22), int(w * 0.16)
        draw.ellipse(
            [cx - rx, cy - ry, cx + rx, cy + ry],
            fill=(180, 150, 130),
        )

        # Eyes
        for off in [-int(rx * 0.35), int(rx * 0.35)]:
            ex, ey = cx + off, cy - int(ry * 0.25)
            draw.ellipse([ex - 6, ey - 4, ex + 6, ey + 4], fill=(30, 30, 30))

        # Mouth
        draw.ellipse(
            [cx - int(rx * 0.3), cy + int(ry * 0.35), cx + int(rx * 0.3), cy + int(ry * 0.55)],
            fill=(180, 80, 80),
        )

        # Text
        try:
            font = ImageFont.truetype("arial.ttf", 16)
        except (OSError, IOError):
            font = ImageFont.load_default()
        draw.text((14, 14), "LiveAvatar Demo", fill=(200, 200, 255), font=font)
        draw.text((14, 36), "Click ▶ Start Stream", fill=(150, 150, 200), font=font)

        return np.array(img, dtype=np.uint8)

    # ------------------------------------------------------------------
    # Pipeline thread
    # ------------------------------------------------------------------

    def _auto_mock_message(self) -> str:
        """Cycle through mock viewer messages automatically."""
        msg = MOCK_VIEWER_MESSAGES[self._mock_msg_idx % len(MOCK_VIEWER_MESSAGES)]
        self._mock_msg_idx += 1
        return msg

    def _run_pipeline(self, ref_image_path: str) -> None:
        """Run the streaming pipeline in a background thread.

        Auto-generates mock viewer messages in a continuous loop.
        Also picks up any manually-sent messages from the queue.
        """
        try:
            self.orchestrator.start_stream(Path(ref_image_path))
            self._stream_log.append("[t=0s] ▶ Stream started — auto-mocking messages")

            def get_next_message():
                # Check for user-sent messages first (priority)
                try:
                    msg = self._viewer_messages.get(timeout=2.0)
                    self._stream_log.append(f"[user] {msg[:60]}")
                    return msg
                except queue.Empty:
                    # Auto-mock cycle — continuous loop
                    msg = self._auto_mock_message()
                    self._stream_log.append(f"[mock] {msg[:60]}")
                    return msg

            for block in self.orchestrator.generate_continuous(get_next_message):
                if not self._running:
                    break

                self._block_results.append(block)
                self._last_response_text = block.audio_text

                # Push frames to WebRTC queue
                for frame in block.frames:
                    self.frame_queue.put_frame(frame, {
                        "block_idx": block.block_idx,
                        "text": block.audio_text,
                    })
                    self._latest_frame = frame

                # Keep log bounded
                if len(self._stream_log) > 100:
                    self._stream_log = self._stream_log[-50:]

            self._stream_log.append("[stopped] ■ Stream ended normally")
        except Exception as exc:
            self._running = False
            err_msg = f"[ERROR] ■ Pipeline crashed: {type(exc).__name__}: {exc}"
            self._stream_log.append(err_msg)
            self._stream_log.append(traceback.format_exc()[-500:])

    # ------------------------------------------------------------------
    # Gradio callbacks
    # ------------------------------------------------------------------

    def on_start_stream(
        self,
        ref_image,
        enable_history_corrupt: bool,
        enable_aas: bool,
        enable_rolling_rope: bool,
    ) -> tuple:
        """Start the streaming pipeline."""
        if self._running:
            return (
                "▶ Already running",
                self._latest_frame if self._latest_frame is not None else self._placeholder,
                self._format_dashboard(),
                self._format_log(),
                self._format_response_text(),
            )

        # Update anti-drift settings
        self.orchestrator.config.enable_history_corrupt = enable_history_corrupt
        self.orchestrator.config.enable_aas = enable_aas
        self.orchestrator.config.enable_rolling_rope = enable_rolling_rope
        self.orchestrator.generator.config.enable_history_corrupt = enable_history_corrupt
        self.orchestrator.generator.config.enable_aas = enable_aas
        self.orchestrator.generator.config.enable_rolling_rope = enable_rolling_rope

        # Handle reference image (use placeholder if none uploaded)
        import tempfile

        ref_dir = Path(tempfile.mkdtemp(prefix="liveavatar_ref_"))
        ref_path = ref_dir / "ref_image.png"

        if ref_image is not None:
            if isinstance(ref_image, dict):
                from PIL import Image
                img = Image.open(ref_image["path"])
                img.save(str(ref_path))
            elif isinstance(ref_image, str):
                ref_path = Path(ref_image)
            else:
                from PIL import Image
                Image.fromarray(ref_image).save(str(ref_path))
        else:
            from PIL import Image
            Image.fromarray(self._placeholder).save(str(ref_path))

        # Reset state
        self._block_results.clear()
        self._stream_log.clear()
        self._mock_msg_idx = 0
        self._last_response_text = ""

        # Start pipeline thread
        self._running = True
        self._pipeline_thread = threading.Thread(
            target=self._run_pipeline,
            args=(str(ref_path),),
            daemon=True,
        )
        self._pipeline_thread.start()

        return (
            "▶ Running — auto-mocking messages",
            self._placeholder,
            self._format_dashboard(),
            self._format_log(),
            "",
        )

    def on_stop_stream(self) -> tuple:
        """Stop the streaming pipeline."""
        self._running = False
        self.orchestrator.stop_stream()
        return (
            "■ Stopped",
            self._latest_frame if self._latest_frame is not None else self._placeholder,
            self._format_dashboard(),
            self._format_log(),
            self._format_response_text(),
        )

    def on_send_message(self, message: str) -> tuple:
        """Send a viewer message to the pipeline."""
        if message.strip():
            self._viewer_messages.put(message.strip())
            return f"✓ Injected: {message.strip()}", self._format_log()
        return "Empty message", self._format_log()

    def on_auto_refresh(self) -> tuple:
        """Called by Gradio Timer to auto-update preview + dashboard."""
        frame = self._latest_frame if self._latest_frame is not None else self._placeholder
        dashboard = self._format_dashboard()
        log = self._format_log()
        response = self._format_response_text()
        status = "▶ Running" if self._running else "■ Stopped"
        return frame, dashboard, log, response, status

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def _format_dashboard(self) -> str:
        """Format pipeline status as a dashboard string."""
        status = self.orchestrator.get_status()
        gen = status.get("generator", {})

        lines = [
            f"**Stream**: {'▶ Running' if status['running'] else '■ Stopped'}",
            f"**Blocks**: {status['block_count']}",
            f"**Frames**: {status['frame_count']}",
            f"",
            f"**Anti-Drift**:",
            f"  History Corrupt: {'✓' if gen.get('history_corrupt_enabled') else '✗'}",
            f"  AAS: {'✓' if gen.get('aas_enabled') else '✗'} "
            f"(sink: {'model' if gen.get('aas_updated') else 'ref'})",
            f"  Rolling RoPE: {'✓' if gen.get('rope_enabled') else '✗'} "
            f"(block: {gen.get('rope_block', 0)})",
            f"",
            f"**KV Cache**: {gen.get('kv_cache_sizes', [])}",
        ]

        if self._block_results:
            last = self._block_results[-1]
            lines.extend([
                f"",
                f"**Last Block**:",
                f"  #{last.block_idx}  Latency: {last.latency_ms:.0f}ms",
            ])

        return "\n".join(lines)

    def _format_log(self) -> str:
        """Format recent stream log entries."""
        if not self._stream_log:
            return "No activity yet"
        return "\n".join(self._stream_log[-20:])

    def _format_response_text(self) -> str:
        """Format the last LLM response text."""
        if not self._last_response_text:
            return ""
        return self._last_response_text

    # ------------------------------------------------------------------
    # Build Gradio UI
    # ------------------------------------------------------------------

    def build_ui(self) -> gr.Blocks:
        """Build the Gradio interface with auto-refresh."""
        with gr.Blocks(
            title="LiveAvatar Demo",
            theme=gr.themes.Soft(),
        ) as demo:
            gr.Markdown("# LiveAvatar Demo — Block-wise Autoregressive Streaming")
            gr.Markdown(
                "Click **▶ Start Stream** to begin continuous streaming. "
                "Mock viewer messages auto-loop. Type messages manually below. "
                "Video preview + dashboard auto-refresh every 0.5s."
            )

            with gr.Row():
                # ── Left panel: controls ──
                with gr.Column(scale=1, min_width=280):
                    ref_image = gr.Image(
                        label="Reference Image (optional)",
                        type="filepath",
                    )

                    with gr.Accordion("Anti-Drift Settings", open=True):
                        enable_hc = gr.Checkbox(value=True, label="History Corrupt")
                        enable_aas = gr.Checkbox(value=True, label="AAS (Adaptive Attention Sink)")
                        enable_rope = gr.Checkbox(value=True, label="Rolling RoPE")

                    with gr.Row():
                        start_btn = gr.Button("▶ Start Stream", variant="primary")
                        stop_btn = gr.Button("■ Stop Stream", variant="stop")

                    status_text = gr.Textbox(
                        label="Status",
                        value="Ready — click Start Stream",
                        interactive=False,
                    )

                    # ── Chat section ──
                    gr.Markdown("---\n### 💬 Chat")
                    chat_input = gr.Textbox(
                        label="Send a message into the stream",
                        placeholder="Xin chào! Giá kem chống nắng?",
                        lines=2,
                    )
                    chat_send = gr.Button("📨 Send", variant="secondary")
                    chat_status = gr.Textbox(label="", interactive=False, show_label=False)

                    # ── LLM Response display ──
                    gr.Markdown("---\n### 🗣 Avatar Speaking")
                    response_text = gr.Textbox(
                        label="Last LLM Response",
                        interactive=False,
                        lines=3,
                        max_lines=5,
                    )

                # ── Center panel: video preview (auto-refresh) ──
                with gr.Column(scale=2):
                    video_preview = gr.Image(
                        label="📺 Live Video (auto-refreshes every 0.5s)",
                        value=self._placeholder,
                        height=450,
                    )

                    # WebRTC viewer link
                    gr.Markdown(
                        f"🔗 **WebRTC low-latency viewer**: "
                        f"[http://localhost:{self.signaling_port}/static/index.html]"
                        f"(http://localhost:{self.signaling_port}/static/index.html)"
                    )

                # ── Right panel: dashboard + log (auto-refresh) ──
                with gr.Column(scale=1, min_width=260):
                    dashboard = gr.Markdown(
                        value=self._format_dashboard(),
                    )
                    stream_log = gr.Textbox(
                        label="📋 Stream Log",
                        value="No activity yet",
                        lines=12,
                        interactive=False,
                        max_lines=20,
                    )

            # ── Auto-refresh: update preview + dashboard every 0.5s ──
            timer = gr.Timer(value=0.5)
            timer.tick(
                fn=self.on_auto_refresh,
                outputs=[video_preview, dashboard, stream_log, response_text, status_text],
            )

            # ── Manual callbacks ──
            start_btn.click(
                fn=self.on_start_stream,
                inputs=[ref_image, enable_hc, enable_aas, enable_rope],
                outputs=[status_text, video_preview, dashboard, stream_log, response_text],
            )

            stop_btn.click(
                fn=self.on_stop_stream,
                outputs=[status_text, video_preview, dashboard, stream_log, response_text],
            )

            chat_send.click(
                fn=self.on_send_message,
                inputs=[chat_input],
                outputs=[chat_status, stream_log],
            )

        return demo

    def launch(self) -> None:
        """Launch the Gradio app and signaling server."""
        from signaling.server import SignalingServer, SignalingServerConfig

        signaling_config = SignalingServerConfig(
            port=self.signaling_port,
            static_dir=str(Path(__file__).parent / "static"),
        )
        signaling = SignalingServer(
            frame_queue=self.frame_queue,
            config=signaling_config,
        )

        signaling_thread = threading.Thread(
            target=signaling.run,
            daemon=True,
        )
        signaling_thread.start()

        demo = self.build_ui()
        demo.launch(
            server_port=self.gradio_port,
            share=False,
        )


def main() -> None:
    """Entry point: launch the LiveAvatar demo app."""
    import argparse

    parser = argparse.ArgumentParser(description="LiveAvatar Demo")
    parser.add_argument("--config", type=Path, default=None, help="Path to default.yaml config")
    parser.add_argument("--device", type=str, default="cpu", help="Torch device (cpu, cuda)")
    parser.add_argument("--signaling-port", type=int, default=8000, help="WebRTC signaling server port")
    parser.add_argument("--gradio-port", type=int, default=7860, help="Gradio UI port")
    args = parser.parse_args()

    app = LiveAvatarApp(
        config_path=args.config,
        device=args.device,
        signaling_port=args.signaling_port,
        gradio_port=args.gradio_port,
    )
    app.launch()


if __name__ == "__main__":
    main()
