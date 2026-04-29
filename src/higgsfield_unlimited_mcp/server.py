"""FastMCP server exposing Higgsfield Unlimited tools."""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiohttp
from mcp.server.fastmcp import Context, FastMCP

from .auth import TokenManager
from .client import HiggsfieldClient
from .config import (
    AUDIO_MODELS,
    IMAGE_MODELS,
    VIDEO_MODELS,
    Settings,
    all_models,
    configure_logging,
    model_category,
    resolve_dimensions,
    resolve_model,
    VALID_RESOLUTIONS,
    ASPECT_DIMENSIONS,
)
from .errors import (
    AuthError,
    ConfigError,
    HiggsfieldError,
    JobFailedError,
    JobNotFoundError,
    JobTimeoutError,
)
from .registry import JobRecord, JobRegistry

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Lifespan: shared resources for the server
# ─────────────────────────────────────────────

@dataclass
class AppContext:
    settings: Settings
    http: aiohttp.ClientSession
    tokens: TokenManager
    client: HiggsfieldClient
    registry: JobRegistry


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    connector = aiohttp.TCPConnector(limit=settings.max_concurrent + 8)
    timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=60)
    http = aiohttp.ClientSession(connector=connector, timeout=timeout)

    tokens = TokenManager(settings.session_id, settings.clerk_cookie)
    client = HiggsfieldClient(http, tokens)
    registry = JobRegistry(settings.max_concurrent)

    settings.output_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "Higgsfield Unlimited MCP ready (model=%s, res=%s, slots=%d)",
        settings.default_model, settings.default_resolution, settings.max_concurrent,
    )

    try:
        yield AppContext(
            settings=settings, http=http, tokens=tokens,
            client=client, registry=registry,
        )
    finally:
        await http.close()
        log.info("Server shutdown complete")


mcp = FastMCP("higgsfield-unlimited", lifespan=lifespan)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _ctx(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]+")

def _safe_filename(prompt: str, job_id: str, idx: int = 0, ext: str = "png") -> str:
    snippet = _SAFE_RE.sub("_", prompt[:40]).strip("_") or "result"
    suffix = f"_{idx}" if idx > 0 else ""
    return f"{snippet}_{job_id[:8]}{suffix}.{ext}"


def _ext_from_url(url: str, default: str = "png") -> str:
    base = url.split("?")[0]
    if "." in base.rsplit("/", 1)[-1]:
        ext = base.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 5:
            return ext
    return default


def _validate_resolution(resolution: str) -> str:
    res = resolution.lower().strip()
    if res not in VALID_RESOLUTIONS:
        raise ConfigError(
            f"Invalid resolution '{resolution}'. Must be one of: {sorted(VALID_RESOLUTIONS)}"
        )
    return res


async def _upload_local_files(app: AppContext, paths: list[str]) -> list[str]:
    """Upload local files via /upload and return their URLs.

    Skips entries that are already URLs (http:// or https://).
    """
    urls: list[str] = []
    for entry in paths:
        if not entry:
            continue
        if entry.startswith("http://") or entry.startswith("https://"):
            urls.append(entry)
            continue
        path = Path(entry).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"input_file not found: {path}")
        ct = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        result = await app.client.upload_media(
            path.read_bytes(), filename=path.name, content_type=ct
        )
        # The upload response shape is flexible — pick the URL from common fields
        url = (
            (isinstance(result, dict) and (
                result.get("url") or result.get("file_url") or result.get("public_url")
                or (isinstance(result.get("media"), dict) and result["media"].get("url"))
                or (isinstance(result.get("data"), dict) and result["data"].get("url"))
            )) or None
        )
        if not url:
            raise HiggsfieldError(
                f"Upload of {path.name} succeeded but no URL in response: {result}"
            )
        urls.append(url)
    return urls


async def _resolve_inputs(
    app: AppContext,
    input_images: Optional[list[str]],
    input_files: Optional[list[str]],
) -> list[str]:
    """Combine pre-uploaded URLs with newly-uploaded local files."""
    out: list[str] = list(input_images or [])
    if input_files:
        uploaded = await _upload_local_files(app, input_files)
        out.extend(uploaded)
    return out


