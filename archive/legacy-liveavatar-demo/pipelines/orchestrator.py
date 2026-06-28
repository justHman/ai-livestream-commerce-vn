"""Streaming Orchestrator — main block-wise autoregressive loop.

Implements Algorithm 3 from LiveAvatar (arXiv 2512.04677):
1. Encode reference image → sink latent
2. For each block in infinite stream:
   a. Get audio chunk → encode → audio_embed
   b. Init noise x ~ N(0, sigma_max)
   c. Denoise T steps with rolling KV cache + anti-drift
   d. Yield decoded video frames
3. After first block: AAS replaces sink with model output

This is the central coordinator that ties together:
- MockLLMResponder (text generation)
- EdgeTTSEngine (text → audio)
- Wav2Vec2AudioEncoder (audio → embedding)
- MockAvatarGenerator (embedding → latent with anti-drift)
- StreamingOutput (latent → video frames)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional

import numpy as np
import torch
import yaml

from .audio_encoder import Wav2Vec2AudioEncoder
from .avatar_generator import GeneratorConfig, MockAvatarGenerator
from .llm_responder import MockLLMResponder
from .streaming_output import StreamingOutput
from .tts_engine import EdgeTTSEngine, TTSResult


@dataclass
class StreamConfig:
    """Full pipeline configuration."""

    # Generator
    generator: GeneratorConfig = None  # type: ignore[assignment]

    # Output
    resolution: tuple[int, int] = (400, 720)
    target_fps: int = 24

    # Audio encoder
    wav2vec2_model: str = "facebook/wav2vec2-base"

    # TTS
    tts_voice: str = "vi-VN-HoaiMyNeural"
    tts_rate: str = "+0%"

    # Anti-drift toggles
    enable_history_corrupt: bool = True
    enable_aas: bool = True
    enable_rolling_rope: bool = True

    # Streaming
    max_blocks: int = 0  # 0 = infinite
    block_delay_ms: int = 0  # artificial delay per block (for testing)

    def __post_init__(self):
        if self.generator is None:
            self.generator = GeneratorConfig()
        # Sync anti-drift settings
        self.generator.enable_history_corrupt = self.enable_history_corrupt
        self.generator.enable_aas = self.enable_aas
        self.generator.enable_rolling_rope = self.enable_rolling_rope


@dataclass
class BlockResult:
    """Output from one generated block."""

    block_idx: int
    frames: list[np.ndarray]
    audio_text: str
    audio_path: Optional[Path]
    latency_ms: float
    cache_sizes: list[int]
    aas_updated: bool
    rope_block: int


class StreamingOrchestrator:
    """Main pipeline orchestrator for block-wise autoregressive streaming.

    Parameters
    ----------
    config : StreamConfig
        Pipeline configuration.
    catalog_path : Path or None
        Product catalog for the LLM responder.
    device : str
        Torch device.
    """

    def __init__(
        self,
        config: Optional[StreamConfig] = None,
        catalog_path: Optional[Path] = None,
        device: str = "cpu",
    ) -> None:
        self.config = config or StreamConfig()
        self.device = device

        # Initialize components
        self.generator = MockAvatarGenerator(
            config=self.config.generator, device=device,
        )
        self.tts = EdgeTTSEngine(
            voice=self.config.tts_voice,
            rate=self.config.tts_rate,
        )
        self.audio_encoder = Wav2Vec2AudioEncoder(
            model_name=self.config.wav2vec2_model,
            device=device,
        )
        self.output = StreamingOutput(
            resolution=self.config.resolution,
            fps=self.config.target_fps,
        )
        self.responder = MockLLMResponder(catalog_path=catalog_path)

        self._running = False
        self._block_count = 0

    def start_stream(
        self,
        ref_image_path: Path,
    ) -> None:
        """Initialize a streaming session with a reference image.

        Parameters
        ----------
        ref_image_path : Path
            Path to the reference image (avatar).
        """
        # Mock encode reference image → latent
        ref_latent = self._mock_encode_ref(ref_image_path)

        # Start the generator stream
        self.generator.start_stream(ref_latent)
        self.output.reset()
        self._block_count = 0
        self._running = True

    def stop_stream(self) -> None:
        """Stop the streaming session."""
        self._running = False

    def generate_blocks(
        self,
        viewer_messages: list[str],
    ) -> Generator[BlockResult, None, None]:
        """Generate blocks of video from viewer messages.

        For each viewer message:
        1. LLM generates response text
        2. TTS generates audio from text
        3. Audio encoder extracts embeddings
        4. Avatar generator produces latent block with anti-drift
        5. Streaming output decodes to video frames

        Parameters
        ----------
        viewer_messages : list[str]
            Chat messages from viewers.

        Yields
        ------
        BlockResult
            One block of video frames with metadata.
        """
        for msg in viewer_messages:
            if not self._running:
                break

            t_start = time.perf_counter()

            try:
                # 1. LLM response
                response_text = self.responder.respond(msg)

                # 2. TTS
                tts_result = self.tts.synthesize_sync(response_text)

                # 3. Audio embedding
                audio_embed = self.audio_encoder.encode(tts_result.wav_path)
            except Exception as exc:
                # TTS / audio encoder failure → skip this block, keep streaming
                import sys
                print(f"[orchestrator] Block {self._block_count} failed: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                continue

            # 4. Generate latent block
            latent_block = self.generator.generate_block(
                audio_embed=audio_embed.embedding,
                block_idx=self._block_count,
            )

            # 5. Decode to frames
            frames = self.output.decode_block(
                latent_block,
                block_idx=self._block_count,
                audio_text=response_text,
            )

            latency_ms = (time.perf_counter() - t_start) * 1000

            status = self.generator.get_status()

            yield BlockResult(
                block_idx=self._block_count,
                frames=frames,
                audio_text=response_text,
                audio_path=tts_result.wav_path,
                latency_ms=latency_ms,
                cache_sizes=status["kv_cache_sizes"],
                aas_updated=status["aas_updated"],
                rope_block=status["rope_block"],
            )

            self._block_count += 1

            if self.config.block_delay_ms > 0:
                time.sleep(self.config.block_delay_ms / 1000)

            # Check max blocks
            if self.config.max_blocks > 0 and self._block_count >= self.config.max_blocks:
                break

    def generate_continuous(
        self,
        get_next_message: callable,
    ) -> Generator[BlockResult, None, None]:
        """Generate blocks continuously from a message source.

        Parameters
        ----------
        get_next_message : callable
            Function that returns the next viewer message string.
            Called repeatedly for infinite streaming.

        Yields
        ------
        BlockResult
        """
        while self._running:
            msg = get_next_message()
            if msg is None:
                break

            for block in self.generate_blocks([msg]):
                yield block

    def _mock_encode_ref(self, ref_image_path: Path) -> torch.Tensor:
        """Mock encode a reference image into a latent.

        In production, this would use the VAE encoder.
        Here we generate a deterministic latent based on image path.

        Parameters
        ----------
        ref_image_path : Path
            Path to the reference image.

        Returns
        -------
        ref_latent : torch.Tensor
            Shape: (C, H, W).
        """
        # Use image path hash as seed for reproducibility
        seed = hash(str(ref_image_path)) % (2**31)
        rng = torch.Generator(device=self.device)
        rng.manual_seed(seed)

        return torch.randn(
            self.config.generator.latent_channels,
            self.config.generator.latent_height,
            self.config.generator.latent_width,
            generator=rng,
            device=self.device,
        ) * 0.5

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def block_count(self) -> int:
        return self._block_count

    def get_status(self) -> dict:
        """Return full pipeline status for the UI dashboard."""
        return {
            "running": self._running,
            "block_count": self._block_count,
            "frame_count": self.output.frame_count,
            "generator": self.generator.get_status(),
        }

    @classmethod
    def from_yaml(cls, config_path: Path, device: str = "cpu") -> "StreamingOrchestrator":
        """Create an orchestrator from a YAML config file."""
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        gen_cfg = GeneratorConfig(**data.get("generator", {}))
        stream_cfg = StreamConfig(
            generator=gen_cfg,
            resolution=tuple(data.get("resolution", [400, 720])),
            target_fps=data.get("target_fps", 24),
            wav2vec2_model=data.get("wav2vec2_model", "facebook/wav2vec2-base"),
            tts_voice=data.get("tts_voice", "vi-VN-HoaiMyNeural"),
            tts_rate=data.get("tts_rate", "+0%"),
            enable_history_corrupt=data.get("enable_history_corrupt", True),
            enable_aas=data.get("enable_aas", True),
            enable_rolling_rope=data.get("enable_rolling_rope", True),
            max_blocks=data.get("max_blocks", 0),
            block_delay_ms=data.get("block_delay_ms", 0),
        )

        return cls(config=stream_cfg, device=device)
