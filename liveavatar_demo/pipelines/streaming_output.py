"""Streaming Output — convert latent blocks to video frames.

Decodes latents (mock VAE decode) into video frames (numpy/PIL arrays).
Frames are visually distinct per block so the streaming loop is observable.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont


class StreamingOutput:
    """Converts latent blocks to displayable video frames.

    In production, this would use Wan2.2's causal VAE (~500MB) to decode
    latents into actual video frames. For this demo, we simulate VAE
    decode by generating visually informative frames showing:
    - Block index (incrementing counter)
    - Audio text being spoken
    - Facial animation that varies with audio amplitude
    - Color shifts per block for visual feedback
    - Anti-drift status indicators

    Parameters
    ----------
    resolution : tuple[int, int]
        Output resolution (height, width).
    fps : int
        Target frames per second.
    """

    def __init__(
        self,
        resolution: tuple[int, int] = (400, 720),
        fps: int = 24,
    ) -> None:
        self.resolution = resolution
        self.fps = fps
        self._frame_count = 0
        self._block_idx = 0
        self._current_text = ""
        self._text_scroll_offset = 0

    def decode_block(
        self,
        latent_block: torch.Tensor,
        block_idx: int = 0,
        audio_text: str = "",
    ) -> list[np.ndarray]:
        """Decode a block of latents into video frames.

        Parameters
        ----------
        latent_block : torch.Tensor
            Latent block from the avatar generator.
            Shape: (block_size, C, H, W).
        block_idx : int
            Current block index for visual feedback.
        audio_text : str
            Text being spoken this block (shown on frame).

        Returns
        -------
        frames : list[np.ndarray]
            Decoded video frames as RGB uint8 arrays.
        """
        block_size = latent_block.shape[0]
        self._block_idx = block_idx
        self._current_text = audio_text
        frames = []

        for i in range(block_size):
            frame = self._mock_vae_decode(
                latent_block[i],
                sub_frame=i,
                block_idx=block_idx,
                audio_text=audio_text,
            )
            frames.append(frame)
            self._frame_count += 1

        return frames

    def _mock_vae_decode(
        self,
        latent: torch.Tensor,
        sub_frame: int = 0,
        block_idx: int = 0,
        audio_text: str = "",
    ) -> np.ndarray:
        """Simulate VAE decode: generate a displayable frame from latent.

        Creates a visually informative frame showing the pipeline state:
        - Gradient background that shifts color per block
        - "Face" region with mouth that opens/closes per sub-frame
        - Block counter overlay
        - Scrolling text of what's being spoken
        - Anti-drift indicator dots

        Parameters
        ----------
        latent : torch.Tensor
            Single frame latent. Shape: (C, H, W).
        sub_frame : int
            Frame index within block (0, 1, 2) — drives mouth animation.
        block_idx : int
            Current block index.
        audio_text : str
            Text being spoken.

        Returns
        -------
        frame : np.ndarray
            RGB uint8 array (H_out, W_out, 3).
        """
        h_out, w_out = self.resolution

        # ── Background: color gradient that shifts per block ──
        latent_mean = latent.mean().item()
        latent_std = latent.std().item()

        # Hue rotates with block index for clear visual change
        hue_phase = (block_idx * 37) % 360  # distinct color per block
        # Map hue to RGB (simplified HSV→RGB for 6 sectors)
        h_sector = (hue_phase / 60.0) % 6
        c = 0.3 + min(abs(latent_mean) * 0.05, 0.2)  # chroma varies with latent
        x = c * (1 - abs(h_sector % 2 - 1))

        if h_sector < 1:
            bg_r, bg_g, bg_b = c, x, 0
        elif h_sector < 2:
            bg_r, bg_g, bg_b = x, c, 0
        elif h_sector < 3:
            bg_r, bg_g, bg_b = 0, c, x
        elif h_sector < 4:
            bg_r, bg_g, bg_b = 0, x, c
        elif h_sector < 5:
            bg_r, bg_g, bg_b = x, 0, c
        else:
            bg_r, bg_g, bg_b = c, 0, x

        # Add base + gradient
        bg_base = np.array([40, 35, 55], dtype=np.float32)
        bg_modulation = np.array([bg_r, bg_g, bg_b], dtype=np.float32) * 80

        frame = np.zeros((h_out, w_out, 3), dtype=np.float32)

        # Vertical gradient (darker at top, lighter at bottom)
        for y in range(h_out):
            t = y / h_out
            frame[y, :] = bg_base + bg_modulation * (0.5 + 0.5 * t)

        # ── Face region ──
        cy, cx = int(h_out * 0.42), w_out // 2
        ry, rx = int(h_out * 0.22), int(w_out * 0.16)
        y_grid, x_grid = np.ogrid[:h_out, :w_out]
        face_mask = ((y_grid - cy) / ry) ** 2 + ((x_grid - cx) / rx) ** 2 <= 1

        # Skin tone with audio modulation
        skin_r = np.clip(int(200 + latent_mean * 8), 140, 240)
        skin_g = np.clip(int(170 + latent_std * 5), 120, 210)
        skin_b = np.clip(int(150 + latent_std * 3), 100, 190)
        frame[face_mask] = [skin_r, skin_g, skin_b]

        # ── Eyes (two small circles) ──
        eye_y = cy - int(ry * 0.25)
        eye_rx = int(rx * 0.12)
        eye_ry = int(ry * 0.10)
        for eye_x_off in [-int(rx * 0.35), int(rx * 0.35)]:
            eye_cx = cx + eye_x_off
            eye_mask = (
                ((y_grid - eye_y) / max(eye_ry, 1)) ** 2
                + ((x_grid - eye_cx) / max(eye_rx, 1)) ** 2
            ) <= 1
            frame[eye_mask] = [30, 30, 30]  # dark eyes

        # ── Mouth (opens/closes per sub-frame — "talking animation") ──
        mouth_cy = cy + int(ry * 0.45)
        mouth_open = int(4 + 8 * (sub_frame + 1) / 3 + abs(latent_mean) * 3)
        mouth_rx = int(rx * 0.35)
        mouth_mask = (
            (np.abs(y_grid - mouth_cy) < mouth_open)
            & (np.abs(x_grid - cx) < mouth_rx)
            & face_mask
        )
        frame[mouth_mask] = [180, 80, 80]  # mouth

        # ── Overlay text: block counter ──
        img = Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8))
        draw = ImageDraw.Draw(img)

        try:
            font_large = ImageFont.truetype("arial.ttf", 20)
            font_small = ImageFont.truetype("arial.ttf", 14)
        except (OSError, IOError):
            font_large = ImageFont.load_default()
            font_small = ImageFont.load_default()

        # Top-left: block/frame counter
        draw.rectangle([8, 8, 230, 70], fill=(0, 0, 0, 160))
        draw.text((14, 14), f"Block #{block_idx}", fill=(255, 255, 255), font=font_large)
        draw.text((14, 38), f"Frame #{self._frame_count}", fill=(200, 200, 200), font=font_small)
        draw.text((14, 54), f"Sub-frame {sub_frame}/3", fill=(160, 160, 160), font=font_small)

        # Top-right: anti-drift dots
        draw.rectangle([w_out - 110, 8, w_out - 8, 32], fill=(0, 0, 0, 160))
        dots = "●" * min(block_idx, 5) + "○" * max(0, 5 - min(block_idx, 5))
        draw.text((w_out - 105, 12), f"Drift:{dots}", fill=(100, 255, 100), font=font_small)

        # Bottom: scrolling text of what's being spoken
        if audio_text:
            # Truncate for display
            display_text = audio_text[:80]
            draw.rectangle([8, h_out - 38, w_out - 8, h_out - 8], fill=(0, 0, 0, 180))
            draw.text((14, h_out - 34), f"🗣 {display_text}", fill=(255, 255, 100), font=font_small)

        return np.array(img, dtype=np.uint8)

    @staticmethod
    def frame_to_pil(frame: np.ndarray) -> Image.Image:
        """Convert numpy frame to PIL Image."""
        return Image.fromarray(frame, mode="RGB")

    @staticmethod
    def frame_to_bytes_jpeg(frame: np.ndarray, quality: int = 85) -> bytes:
        """Compress frame to JPEG bytes for WebRTC encoding."""
        img = Image.fromarray(frame, mode="RGB")
        import io

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def reset(self) -> None:
        self._frame_count = 0
        self._block_idx = 0
        self._current_text = ""
