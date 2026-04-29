# Higgsfield Unlimited MCP

A complete MCP server for [Higgsfield AI](https://higgsfield.ai) that calls the generation API in **unlimited mode**, bypassing the credit system used by the official Higgsfield MCP. Auth uses your existing browser session (Clerk JWT auto-refreshed every 4 minutes).

**What's covered:** image generation (32 models), video generation (31 models), audio (TTS), storyboards (multi-shot continuity), workspaces, media library, assets, favourites, generation history, account/plan info, job cancellation — everything the official Higgsfield MCP exposes, plus video, audio, storyboard, and cancel that the official MCP doesn't.

> Private use only. Requires an active Higgsfield subscription with unlimited mode enabled.

---

## Tools (31 total)

### Auth & account
| Tool | Description |
|---|---|
| `auth_status` | Verify Clerk credentials by fetching a fresh JWT. |
| `account_info` | Plan info, all credit balances, `has_unlim` flag. |
| `concurrent_state` | Concurrent-slot tier (4/8/12/16). |
| `queue_status` | In-process job registry snapshot. |

### Models
| Tool | Description |
|---|---|
| `list_models` | All 60+ generation models, optionally filtered by `category` (`image` / `video` / `audio`). |
| `get_aspect_dimensions` | Canonical pixel dims for an aspect ratio. |

### Image generation
| Tool | Description |
|---|---|
| `generate_image` | Single image — 32 models including nano-banana-2, flux-2, seedream-v4-5, openai-hazel, reve, z-image. Supports `input_files` for local-file auto-upload. |
| `generate_image_batch` | Queue many prompts; runs up to `max_concurrent` in parallel. |
| `generate_storyboard` | Multi-shot storyboard with character/style continuity (uses `nano-banana-2-shots` — Higgsfield's dedicated multi-shot endpoint). |

### Video generation
| Tool | Description |
|---|---|
| `generate_video` | 31 models including seedance, kling/kling2-6, sora2-video, veo3 / veo3-1, minimax-hailuo, wan2-2-video / wan2-5-video / wan2-6, image2video, infinite-talk. |

### Audio
| Tool | Description |
|---|---|
| `generate_audio` | Text-to-speech (`text2speech` model). |

### Generic / advanced
| Tool | Description |
|---|---|
| `generate_raw` | Escape hatch for any model with a custom params dict (face-swap, character-swap, upscale, inpaint, etc.). |

### Job management
| Tool | Description |
|---|---|
| `check_job` | Poll a single job (local + remote). |
| `wait_for_job` | Block until a job finishes; optionally download. |
| `cancel_job` | Cancel an in-progress job (`DELETE /jobs/{id}`). |
| `list_jobs` | List jobs in the local registry. |
| `download_job_result` | Re-fetch a completed job's results to disk. |
| `show_generations` | Server-side recent generation history (your account's `/jobs/accessible`). |

### Workspaces
| Tool | Description |
|---|---|
| `list_workspaces` | List all your workspaces. |
| `workspace_details` | Active workspace info (id, name, type, role). |
| `workspace_wallet` | Credit balance. |
| `workspace_members` | Members of the active workspace. |
| `workspace_usage` | Credit-usage chart. |

### Media library
| Tool | Description |
|---|---|
| `show_medias` | Paginated media library (images + videos). |
| `media_upload` | Upload a local image/video for use as `input_images` on subsequent generations. |
| `media_status` | Check status of media items by ID. |
| `media_download_batch` | Bulk download URLs by media ID. |

### Assets & favourites
| Tool | Description |
|---|---|
| `list_assets` | List your assets. |
| `list_favourites` | List your favourited assets. |
| `like_asset` / `unlike_asset` | Add/remove asset from favourites. |

---

## Setup

### 1. Get your credentials (one-time, ~2 min)

Open `https://higgsfield.ai` in Chrome while logged in.

**`__client` cookie:**
- DevTools → Application → Cookies → `https://higgsfield.ai`
- Copy the value of the `__client` cookie

**Session ID:**
- DevTools → Console → run: `window.Clerk.session.id`
- Copy the result (starts with `sess_`)

### 2. Install

**From the local clone:**
```bash
cd higgsfield-unlimited-mcp
pip install -e .
```

**From a private GitHub repo (after pushing):**
```bash
pip install git+https://<token>@github.com/<you>/higgsfield-unlimited-mcp.git
```

Or `uvx` for ephemeral runs:
```bash
uvx --from git+https://<token>@github.com/<you>/higgsfield-unlimited-mcp.git higgsfield-unlimited-mcp
```

### 3. Configure

Copy `.env.example` to `.env` and fill in your credentials, **or** pass them as env vars in your MCP client config.

```bash
cp .env.example .env
# then edit .env
```

### 4. Register with Claude Code

```json
{
  "mcpServers": {
    "higgsfield-unlimited": {
      "command": "python",
      "args": ["-m", "higgsfield_unlimited_mcp"],
      "env": {
        "HIGGSFIELD_CLERK_COOKIE": "<paste cookie value>",
        "HIGGSFIELD_SESSION_ID": "sess_xxxxxxxxxxxxx",
        "HIGGSFIELD_MAX_CONCURRENT": "4",
        "HIGGSFIELD_DEFAULT_MODEL": "nano-banana-2",
        "HIGGSFIELD_DEFAULT_RESOLUTION": "2k"
      }
    }
  }
}
```

Or via CLI:
```bash
claude mcp add higgsfield-unlimited -s user -- python -m higgsfield_unlimited_mcp
```

### 5. Verify

**Standalone smoke test (recommended after install):**
```bash
higgsfield-unlimited-verify              # full check: auth + GETs + 1 test gen + download
higgsfield-unlimited-verify --skip-generate  # auth + GETs only, no submission
higgsfield-unlimited-verify --keep-output    # keep the test image after success
```
Or via Python:
```bash
python -m higgsfield_unlimited_mcp.verify
```

**Inside Claude Code:**
```
> Use higgsfield-unlimited to check auth_status, then account_info.
```

---

## Usage examples

**Storyboard batch (no waiting):**
```
Generate this 11-shot storyboard at 2k 9:16, fire-and-forget.
```
Calls `generate_image_batch(prompts=[...], wait=False, tag_prefix="shot")`, then `queue_status` / `list_jobs(status="completed")`.

**Single video at Veo 3 1080p:**
```
Generate a 5-second 16:9 video with Veo 3: "drone shot of a desert pyramid at sunset".
```
Calls `generate_video(prompt=..., model="veo3", duration=5, resolution="1080p")`.

**Image-to-video with auto-upload (one step):**
```
Animate ./Assets/Character\ 1.png with seedance for 5s, 720, 16:9.
```
Calls `generate_video(model="seedance", input_files=["./Assets/Character 1.png"], duration=5, resolution="720", aspect_ratio="16:9")` — the file is uploaded server-side and used as the input image automatically.

**Storyboard with character continuity:**
```
Use ./Assets/Character\ 1.png as the reference and generate this 5-shot storyboard at 2k 9:16.
```
Calls `generate_storyboard(prompts=[...], reference_files=["./Assets/Character 1.png"], resolution="2k", aspect_ratio="9:16")` — uploads the ref once and shares it across every shot via `nano-banana-2-shots`.

**Cancel a runaway video:**
```
Cancel job <id>.
```
Calls `cancel_job(job_id="...")` (DELETE /jobs/{id}).

**Inspect plan & balances:**
```
What's my Higgsfield plan?
```
Calls `account_info` — returns `has_unlim`, plan type, all per-feature credit balances.

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `HIGGSFIELD_CLERK_COOKIE` | *(required)* | The `__client` cookie from your browser. |
| `HIGGSFIELD_SESSION_ID` | *(required)* | `window.Clerk.session.id`. |
| `HIGGSFIELD_MAX_CONCURRENT` | `4` | Max parallel jobs (match your plan tier). |
| `HIGGSFIELD_DEFAULT_MODEL` | `nano-banana-2` | Image model when not specified. |
| `HIGGSFIELD_DEFAULT_RESOLUTION` | `2k` | One of `1k`, `2k`, `4k`. |
| `HIGGSFIELD_OUTPUT_DIR` | `./higgsfield_output` | Default download directory. |
| `HIGGSFIELD_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

---

## How auth works

Higgsfield's web app authenticates with a short-lived JWT (~5 min TTL) issued by Clerk. The long-lived `__client` cookie lets us mint fresh JWTs as needed:

```
POST https://clerk.higgsfield.ai/v1/client/sessions/{session_id}/tokens
Cookie: __client=<your cookie>
→ { "jwt": "..." }
```

The MCP server caches the JWT and proactively refreshes every 4 minutes. On a 401, it invalidates and retries once.

The generation call differs from the paid path only by `"use_unlim": true` in both the `params` object and the top-level body:

```
POST https://fnf.higgsfield.ai/jobs/{model}
Authorization: Bearer <jwt>
{
  "params": { ..., "use_unlim": true },
  "use_unlim": true,
  "use_seedream_bonus": false
}
```

See [`docs/EXTENDING.md`](docs/EXTENDING.md) for how to add new endpoints, and [`docs/MODEL_SCHEMAS.md`](docs/MODEL_SCHEMAS.md) for per-model required-field reference.

> **Important:** Each video model has different required fields (e.g. `seedance` needs `input_image`, `kling` needs a sub-variant `model` ID, `veo3` needs `enhance_prompt` and `seed`). When `generate_video` returns a 422 error, the message tells you exactly which fields to add via `extra_params` — or use `generate_raw` to pass the params dict directly.

---

## Discovered API surface

The full endpoint map (all under `https://fnf.higgsfield.ai`):

```
Generation:
  POST /jobs/{model}                       — submit (60+ models)
  GET  /jobs/{job_id}/status               — poll
  GET  /jobs/accessible                    — recent jobs

Account:
  GET  /user                               — plan + credit balances
  GET  /user/profile                       — profile
  GET  /user/features                      — feature flags
  GET  /user/meta

Workspaces (39 endpoints):
  GET  /workspaces                         — list
  GET  /workspaces/details                 — current workspace
  GET  /workspaces/wallet                  — credit balance
  GET  /workspaces/members
  GET  /workspaces/credit-ledger/usage-chart
  POST /workspaces/rename
  …and 33 more (billing, invites, plans, payment cards)

Media:
  GET  /media/accessible                   — paginated library
  POST /media/batch
  POST /media/download-batch
  GET  /media/status

Assets:
  GET  /assets, /assets/favourites
  POST /assets/favourites/like, /unlike
  POST /assets/publish/v2, /unpublish/v2

Concurrency:
  GET  /concurrent-boost-credits/state
  POST /concurrent-boost-credits/enable, /disable, /purchase

Upload:
  POST /upload                             — multipart media upload
```

---

## Security

- **Never commit `.env`.** It's in `.gitignore` already.
- The cookie + session ID are equivalent to your logged-in browser. Treat them as credentials.
- For team use: each member sets up with their own Higgsfield account.

---

## License

MIT.