async def _run_job_to_completion(
    app: AppContext,
    record: JobRecord,
    *,
    output_dir: Path,
    download: bool,
    poll_timeout: int,
    default_ext: str = "png",
) -> JobRecord:
    """Wait for a job, optionally download, update registry. Holds a slot."""
    async with app.registry.semaphore:
        await app.registry.mark_active()
        try:
            await app.registry.update(record.job_id, status="active")
            try:
                final = await app.client.poll_job(record.job_id, timeout=poll_timeout)
            except JobTimeoutError as e:
                await app.registry.update(record.job_id, status="timeout", error=str(e))
                raise
            except JobFailedError as e:
                await app.registry.update(record.job_id, status="failed", error=str(e))
                raise

            urls = HiggsfieldClient.extract_result_urls(final)
            paths: list[Path] = []
            if download and urls:
                output_dir.mkdir(parents=True, exist_ok=True)
                for i, url in enumerate(urls):
                    try:
                        data = await app.client.download(url)
                        ext = _ext_from_url(url, default_ext)
                        out = output_dir / _safe_filename(record.prompt, record.job_id, i, ext)
                        out.write_bytes(data)
                        paths.append(out)
                    except Exception as e:
                        log.warning("Download failed for %s: %s", url, e)

            await app.registry.update(
                record.job_id,
                status="completed",
                completed_at=time.time(),
                last_status_payload=final,
                output_urls=urls,
                output_paths=paths,
            )
            return await app.registry.get(record.job_id)
        finally:
            await app.registry.mark_inactive()


# ─────────────────────────────────────────────
# Auth / account / status
# ─────────────────────────────────────────────

@mcp.tool()
async def auth_status(ctx: Context) -> dict:
    """Verify Clerk credentials by fetching a fresh JWT."""
    app = _ctx(ctx)
    try:
        await app.tokens.get_token(app.http, force=True)
        return {
            "ok": True,
            "session_id_prefix": app.settings.session_id[:12] + "...",
            "message": "Credentials valid; JWT acquired.",
        }
    except AuthError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def account_info(ctx: Context) -> dict:
    """Get user plan info, credit balances, and unlimited mode status.

    Equivalent to the official MCP's `balance` + `transactions` combined.
    Returns `has_unlim`, plan type, and all per-feature credit balances.
    """
    app = _ctx(ctx)
    out: dict[str, Any] = {}
    try:
        out["user"] = await app.client.get_user()
    except HiggsfieldError as e:
        out["user_error"] = str(e)
    try:
        out["profile"] = await app.client.get_user_profile()
    except HiggsfieldError as e:
        out["profile_error"] = str(e)
    try:
        out["features"] = await app.client.get_user_features()
    except HiggsfieldError as e:
        out["features_error"] = str(e)
    return out


@mcp.tool()
async def concurrent_state(ctx: Context) -> dict:
    """Inspect concurrent-slot tier (4/8/12/16) for parallel generation."""
    app = _ctx(ctx)
    try:
        data = await app.client.get_concurrent_state()
    except HiggsfieldError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "remote_state": data,
        "local_max_concurrent": app.registry.max_concurrent,
        "local_active": app.registry.active_count,
    }


@mcp.tool()
async def queue_status(ctx: Context) -> dict:
    """Snapshot of the in-process job registry."""
    app = _ctx(ctx)
    return app.registry.snapshot()


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

@mcp.tool()
async def list_models(category: Optional[str] = None) -> dict:
    """List all generation models supported by Higgsfield in unlimited mode.

    Args:
        category: filter to "image", "video", or "audio". Default: all.

    Returns 60+ models discovered from the live web app — every model the
    paid Higgsfield MCP exposes, plus video and audio models the official
    MCP doesn't expose at all.
    """
    catalog = all_models()
    if category:
        cat = category.lower()
        if cat not in catalog:
            return {"error": f"Unknown category '{category}'. Valid: {list(catalog)}"}
        return {category: [{"id": k, "description": v} for k, v in catalog[cat].items()]}
    return {
        "image": [{"id": k, "description": v} for k, v in IMAGE_MODELS.items()],
        "video": [{"id": k, "description": v} for k, v in VIDEO_MODELS.items()],
        "audio": [{"id": k, "description": v} for k, v in AUDIO_MODELS.items()],
        "totals": {
            "image": len(IMAGE_MODELS),
            "video": len(VIDEO_MODELS),
            "audio": len(AUDIO_MODELS),
        },
    }


