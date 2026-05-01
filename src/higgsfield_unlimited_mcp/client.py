"""Higgsfield API client — all HTTP calls go through here.

Endpoint reference (verified against the live web app on 2026-04-29):

  Auth (Clerk):
    POST https://clerk.higgsfield.ai/v1/client/sessions/{session_id}/tokens
                                                              → { jwt }

  Generation jobs (use_unlim: true unlocks unlimited mode):
    POST https://fnf.higgsfield.ai/jobs/{model}              → submit
    GET  https://fnf.higgsfield.ai/jobs/{job_id}/status      → poll
    GET  https://fnf.higgsfield.ai/jobs/accessible           → recent jobs

  Account / plan info:
    GET  https://fnf.higgsfield.ai/user                      → plan + credit balances
    GET  https://fnf.higgsfield.ai/user/profile              → user profile
    GET  https://fnf.higgsfield.ai/user/features             → feature flags
    GET  https://fnf.higgsfield.ai/user/meta                 → meta

  Workspaces (Higgsfield's "team" / "workspace" feature):
    GET  https://fnf.higgsfield.ai/workspaces                → list workspaces
    GET  https://fnf.higgsfield.ai/workspaces/details        → current workspace
    GET  https://fnf.higgsfield.ai/workspaces/wallet         → credit balance
    GET  https://fnf.higgsfield.ai/workspaces/members        → members
    GET  https://fnf.higgsfield.ai/workspaces/usage-stats    → usage chart
    POST https://fnf.higgsfield.ai/workspaces/rename
    POST https://fnf.higgsfield.ai/workspaces/leave

  Media library:
    GET  https://fnf.higgsfield.ai/media/accessible          → media items (paginated)
    POST https://fnf.higgsfield.ai/media/batch               → batch operations
    POST https://fnf.higgsfield.ai/media/download-batch      → bulk download
    GET  https://fnf.higgsfield.ai/media/status              → status

  Concurrency:
    GET  https://fnf.higgsfield.ai/concurrent-boost-credits/state    → tier info
    POST https://fnf.higgsfield.ai/concurrent-boost-credits/enable
    POST https://fnf.higgsfield.ai/concurrent-boost-credits/disable

  Assets:
    GET  https://fnf.higgsfield.ai/assets                    → assets
    GET  https://fnf.higgsfield.ai/assets/favourites         → favorites
    POST https://fnf.higgsfield.ai/assets/favourites/like
    POST https://fnf.higgsfield.ai/assets/favourites/unlike
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from curl_cffi.requests import AsyncSession

from .auth import TokenManager
from .config import resolve_model
from .errors import HiggsfieldError, JobFailedError, JobSubmitError, JobTimeoutError

log = logging.getLogger(__name__)

API_BASE = "https://fnf.higgsfield.ai"
DEFAULT_HEADERS = {
    "Origin": "https://higgsfield.ai",
    "Referer": "https://higgsfield.ai/",
}

POLL_INITIAL_INTERVAL = 3
POLL_MAX_INTERVAL = 15
POLL_BACKOFF_STEP = 2


class HiggsfieldClient:
    """Async client for the Higgsfield generation API in unlimited mode."""

    def __init__(self, http: AsyncSession, tokens: TokenManager):
        self._http = http
        self._tokens = tokens

    # ─────────── auth helper ───────────

    async def _auth_headers(self, force_refresh: bool = False) -> dict[str, str]:
        token = await self._tokens.get_token(self._http, force=force_refresh)
        return {**DEFAULT_HEADERS, "Authorization": f"Bearer {token}"}

    async def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        retry_on_401: bool = True,
        raw_data: Optional[Any] = None,
        extra_headers: Optional[dict] = None,
    ) -> dict | str:
        """Generic HTTP request with auth + 401 retry. Returns parsed JSON or raw text."""
        url = path_or_url if path_or_url.startswith("http") else f"{API_BASE}{path_or_url}"
        headers = await self._auth_headers()
        if json is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        # curl_cffi AsyncSession: returns Response directly (no context manager).
        resp = await self._http.request(
            method, url, json=json, params=params, data=raw_data, headers=headers
        )
        if resp.status_code == 401 and retry_on_401:
            log.warning("Got 401 — refreshing token and retrying once")
            await self._tokens.invalidate()
            return await self._request(
                method, path_or_url, json=json, params=params,
                retry_on_401=False, raw_data=raw_data, extra_headers=extra_headers,
            )
        text = resp.text
        if resp.status_code >= 400:
            raise JobSubmitError(
                f"{method} {url} → HTTP {resp.status_code}: {text[:300]}"
            )
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                return resp.json()
            except Exception:
                return text
        return text

    # ─────────── generation ───────────

    async def submit_job(
        self,
        *,
        model: str,
        params: dict[str, Any],
        top_level: Optional[dict[str, Any]] = None,
    ) -> str:
        """Submit a generation job in unlimited mode. Returns job_id.

        Args:
            model: model ID (canonical hyphenated form, e.g. "nano-banana-2", "veo3", "sora2-video")
            params: model-specific params (will have use_unlim=True injected)
            top_level: extra fields to merge at the top level of the body

        The body shape always has use_unlim=True at both levels, matching
        the official web app's request shape.
        """
        canonical = resolve_model(model)
        body_params = dict(params)
        body_params.setdefault("use_unlim", True)

        body: dict[str, Any] = {
            "params": body_params,
            "use_unlim": True,
            "use_seedream_bonus": False,
        }
        if top_level:
            body.update(top_level)

        url = f"{API_BASE}/jobs/{canonical}"
        data = await self._request("POST", url, json=body)
        if not isinstance(data, dict):
            raise JobSubmitError(f"Unexpected non-JSON submit response: {data!r}")
        # Response shape (verified live):
        #   { "id": <project_id>, "job_sets": [{ "id": <job_set_id>,
        #     "jobs": [{ "id": <job_id>, ... }] }] }
        # The job_id is what /jobs/{id}/status accepts; the project/job_set IDs 404.
        job_id: Optional[str] = None
        job_sets = data.get("job_sets") or []
        if job_sets and isinstance(job_sets, list):
            jobs = job_sets[0].get("jobs") if isinstance(job_sets[0], dict) else None
            if jobs and isinstance(jobs, list) and isinstance(jobs[0], dict):
                job_id = jobs[0].get("id")
        # Fallback for older/different shapes
        if not job_id:
            job_id = data.get("job_id") or data.get("id")
        if not job_id:
            raise JobSubmitError(f"No job_id in response: {data}")
        log.info("Submitted job %s (model=%s)", job_id, canonical)
        return job_id

    async def submit_image_job(
        self,
        *,
        prompt: str,
        model: str,
        width: int,
        height: int,
        aspect_ratio: str,
        resolution: str,
        batch_size: int = 1,
        input_images: Optional[list[str]] = None,
        is_storyboard: bool = False,
        is_zoom_control: bool = False,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> str:
        """Convenience wrapper for image generation."""
        params: dict[str, Any] = {
            "prompt": prompt,
            "input_images": input_images or [],
            "width": width,
            "height": height,
            "batch_size": batch_size,
            "aspect_ratio": aspect_ratio,
            "is_storyboard": is_storyboard,
            "is_zoom_control": is_zoom_control,
            "resolution": resolution,
        }
        if extra_params:
            params.update(extra_params)
        return await self.submit_job(model=model, params=params)

    async def submit_video_job(
        self,
        *,
        prompt: str,
        model: str,
        aspect_ratio: str = "16:9",
        duration: int = 5,
        resolution: str = "1080p",
        input_images: Optional[list[str]] = None,
        input_video: Optional[str] = None,
        seed: Optional[int] = None,
        audio: bool = False,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> str:
        """Convenience wrapper for video generation.

        Note: video models accept different parameter sets. This builds the
        most common shape; pass extra_params for model-specific overrides.
        """
        params: dict[str, Any] = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "duration": duration,
            "resolution": resolution,
            "audio": audio,
        }
        if input_images:
            params["input_images"] = input_images
        if input_video:
            params["input_video"] = input_video
        if seed is not None:
            params["seed"] = seed
        if extra_params:
            params.update(extra_params)
        return await self.submit_job(model=model, params=params)

    async def submit_audio_job(
        self,
        *,
        text: str,
        model: str = "text2speech",
        voice: Optional[str] = None,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> str:
        params: dict[str, Any] = {"text": text}
        if voice:
            params["voice"] = voice
        if extra_params:
            params.update(extra_params)
        return await self.submit_job(model=model, params=params)

    # ─────────── status / polling ───────────

    async def get_job_status(self, job_id: str) -> dict:
        """GET /jobs/{id} — full job record including params + results.

        Note: there's also /jobs/{id}/status which is lighter but does NOT
        contain the result URLs after completion. We use the full endpoint.
        """
        return await self._request("GET", f"/jobs/{job_id}")  # type: ignore[return-value]

    async def get_job_status_light(self, job_id: str) -> dict:
        """GET /jobs/{id}/status — lighter response (no results), for polling only."""
        return await self._request("GET", f"/jobs/{job_id}/status")  # type: ignore[return-value]

    async def poll_job(
        self,
        job_id: str,
        *,
        timeout: int = 600,
        on_status: Optional[Any] = None,
    ) -> dict:
        elapsed = 0
        interval = POLL_INITIAL_INTERVAL
        while elapsed < timeout:
            data = await self.get_job_status(job_id)
            status = data.get("status", "unknown")
            if on_status is not None:
                try:
                    await on_status(data)
                except Exception:
                    log.exception("on_status callback failed")
            if status == "completed":
                return data
            if status in ("failed", "error", "cancelled", "canceled"):
                raise JobFailedError(f"Job {job_id} ended in '{status}': {data}")
            await asyncio.sleep(interval)
            elapsed += interval
            interval = min(interval + POLL_BACKOFF_STEP, POLL_MAX_INTERVAL)
        raise JobTimeoutError(f"Job {job_id} did not finish within {timeout}s")

    async def list_recent_jobs(self, limit: int = 20) -> dict:
        """GET /jobs/accessible — your most recent generations."""
        return await self._request("GET", "/jobs/accessible", params={"limit": limit})  # type: ignore[return-value]

    async def cancel_job(self, job_id: str) -> dict:
        """DELETE /jobs/{id} — cancel an in-progress job. Verified live."""
        return await self._request("DELETE", f"/jobs/{job_id}")  # type: ignore[return-value]

    # ─────────── account / plan ───────────

    async def get_user(self) -> dict:
        """GET /user — plan, credit balances, has_unlim flag."""
        return await self._request("GET", "/user")  # type: ignore[return-value]

    async def get_user_profile(self) -> dict:
        return await self._request("GET", "/user/profile")  # type: ignore[return-value]

    async def get_user_features(self) -> dict:
        return await self._request("GET", "/user/features")  # type: ignore[return-value]

    async def get_user_meta(self) -> dict:
        return await self._request("GET", "/user/meta")  # type: ignore[return-value]

    # ─────────── workspaces ───────────

    async def list_workspaces(self) -> list[dict]:
        return await self._request("GET", "/workspaces")  # type: ignore[return-value]

    async def get_workspace_details(self) -> dict:
        return await self._request("GET", "/workspaces/details")  # type: ignore[return-value]

    async def get_workspace_wallet(self) -> dict:
        return await self._request("GET", "/workspaces/wallet")  # type: ignore[return-value]

    async def get_workspace_members(self) -> dict:
        return await self._request("GET", "/workspaces/members")  # type: ignore[return-value]

    async def get_workspace_usage(self) -> dict:
        return await self._request("GET", "/workspaces/credit-ledger/usage-chart")  # type: ignore[return-value]

    async def rename_workspace(self, name: str) -> dict:
        return await self._request("POST", "/workspaces/rename", json={"name": name})  # type: ignore[return-value]

    async def get_workspace_invites(self) -> dict:
        return await self._request("GET", "/workspaces/invites")  # type: ignore[return-value]

    # ─────────── media library ───────────

    async def list_media(
        self,
        cursor: Optional[int] = None,
        limit: int = 20,
        kind: Optional[str] = None,
    ) -> dict:
        """GET /media/accessible — paginated. Returns { has_more, cursor, items }."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if kind:
            params["kind"] = kind
        return await self._request("GET", "/media/accessible", params=params)  # type: ignore[return-value]

    async def media_status(self, media_ids: list[str]) -> dict:
        return await self._request("POST", "/media/status", json={"media_ids": media_ids})  # type: ignore[return-value]

    async def media_batch(self, payload: dict) -> dict:
        return await self._request("POST", "/media/batch", json=payload)  # type: ignore[return-value]

    async def media_download_batch(self, media_ids: list[str]) -> dict:
        return await self._request("POST", "/media/download-batch", json={"media_ids": media_ids})  # type: ignore[return-value]

    # ─────────── assets / favourites ───────────

    async def list_assets(self, limit: int = 20, cursor: Optional[int] = None) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", "/assets", params=params)  # type: ignore[return-value]

    async def list_favourites(self) -> dict:
        return await self._request("GET", "/assets/favourites")  # type: ignore[return-value]

    async def like_asset(self, asset_id: str) -> dict:
        return await self._request("POST", "/assets/favourites/like", json={"asset_id": asset_id})  # type: ignore[return-value]

    async def unlike_asset(self, asset_id: str) -> dict:
        return await self._request("POST", "/assets/favourites/unlike", json={"asset_id": asset_id})  # type: ignore[return-value]

    # ─────────── concurrency ───────────

    async def get_concurrent_state(self) -> dict:
        return await self._request("GET", "/concurrent-boost-credits/state")  # type: ignore[return-value]

    async def enable_concurrent_boost(self, product_id: str) -> dict:
        return await self._request(
            "POST", "/concurrent-boost-credits/enable",
            json={"product_id": product_id},
        )  # type: ignore[return-value]

    async def disable_concurrent_boost(self) -> dict:
        return await self._request("POST", "/concurrent-boost-credits/disable", json={})  # type: ignore[return-value]

    # ─────────── media upload ───────────

    async def upload_media(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str = "image/png",
    ) -> dict:
        """POST /upload — multipart upload of an image/video for use as input.

        Returns the uploaded media descriptor (typically with id/url) that can
        be passed as input_images=[url] to subsequent generation calls.
        """
        from curl_cffi import CurlMime

        url = f"{API_BASE}/upload"

        def _build_mime() -> CurlMime:
            m = CurlMime()
            m.addpart(name="file", filename=filename, data=file_bytes, content_type=content_type)
            return m

        headers = await self._auth_headers()
        mp = _build_mime()
        resp = await self._http.post(url, multipart=mp, headers=headers)
        if resp.status_code == 401:
            await self._tokens.invalidate()
            headers = await self._auth_headers()
            mp = _build_mime()
            resp = await self._http.post(url, multipart=mp, headers=headers)
        if resp.status_code >= 400:
            raise JobSubmitError(f"upload → HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ─────────── downloads & result helpers ───────────

    async def download(self, image_url: str) -> bytes:
        resp = await self._http.get(image_url)
        if resp.status_code >= 400:
            raise JobSubmitError(f"download → HTTP {resp.status_code}")
        return resp.content

    @staticmethod
    def extract_result_urls(result: dict) -> list[str]:
        """Pull image/video/audio URLs out of a completed job's payload.

        Verified shape (live, nano-banana-2):
            { "results": { "raw": { "type": "image", "url": "https://..." } } }

        Other observed shapes handled defensively:
            results: { rawUrl: "..." } or { url: "..." }
            results: [{ url: "..." }, ...]
            results: { items: [{ url: "..." }, ...] }
        """
        urls: list[str] = []

        def _add(u: Any) -> None:
            if isinstance(u, str) and u and u not in urls:
                urls.append(u)

        def _from_dict(d: dict) -> None:
            for key in ("url", "rawUrl", "raw_url", "image_url", "video_url", "audio_url"):
                if isinstance(d.get(key), str):
                    _add(d[key])
            # Nested {raw: {url: "..."}}-style wrappers
            for wrapper_key in ("raw", "thumbnail", "preview", "watermarked"):
                w = d.get(wrapper_key)
                if isinstance(w, dict):
                    _from_dict(w)
            # Collections
            for col_key in ("items", "images", "videos", "media", "frames"):
                items = d.get(col_key)
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict):
                            _from_dict(it)
                        elif isinstance(it, str):
                            _add(it)

        results = result.get("results")
        if isinstance(results, dict):
            _from_dict(results)
        elif isinstance(results, list):
            for it in results:
                if isinstance(it, dict):
                    _from_dict(it)
                elif isinstance(it, str):
                    _add(it)

        # Top-level fallbacks
        for k in ("output_url", "url", "rawUrl", "video_url", "audio_url"):
            v = result.get(k)
            if isinstance(v, str):
                _add(v)

        return urls
