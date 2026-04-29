# Model schema reference

Each generation model has its own server-side Pydantic schema. The MCP's
`generate_image` / `generate_video` / `generate_audio` tools build the most
common shape, but **model-specific required fields differ**. When a submission
fails with HTTP 422, the response tells you exactly which fields are missing —
pass them via `extra_params` (or use `generate_raw` for full control).

## Verified live (HTTP 200)

### `nano-banana-2` (image)
```json
{
  "params": {
    "prompt": "...",
    "input_images": [],
    "width": 768,
    "height": 1376,
    "batch_size": 1,
    "aspect_ratio": "9:16",
    "is_storyboard": false,
    "is_zoom_control": false,
    "use_unlim": true,
    "resolution": "1k"        // "1k" | "2k" | "4k"
  },
  "use_unlim": true,
  "use_seedream_bonus": false
}
```
Response shape:
```json
{
  "id": "<project_id>",
  "job_sets": [
    { "id": "<job_set_id>",
      "jobs": [{ "id": "<job_id>", "status": "queued" }] }
  ]
}
```
The `<job_id>` (NOT `<project_id>`) is what `/jobs/{id}` and `/jobs/{id}/status` accept.

Result lives at `data.results.raw.url` once status is `completed`.

---

## Schemas inferred from 422 validation errors

These are the **required** fields per model (additional optional fields likely exist).
Use `generate_raw(model="...", params={...})` to pass them precisely.

### `seedance` (image-to-video)
```json
{
  "prompt": "...",
  "width": 1280,
  "height": 720,
  "resolution": "720",        // "480" | "720" | "1080"  (NO "p" suffix)
  "input_image": "<url>",     // REQUIRED — singular, not input_images array
  "duration": 5,
  "audio": false,
  "use_unlim": true
}
```

### `wan2-2-video`
```json
{
  "prompt": "...",
  "input_image": "<url>",     // REQUIRED — i2v only
  "use_unlim": true
}
```

### `kling`
```json
{
  "prompt": "...",
  "model": "<kling-variant>", // REQUIRED — sub-variant ID, e.g. "kling-2.0", "kling-3.0"
  "width": 1280,
  "height": 720,
  "use_unlim": true
}
```

### `veo3`
```json
{
  "prompt": "...",
  "input_image": "<url>",     // REQUIRED
  "enhance_prompt": true,     // REQUIRED (boolean)
  "seed": 42,                 // REQUIRED (int)
  "model": "<veo-variant>",   // REQUIRED — e.g. "veo-3.0-generate-preview"
  "use_unlim": true
}
```

### `minimax-hailuo`
```json
{
  "prompt": "...",
  "width": 1280,
  "height": 720,
  "use_unlim": true
}
```

---

## Resolution literal cheatsheet

| Model family | Resolution values |
|---|---|
| Image (`nano-banana-2`, `seedream`, etc.) | `"1k"`, `"2k"`, `"4k"` |
| Video (`seedance`, `kling`, `veo3`, `wan*`) | `"480"`, `"720"`, `"1080"` *(string, NO "p" suffix)* |

---

## How to discover a model's full schema

1. In Chrome with the Higgsfield tab open, run `auth_status` to get a fresh JWT.
2. Submit a deliberately incomplete request to the model's endpoint:
   ```js
   await fetch('https://fnf.higgsfield.ai/jobs/<MODEL_ID>', {
     method: 'POST',
     headers: { 'Authorization': 'Bearer <jwt>', 'Content-Type': 'application/json' },
     body: JSON.stringify({ params: { prompt: "test", use_unlim: true }, use_unlim: true })
   }).then(r => r.json())
   ```
3. The 422 response's `detail` array enumerates every required field with its expected type/literal.
4. Trigger one real submission via the Higgsfield UI for that model and capture the network request body — that gives you all the optional fields and their typical values.

---

## Body shape pattern (universal)

Every model uses this outer envelope:
```json
{
  "params": { /* model-specific fields */, "use_unlim": true },
  "use_unlim": true,
  "use_seedream_bonus": false
}
```

The `use_unlim: true` flag at both levels is the unlock. Everything else is per-model.