@mcp.tool()
async def get_aspect_dimensions(aspect_ratio: str) -> dict:
    """Look up the canonical width/height for an aspect ratio."""
    if aspect_ratio not in ASPECT_DIMENSIONS:
        return {
            "error": f"Unknown aspect_ratio '{aspect_ratio}'",
            "supported": sorted(ASPECT_DIMENSIONS.keys()),
        }
    w, h = ASPECT_DIMENSIONS[aspect_ratio]
    return {"aspect_ratio": aspect_ratio, "width": w, "height": h}


# ─────────────────────────────────────────────
# Image generation
# ─────────────────────────────────────────────

@mcp.tool()
async def generate_image(
    ctx: Context,
    prompt: str,
    model: Optional[str] = None,
    resolution: Optional[str] = None,
    aspect_ratio: str = "9:16",
    width: Optional[int] = None,
    height: Optional[int] = None,
    batch_size: int = 1,
    input_images: Optional[list[str]] = None,
    input_files: Optional[list[str]] = None,
    wait: bool = True,
    download: bool = True,
    output_dir: Optional[str] = None,
    poll_timeout: int = 600,
    tags: Optional[list[str]] = None,
) -> dict:
    """Submit a single image generation in unlimited mode.

    Models: nano-banana-2 (default), nano-banana, flux-2, seedream-v4-5,
    openai-hazel, reve, z-image, plus 25+ more — see list_models(category="image").

    Args:
        input_images: list of pre-uploaded URLs to use as references.
        input_files: list of LOCAL file paths to auto-upload via /upload, then
                     append to input_images. Use this to feed local PNGs/JPGs
                     without a separate media_upload call.
    """
    app = _ctx(ctx)
    model = resolve_model(model or app.settings.default_model)
    resolution = _validate_resolution(resolution or app.settings.default_resolution)
    w, h = resolve_dimensions(aspect_ratio, width, height)
    out_dir = Path(output_dir).expanduser() if output_dir else app.settings.output_dir

    resolved_inputs = await _resolve_inputs(app, input_images, input_files)

    job_id = await app.client.submit_image_job(
        prompt=prompt, model=model, width=w, height=h,
        aspect_ratio=aspect_ratio, resolution=resolution,
        batch_size=batch_size, input_images=resolved_inputs,
    )
    record = JobRecord(
        job_id=job_id, prompt=prompt, model=model, resolution=resolution,
        aspect_ratio=aspect_ratio, width=w, height=h, batch_size=batch_size,
        tags=list(tags or []),
    )
    await app.registry.register(record)

    if not wait:
        asyncio.create_task(
            _run_job_to_completion(
                app, record, output_dir=out_dir,
                download=download, poll_timeout=poll_timeout,
            )
        )
        return (await app.registry.get(job_id)).to_dict()

    try:
        final = await _run_job_to_completion(
            app, record, output_dir=out_dir,
            download=download, poll_timeout=poll_timeout,
        )
        return final.to_dict()
    except HiggsfieldError as e:
        rec = await app.registry.get(job_id)
        return {**rec.to_dict(), "error": str(e)}


@mcp.tool()
async def generate_image_batch(
    ctx: Context,
    prompts: list[str],
    model: Optional[str] = None,
    resolution: Optional[str] = None,
    aspect_ratio: str = "9:16",
    width: Optional[int] = None,
    height: Optional[int] = None,
    batch_size: int = 1,
    input_images: Optional[list[str]] = None,
    input_files: Optional[list[str]] = None,
    output_dir: Optional[str] = None,
    poll_timeout: int = 600,
    tag_prefix: Optional[str] = None,
    wait: bool = True,
) -> dict:
    """Queue many image prompts; runs up to max_concurrent in parallel.

    Same input_images / input_files semantics as generate_image — references
    are shared across every prompt in the batch (useful for storyboards).
    """
    app = _ctx(ctx)
    if not prompts:
        return {"job_ids": [], "results": [], "error": "No prompts provided"}

    model = resolve_model(model or app.settings.default_model)
    resolution = _validate_resolution(resolution or app.settings.default_resolution)
    w, h = resolve_dimensions(aspect_ratio, width, height)
    out_dir = Path(output_dir).expanduser() if output_dir else app.settings.output_dir

    # Upload local files ONCE, share across all prompts
    shared_inputs = await _resolve_inputs(app, input_images, input_files)

    records: list[JobRecord] = []
    for i, p in enumerate(prompts, 1):
        try:
            job_id = await app.client.submit_image_job(
                prompt=p, model=model, width=w, height=h,
                aspect_ratio=aspect_ratio, resolution=resolution,
                batch_size=batch_size, input_images=shared_inputs,
            )
            tags = [f"{tag_prefix}_{i:02d}"] if tag_prefix else []
            record = JobRecord(
                job_id=job_id, prompt=p, model=model, resolution=resolution,
                aspect_ratio=aspect_ratio, width=w, height=h, batch_size=batch_size,
                tags=tags,
            )
            await app.registry.register(record)
            records.append(record)
        except HiggsfieldError as e:
            log.error("Submission %d failed: %s", i, e)

    job_ids = [r.job_id for r in records]

    if not wait:
        for r in records:
            asyncio.create_task(
                _run_job_to_completion(
                    app, r, output_dir=out_dir,
                    download=True, poll_timeout=poll_timeout,
                )
            )
        return {
            "job_ids": job_ids, "results": [],
            "message": f"Queued {len(records)} jobs.",
        }

    async def _wrap(r: JobRecord) -> dict:
        try:
            final = await _run_job_to_completion(
                app, r, output_dir=out_dir,
                download=True, poll_timeout=poll_timeout,
            )
            return final.to_dict()
        except HiggsfieldError as e:
            rec = await app.registry.get(r.job_id)
            return {**rec.to_dict(), "error": str(e)}

    results = await asyncio.gather(*(_wrap(r) for r in records))
    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") in ("failed", "timeout"))
    return {
        "job_ids": job_ids, "results": results,
        "summary": {"submitted": len(records), "completed": completed, "failed": failed},
    }


