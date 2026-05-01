"""Configuration loaded from environment variables (with .env support)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError

load_dotenv()


@dataclass(frozen=True)
class Settings:
    clerk_cookie: str
    session_id: str
    max_concurrent: int
    default_model: str
    default_resolution: str
    output_dir: Path
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        cookie = os.getenv("HIGGSFIELD_CLERK_COOKIE", "").strip()
        session = os.getenv("HIGGSFIELD_SESSION_ID", "").strip()

        if not cookie:
            raise ConfigError(
                "HIGGSFIELD_CLERK_COOKIE is not set. Get it from Chrome DevTools: "
                "Application → Cookies → higgsfield.ai → __client"
            )
        if not session:
            raise ConfigError(
                "HIGGSFIELD_SESSION_ID is not set. Get it in Chrome console: "
                "window.Clerk.session.id"
            )

        # Output dir resolution priority:
        #   1. Per-call `output_dir` argument on a tool   (handled in server.py)
        #   2. HIGGSFIELD_OUTPUT_DIR env var               (explicit global default)
        #   3. <cwd>/higgsfield_output/                    (auto-routes per project)
        #
        # The cwd default means: launch Claude Code from a project folder, and
        # outputs land in that project's higgsfield_output/ subfolder. No config
        # needed for per-project routing.
        env_output = os.getenv("HIGGSFIELD_OUTPUT_DIR", "").strip()
        if env_output:
            output_dir = Path(env_output).expanduser().resolve()
        else:
            output_dir = (Path.cwd() / "higgsfield_output").resolve()

        return cls(
            clerk_cookie=cookie,
            session_id=session,
            max_concurrent=int(os.getenv("HIGGSFIELD_MAX_CONCURRENT", "4")),
            default_model=os.getenv("HIGGSFIELD_DEFAULT_MODEL", "nano-banana-2"),
            default_resolution=os.getenv("HIGGSFIELD_DEFAULT_RESOLUTION", "2k"),
            output_dir=output_dir,
            log_level=os.getenv("HIGGSFIELD_LOG_LEVEL", "INFO").upper(),
        )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


# Aspect ratio → pixel dimensions
ASPECT_DIMENSIONS: dict[str, tuple[int, int]] = {
    "9:16": (768, 1376),
    "16:9": (1376, 768),
    "1:1":  (1024, 1024),
    "4:5":  (896, 1120),
    "5:4":  (1120, 896),
    "2:3":  (832, 1248),
    "3:2":  (1248, 832),
    "21:9": (1680, 720),
    "4:3":  (1152, 864),
    "3:4":  (864, 1152),
}

VALID_RESOLUTIONS = {"1k", "2k", "4k"}


# Models discovered from the live web app (POST /jobs/{model_id})
# Categorized for the list_models tool. All accept use_unlim=True.
IMAGE_MODELS: dict[str, str] = {
    "nano-banana": "Nano Banana — base image model",
    "nano-banana-2": "Nano Banana Pro — flagship image quality",
    "nano-banana-2-shots": "Nano Banana Pro multi-shot (storyboards)",
    "nano-banana-2-upscale": "Nano Banana Pro upscaler",
    "nano-banana/batch": "Nano Banana batch endpoint",
    "v2/nano_banana_flash": "Nano Banana Flash — faster",
    "flux-2": "Flux 2",
    "flux-kontext": "Flux Kontext — context-aware editing",
    "openai-hazel": "OpenAI Hazel",
    "openai-hazel-mini": "OpenAI Hazel Mini",
    "reve": "Reve",
    "seedream": "Seedream",
    "seedream-v4-5": "Seedream v4.5",
    "v2/seedream_v5_lite": "Seedream v5 Lite",
    "text2image": "text2image (legacy)",
    "text2image-gpt": "text2image GPT",
    "text2image-soul": "text2image Soul",
    "text2image-soul/batch": "text2image Soul batch",
    "v2/text2image_soul_v2": "text2image Soul v2",
    "wan2-2-image": "Wan 2.2 image",
    "z-image": "Z-Image",
    "kling-omni-image": "Kling Omni image",
    "kling-omni-image-reference": "Kling Omni image with reference",
    "v2/image_auto": "Image Auto (model auto-routing)",
    "v2/imagegen_2_0": "Imagegen 2.0",
    "v2/soul_cinematic": "Soul Cinematic image",
    "v2/soul_location": "Soul Location image",
    "v2/cinematic_studio_image": "Cinematic Studio image",
    "v2/next_shots": "Next Shots — character continuation",
    "character-swap/v2": "Character Swap v2",
    "face-swap": "Face Swap",
    "canvas": "Canvas",
    "canvas-soul": "Canvas Soul",
}

VIDEO_MODELS: dict[str, str] = {
    "image2video": "Image-to-video (generic)",
    "image2video-extend-v2": "Extend an existing video",
    "image2video-mix": "Mix multiple images into video",
    "kling": "Kling video",
    "kling2-6": "Kling 2.6",
    "kling-omni-flf": "Kling Omni first-last-frame",
    "kling-speak": "Kling lip-sync from speech",
    "kling-transition": "Kling transition between shots",
    "kling-video-edit": "Kling video edit",
    "kling-video-reference": "Kling video reference",
    "minimax-hailuo": "MiniMax Hailuo",
    "seedance": "Seedance video",
    "sora2-video": "Sora 2 video",
    "veo3": "Google Veo 3",
    "veo3-1": "Google Veo 3.1",
    "veo3-speak": "Veo 3 with speech",
    "veo3-1-speak": "Veo 3.1 with speech",
    "viral-transform-video": "Viral transform",
    "wan2-2-video": "Wan 2.2 video",
    "wan2-2-animate": "Wan 2.2 animate",
    "wan2-2-animate-faceswap": "Wan 2.2 animate face-swap",
    "wan2-2-animate-revoice": "Wan 2.2 animate revoice",
    "wan2-2-animate-v3": "Wan 2.2 animate v3",
    "wan2-5-video": "Wan 2.5 video",
    "wan2-5-speak": "Wan 2.5 lip-sync",
    "wan2-6": "Wan 2.6",
    "infinite-talk": "Infinite Talk — lip-sync long form",
    "v2/cinematic_studio_3_0": "Cinematic Studio 3.0",
    "v2/cinematic_studio_video": "Cinematic Studio video",
    "v2/cinematic_studio_video_3_5": "Cinematic Studio video 3.5",
    "v2/cinematic_studio_video_v2": "Cinematic Studio video v2",
}

AUDIO_MODELS: dict[str, str] = {
    "text2speech": "Text-to-speech",
}

# Common alias mapping — accept underscored names and route to live hyphenated model IDs
MODEL_ALIASES: dict[str, str] = {
    "nano_banana": "nano-banana",
    "nano_banana_2": "nano-banana-2",
    "nano_banana_flash": "v2/nano_banana_flash",
    "soul_cinematic": "v2/soul_cinematic",
    "cinematic_studio_2_5": "v2/cinematic_studio_video_3_5",  # closest current equivalent
    "cinematic_studio_3_0": "v2/cinematic_studio_3_0",
}


def all_models() -> dict[str, dict[str, str]]:
    return {
        "image": IMAGE_MODELS,
        "video": VIDEO_MODELS,
        "audio": AUDIO_MODELS,
    }


def resolve_model(model: str) -> str:
    """Normalize a model name (handle underscored aliases)."""
    return MODEL_ALIASES.get(model, model)


def model_category(model: str) -> str:
    canonical = resolve_model(model)
    if canonical in IMAGE_MODELS:
        return "image"
    if canonical in VIDEO_MODELS:
        return "video"
    if canonical in AUDIO_MODELS:
        return "audio"
    return "unknown"


def resolve_dimensions(aspect: str, width: int | None, height: int | None) -> tuple[int, int]:
    """If width/height not given, look up from aspect."""
    if width and height:
        return width, height
    if aspect not in ASPECT_DIMENSIONS:
        raise ConfigError(
            f"Unknown aspect_ratio '{aspect}'. Provide width+height or use one of: "
            f"{sorted(ASPECT_DIMENSIONS)}"
        )
    return ASPECT_DIMENSIONS[aspect]
