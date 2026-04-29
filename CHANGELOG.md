# Changelog

## [0.1.0] — Initial release

### Discovery
- Reverse-engineered the Higgsfield API by intercepting fetch/XHR in the live web app
- Identified `use_unlim: true` flag (at both `params.use_unlim` and top-level body) as the unlimited-mode unlock
- Mapped 60+ generation models across image, video, audio categories
- Validated 6 GET endpoints + 1 POST endpoint with live HTTP calls

### Tools (31 total)
- **Auth & account:** `auth_status`, `account_info`, `concurrent_state`, `queue_status`
- **Models:** `list_models`, `get_aspect_dimensions`
- **Image:** `generate_image`, `generate_image_batch`, `generate_storyboard`
- **Video:** `generate_video`
- **Audio:** `generate_audio`
- **Generic:** `generate_raw` (escape hatch for any model)
- **Job tracking:** `check_job`, `wait_for_job`, `cancel_job`, `list_jobs`, `download_job_result`, `show_generations`
- **Workspaces:** `list_workspaces`, `workspace_details`, `workspace_wallet`, `workspace_members`, `workspace_usage`
- **Media:** `show_medias`, `media_upload`, `media_status`, `media_download_batch`
- **Assets:** `list_assets`, `list_favourites`, `like_asset`, `unlike_asset`

### Architecture
- Async `aiohttp` throughout (no thread-pool blocking)
- `asyncio.Semaphore`-throttled queue matching the user's concurrent-slot tier (4/8/12/16)
- Clerk JWT auto-refresh every 4 minutes + invalidate-and-retry on 401
- Polling backoff: 3s → 15s capped, 600s default timeout (1200s for video)
- In-process registry tracks every submitted job across MCP tool calls
- Local-file auto-upload via `input_files` param on image/video/storyboard tools
- Robust result URL extraction handling `results.raw.url`, nested wrappers, and 6 collection shapes

### Validation
- `verify.py` smoke test (10 steps end-to-end): auth → GETs → submit → poll → download → integrity check
- Available as `higgsfield-unlimited-verify` console script

### Documentation
- `README.md` — full tool reference, setup, usage examples
- `docs/EXTENDING.md` — how to discover and wire new endpoints
- `docs/MODEL_SCHEMAS.md` — per-model required field reference (verified via 422 responses)