# ─────────────────────────────────────────────
# Storyboard (multi-shot continuity)
# ─────────────────────────────────────────────

@mcp.tool()
async def generate_storyboard(
    ctx: Context,
    prompts: list[str],
    reference_images: Optional[list[str]] = None,
    reference_files: Optional[list[str]] = None,
    aspect_ratio: str = "9:16",
    width: Optional[int] = None,
    height: Optional[int] = None,
    resolution: Optional[str] = None,
    output_dir: Optional[str] = None,
    poll_timeout: int = 600,
    tag_prefix: str = "shot",
    wait: bool = True,
) -> dict:
    """Generate a multi-shot storyboard with character/style continuity.

    Uses `nano-banana-2-shots` — Higgsfield's dedicated multi-shot endpoint —
    which preserves character, style, and palette across every prompt by
    referencing the same set of input images on the server side. Better than
    firing N parallel `nano-banana-2` calls when you need continuity.

    Args:
        prompts: one prompt per shot
        reference_images: pre-uploaded URLs to use as style/character refs
        reference_files: LOCAL file paths to auto-upload as refs (uploaded once,
                         shared across all shots)
        aspect_ratio: e.g. "9:16" (portrait, recommended for storyboards)
        width, height: explicit pixel dims (overrides aspect_ratio)
        resolution: "1k" | "2k" | "4k"
        tag_prefix: prepended to a sequence number in each record's tags
        wait: if True, blocks until ALL shots finish

    At least one reference image OR file is REQUIRED — the model rejects empty
    input_images with HTTP 422.
    """
    app = _ctx(ctx)
    if not prompts:
        return {"error": "No prompts provided"}
    if not reference_images and not reference_files:
        return {
            "error": (
                "nano-banana-2-shots requires at least one reference image. "
                "Pass reference_images=[...URLs] or reference_files=[...local paths]."
            )
        }

    resolution = _validate_resolution(resolution or app.settings.default_resolution)
    w, h = resolve_dimensions(aspect_ratio, width, height)
    out_dir = Path(output_dir).expanduser() if output_dir else app.settings.output_dir

    # Upload local refs ONCE, reused across every shot
    shared_refs = await _resolve_inputs(app, reference_images, reference_files)

    records: list[JobRecord] = []
    for i, p in enumerate(prompts, 1):
        try:
            job_id = await app.client.submit_image_job(
                prompt=p, model="nano-banana-2-shots",
                width=w, height=h,
                aspect_ratio=aspect_ratio, resolution=resolution,
                batch_size=1, input_images=shared_refs,
                is_storyboard=True,
            )
            record = JobRecord(
                job_id=job_id, prompt=p, model="nano-banana-2-shots",
                resolution=resolution, aspect_ratio=aspect_ratio,
                width=w, height=h, batch_size=1,
                tags=[f"{tag_prefix}_{i:02d}"],
            )
            await app.registry.register(record)
            records.append(record)
        except HiggsfieldError as e:
            log.error("Storyboard shot %d submission failed: %s", i, e)

    job_ids = [r.job_id for r in records]

    if not wait:
        for r in records:
            asyncio.create_task(
                _run_job_to_completion(
                    app, r, output_dir=out_dir,
                    download=True, poll_timeout=poll_timeout,
                )
            )
        return {
            "job_ids": job_ids, "results": [],
            "message": f"Queued {len(records)} storyboard shots. Use queue_status to track.",
            "shared_refs": shared_refs,
        }

    async def _wrap(r: JobRecord) -> dict:
        try:
            final = await _run_job_to_completion(
                app, r, output_dir=out_dir,
                download=True, poll_timeout=poll_timeout,
            )
            return final.to_dict()
        except HiggsfieldError as e:
            rec = await app.registry.get(r.job_id)
            return {**rec.to_dict(), "error": str(e)}

    results = await asyncio.gather(*(_wrap(r) for r in records))
    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") in ("failed", "timeout"))
    return {
        "job_ids": job_ids,
        "results": results,
        "shared_refs": shared_refs,
        "summary": {"submitted": len(records), "completed": completed, "failed": failed},
    }


# ─────────────────────────────────────────────
# Video generation
# ─────────────────────────────────────────────

@mcp.tool()
async def generate_video(
    ctx: Context,
    prompt: str,
    model: str = "seedance",
    aspect_ratio: str = "16:9",
    duration: int = 5,
    resolution: str = "1080",
    input_images: Optional[list[str]] = None,
    input_files: Optional[list[str]] = None,
    input_video: Optional[str] = None,
    audio: bool = False,
    seed: Optional[int] = None,
    extra_params: Optional[dict] = None,
    wait: bool = True,
    download: bool = True,
    output_dir: Optional[str] = None,
    poll_timeout: int = 1200,
    tags: Optional[list[str]] = None,
) -> dict:
    """Submit a video generation in unlimited mode.

    Models: seedance, kling, kling2-6, sora2-video, veo3, veo3-1, veo3-speak,
    minimax-hailuo, wan2-2-video, wan2-5-video, wan2-6, image2video, infinite-talk
    plus 15+ more — see list_models(category="video").

    Args:
        prompt: text prompt
        model: video model ID (default seedance — note this requires input_image)
        aspect_ratio: "16:9" | "9:16" | "1:1" | "4:3" | "3:4" | "21:9"
        duration: seconds (typical 5–10, model-specific)
        resolution: "480" | "720" | "1080" — STRING, NO "p" suffix
        input_images: optional list of pre-uploaded image URLs (img2vid)
        input_files: optional list of LOCAL file paths to auto-upload then use as input
        input_video: optional video URL (vid2vid / extend)
        audio: whether to generate audio (model-dependent)
        seed: optional random seed
        extra_params: model-specific params merged into the request
                      (use this for kling's `model` sub-variant, veo3's `enhance_prompt`, etc.)

    Each video model has different required fields — see docs/MODEL_SCHEMAS.md.
    A 422 response will tell you exactly which params to add via extra_params.
    """
    app = _ctx(ctx)
    model = resolve_model(model)
    if model_category(model) not in ("video", "unknown"):
        return {"error": f"'{model}' is not a video model. Try list_models(category='video')."}
    out_dir = Path(output_dir).expanduser() if output_dir else app.settings.output_dir

    resolved_inputs = await _resolve_inputs(app, input_images, input_files)

    job_id = await app.client.submit_video_job(
        prompt=prompt, model=model, aspect_ratio=aspect_ratio,
        duration=duration, resolution=resolution,
        input_images=resolved_inputs, input_video=input_video,
        audio=audio, seed=seed, extra_params=extra_params,
    )
    record = JobRecord(
        job_id=job_id, prompt=prompt, model=model, resolution=resolution,
        aspect_ratio=aspect_ratio, width=0, height=0, batch_size=1,
        tags=list(tags or []),
    )
    await app.registry.register(record)

    if not wait:
        asyncio.create_task(
            _run_job_to_completion(
                app, record, output_dir=out_dir,
                download=download, poll_timeout=poll_timeout, default_ext="mp4",
            )
        )
        return (await app.registry.get(job_id)).to_dict()

    try:
        final = await _run_job_to_completion(
            app, record, output_dir=out_dir,
            download=download, poll_timeout=poll_timeout, default_ext="mp4",
        )
        return final.to_dict()
    except HiggsfieldError as e:
        rec = await app.registry.get(job_id)
        return {**rec.to_dict(), "error": str(e)}


# ─────────────────────────────────────────────
# Audio (text-to-speech)
# ─────────────────────────────────────────────

@mcp.tool()
async def generate_audio(
    ctx: Context,
    text: str,
    voice: Optional[str] = None,
    model: str = "text2speech",
    extra_params: Optional[dict] = None,
    wait: bool = True,
    download: bool = True,
    output_dir: Optional[str] = None,
    poll_timeout: int = 600,
) -> dict:
    """Submit an audio (text-to-speech) generation in unlimited mode."""
    app = _ctx(ctx)
    out_dir = Path(output_dir).expanduser() if output_dir else app.settings.output_dir

    job_id = await app.client.submit_audio_job(
        text=text, model=model, voice=voice, extra_params=extra_params,
    )
    record = JobRecord(
        job_id=job_id, prompt=text[:200], model=model, resolution="",
        aspect_ratio="", width=0, height=0, batch_size=1,
    )
    await app.registry.register(record)

    if not wait:
        asyncio.create_task(
            _run_job_to_completion(
                app, record, output_dir=out_dir,
                download=download, poll_timeout=poll_timeout, default_ext="mp3",
            )
        )
        return (await app.registry.get(job_id)).to_dict()

    try:
        final = await _run_job_to_completion(
            app, record, output_dir=out_dir,
            download=download, poll_timeout=poll_timeout, default_ext="mp3",
        )
        return final.to_dict()
    except HiggsfieldError as e:
        rec = await app.registry.get(job_id)
        return {**rec.to_dict(), "error": str(e)}


# ─────────────────────────────────────────────
# Generic generate (any model + raw params)
# ─────────────────────────────────────────────

@mcp.tool()
async def generate_raw(
    ctx: Context,
    model: str,
    params: dict,
    top_level: Optional[dict] = None,
    wait: bool = True,
    download: bool = True,
    output_dir: Optional[str] = None,
    poll_timeout: int = 600,
    default_ext: str = "png",
) -> dict:
    """Escape hatch for any model with a custom params dict.

    Use this for models with unusual parameter shapes (face-swap, character-swap,
    upscale, inpaint, etc.). The `use_unlim: true` flag is always injected.

    Args:
        model: model ID (e.g. "face-swap", "nano-banana-2-upscale")
        params: full params dict for the model
        top_level: extra top-level body fields
    """
    app = _ctx(ctx)
    out_dir = Path(output_dir).expanduser() if output_dir else app.settings.output_dir

    job_id = await app.client.submit_job(model=model, params=params, top_level=top_level)
    record = JobRecord(
        job_id=job_id, prompt=str(params.get("prompt", ""))[:200],
        model=resolve_model(model),
        resolution=str(params.get("resolution", "")),
        aspect_ratio=str(params.get("aspect_ratio", "")),
        width=int(params.get("width", 0) or 0),
        height=int(params.get("height", 0) or 0),
        batch_size=int(params.get("batch_size", 1) or 1),
    )
    await app.registry.register(record)

    if not wait:
        asyncio.create_task(
            _run_job_to_completion(
                app, record, output_dir=out_dir,
                download=download, poll_timeout=poll_timeout, default_ext=default_ext,
            )
        )
        return (await app.registry.get(job_id)).to_dict()

    try:
        final = await _run_job_to_completion(
            app, record, output_dir=out_dir,
            download=download, poll_timeout=poll_timeout, default_ext=default_ext,
        )
        return final.to_dict()
    except HiggsfieldError as e:
        rec = await app.registry.get(job_id)
        return {**rec.to_dict(), "error": str(e)}


# ─────────────────────────────────────────────
# Job tracking
# ─────────────────────────────────────────────

@mcp.tool()
async def check_job(ctx: Context, job_id: str, refresh: bool = True) -> dict:
    """Get the current state of a job (local registry + remote refresh)."""
    app = _ctx(ctx)
    try:
        record = await app.registry.get(job_id)
    except JobNotFoundError:
        record = None

    result: dict[str, Any] = {}
    if record is not None:
        result["local"] = record.to_dict()

    if refresh:
        try:
            remote = await app.client.get_job_status(job_id)
            result["remote"] = remote
        except HiggsfieldError as e:
            result["remote_error"] = str(e)

    if not result:
        return {"error": f"Job {job_id} not found locally and no refresh requested"}
    return result


@mcp.tool()
async def wait_for_job(
    ctx: Context,
    job_id: str,
    timeout: int = 600,
    download: bool = True,
    output_dir: Optional[str] = None,
    default_ext: str = "png",
) -> dict:
    """Block until the named job completes, then optionally download."""
    app = _ctx(ctx)
    try:
        record = await app.registry.get(job_id)
    except JobNotFoundError:
        record = JobRecord(
            job_id=job_id, prompt="(unknown)", model="(unknown)",
            resolution="(unknown)", aspect_ratio="", width=0, height=0,
            batch_size=1,
        )
        await app.registry.register(record)

    if record.status == "completed":
        return record.to_dict()

    out_dir = Path(output_dir).expanduser() if output_dir else app.settings.output_dir
    try:
        final = await _run_job_to_completion(
            app, record, output_dir=out_dir,
            download=download, poll_timeout=timeout, default_ext=default_ext,
        )
        return final.to_dict()
    except HiggsfieldError as e:
        rec = await app.registry.get(job_id)
        return {**rec.to_dict(), "error": str(e)}


@mcp.tool()
async def list_jobs(ctx: Context, status: Optional[str] = None, limit: int = 50) -> dict:
    """List jobs in the local registry."""
    app = _ctx(ctx)
    records = await app.registry.list(status=status)
    records.sort(key=lambda r: r.submitted_at, reverse=True)
    return {
        "count": len(records),
        "jobs": [r.to_dict() for r in records[:limit]],
        "snapshot": app.registry.snapshot(),
    }


@mcp.tool()
async def cancel_job(ctx: Context, job_id: str) -> dict:
    """Cancel an in-progress job. Verified live: DELETE /jobs/{id}.

    Useful for long-running video jobs (5–10 min) you started by accident or
    that you no longer need. Returns immediately; the job's terminal status
    will become "cancelled" (or "canceled") on the next poll.
    """
    app = _ctx(ctx)
    try:
        result = await app.client.cancel_job(job_id)
        try:
            await app.registry.update(job_id, status="failed", error="cancelled by user")
        except JobNotFoundError:
            pass
        return {"ok": True, "job_id": job_id, "response": result}
    except HiggsfieldError as e:
        return {"ok": False, "job_id": job_id, "error": str(e)}


@mcp.tool()
async def show_generations(ctx: Context, limit: int = 20) -> dict:
    """List recent generations from your Higgsfield account (server-side history).

    Equivalent to the official MCP's `show_generations`. Hits /jobs/accessible.
    """
    app = _ctx(ctx)
    try:
        return await app.client.list_recent_jobs(limit=limit)
    except HiggsfieldError as e:
        return {"error": str(e)}


@mcp.tool()
async def download_job_result(
    ctx: Context,
    job_id: str,
    output_dir: Optional[str] = None,
    overwrite: bool = False,
    default_ext: str = "png",
) -> dict:
    """Download a completed job's result(s) to disk."""
    app = _ctx(ctx)
    record = await app.registry.get(job_id)

    if record.status != "completed":
        return {"error": f"Job {job_id} status is '{record.status}', not completed"}

    urls = record.output_urls
    if not urls and record.last_status_payload:
        urls = HiggsfieldClient.extract_result_urls(record.last_status_payload)
    if not urls:
        remote = await app.client.get_job_status(job_id)
        urls = HiggsfieldClient.extract_result_urls(remote)
    if not urls:
        return {"error": f"No result URLs found for job {job_id}"}

    out_dir = Path(output_dir).expanduser() if output_dir else app.settings.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    for i, url in enumerate(urls):
        ext = _ext_from_url(url, default_ext)
        out_path = out_dir / _safe_filename(record.prompt, record.job_id, i, ext)
        if out_path.exists() and not overwrite:
            saved.append(str(out_path) + " (already exists, skipped)")
            continue
        try:
            data = await app.client.download(url)
            out_path.write_bytes(data)
            saved.append(str(out_path))
        except Exception as e:
            saved.append(f"FAILED: {url} → {e}")

    await app.registry.update(
        job_id, output_urls=urls,
        output_paths=[Path(s) for s in saved if not s.startswith("FAILED")],
    )
    return {"job_id": job_id, "urls": urls, "saved": saved}


# ─────────────────────────────────────────────
# Workspaces
# ─────────────────────────────────────────────

@mcp.tool()
async def list_workspaces(ctx: Context) -> dict:
    """List your workspaces (teams). Equivalent to official MCP's `list_workspaces`."""
    app = _ctx(ctx)
    try:
        return {"workspaces": await app.client.list_workspaces()}
    except HiggsfieldError as e:
        return {"error": str(e)}


@mcp.tool()
async def workspace_details(ctx: Context) -> dict:
    """Get the active workspace's details (id, name, type, role)."""
    app = _ctx(ctx)
    try:
        return await app.client.get_workspace_details()
    except HiggsfieldError as e:
        return {"error": str(e)}


@mcp.tool()
async def workspace_wallet(ctx: Context) -> dict:
    """Get the active workspace's credit balance."""
    app = _ctx(ctx)
    try:
        return await app.client.get_workspace_wallet()
    except HiggsfieldError as e:
        return {"error": str(e)}


@mcp.tool()
async def workspace_members(ctx: Context) -> dict:
    """List members of the active workspace."""
    app = _ctx(ctx)
    try:
        return await app.client.get_workspace_members()
    except HiggsfieldError as e:
        return {"error": str(e)}


@mcp.tool()
async def workspace_usage(ctx: Context) -> dict:
    """Get the credit-usage chart for the active workspace."""
    app = _ctx(ctx)
    try:
        return await app.client.get_workspace_usage()
    except HiggsfieldError as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
# Media library
# ─────────────────────────────────────────────

@mcp.tool()
async def show_medias(
    ctx: Context,
    cursor: Optional[int] = None,
    limit: int = 20,
    kind: Optional[str] = None,
) -> dict:
    """List your media library (images & videos) with pagination.

    Equivalent to official MCP's `show_medias`. Hits /media/accessible.

    Args:
        cursor: pagination cursor from a previous response
        limit: page size
        kind: optional filter (e.g. "image" or "video")
    """
    app = _ctx(ctx)
    try:
        return await app.client.list_media(cursor=cursor, limit=limit, kind=kind)
    except HiggsfieldError as e:
        return {"error": str(e)}


@mcp.tool()
async def media_upload(
    ctx: Context,
    file_path: str,
    content_type: Optional[str] = None,
) -> dict:
    """Upload a local image/video for use as input in subsequent generations.

    Args:
        file_path: absolute or ~ path to the local file
        content_type: MIME type override (auto-detected from extension by default)

    Returns the uploaded media descriptor — pass its URL to input_images
    on a subsequent generate_image / generate_video call.
    """
    app = _ctx(ctx)
    path = Path(file_path).expanduser()
    if not path.is_file():
        return {"error": f"File not found: {path}"}

    ct = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        data = path.read_bytes()
        result = await app.client.upload_media(data, filename=path.name, content_type=ct)
        return {"ok": True, "uploaded": result}
    except HiggsfieldError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"upload failed: {e}"}


@mcp.tool()
async def media_status(ctx: Context, media_ids: list[str]) -> dict:
    """Check status of media items by ID."""
    app = _ctx(ctx)
    try:
        return await app.client.media_status(media_ids)
    except HiggsfieldError as e:
        return {"error": str(e)}


@mcp.tool()
async def media_download_batch(ctx: Context, media_ids: list[str]) -> dict:
    """Get bulk download URLs for media items by ID."""
    app = _ctx(ctx)
    try:
        return await app.client.media_download_batch(media_ids)
    except HiggsfieldError as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
# Assets / favourites
# ─────────────────────────────────────────────

@mcp.tool()
async def list_assets(ctx: Context, limit: int = 20, cursor: Optional[int] = None) -> dict:
    """List your assets."""
    app = _ctx(ctx)
    try:
        return await app.client.list_assets(limit=limit, cursor=cursor)
    except HiggsfieldError as e:
        return {"error": str(e)}


@mcp.tool()
async def list_favourites(ctx: Context) -> dict:
    """List your favourited assets."""
    app = _ctx(ctx)
    try:
        return await app.client.list_favourites()
    except HiggsfieldError as e:
        return {"error": str(e)}


@mcp.tool()
async def like_asset(ctx: Context, asset_id: str) -> dict:
    """Add an asset to favourites."""
    app = _ctx(ctx)
    try:
        return await app.client.like_asset(asset_id)
    except HiggsfieldError as e:
        return {"error": str(e)}


@mcp.tool()
async def unlike_asset(ctx: Context, asset_id: str) -> dict:
    """Remove an asset from favourites."""
    app = _ctx(ctx)
    try:
        return await app.client.unlike_asset(asset_id)
    except HiggsfieldError as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
