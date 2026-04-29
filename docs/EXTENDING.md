# Extending the MCP Server

The current implementation covers the verified-working endpoints for image generation. Other Higgsfield features (video, workspaces, media uploads, marketing studio) require their own endpoints, which can be discovered with the same browser-inspection technique used to build the image flow.

## Discovering new endpoints

1. Open `https://higgsfield.ai` in Chrome and log in.
2. Open DevTools → Console.
3. Paste this fetch interceptor:

   ```js
   window.__capturedRequests = [];
   const origFetch = window.fetch;
   window.fetch = async function(...args) {
     const url = typeof args[0] === 'string' ? args[0] :
                 (args[0] instanceof Request ? args[0].url : String(args[0]));
     const opts = args[1] || (args[0] instanceof Request ? args[0] : {});
     if (opts.method === 'POST' || opts.method === 'PUT' || opts.method === 'PATCH') {
       const body = typeof opts.body === 'string' ? opts.body : '';
       const entry = { url, method: opts.method, body: body.slice(0, 2000) };
       window.__capturedRequests.push(entry);
       console.log('REQ', entry);
     }
     return origFetch.apply(this, args);
   };
   ```

4. Trigger the feature in the UI (e.g. start a video generation, switch workspace).
5. Read the captured calls: `console.log(JSON.stringify(window.__capturedRequests, null, 2))`.

You'll get the URL pattern, method, headers, and body shape — everything needed to add a new client method.

## Adding a new endpoint to the client

1. Add a method to `client.py`:

   ```python
   async def submit_video_job(self, *, prompt: str, model: str, ...) -> str:
       url = f"{API_BASE}/jobs/{model}"   # or whatever you discovered
       payload = { ..., "use_unlim": True }
       data = await self._request("POST", url, json=payload)
       return data["id"]
   ```

2. Expose a tool in `server.py`:

   ```python
   @mcp.tool()
   async def generate_video(ctx: Context, prompt: str, ...) -> dict:
       app = _ctx(ctx)
       job_id = await app.client.submit_video_job(prompt=prompt, ...)
       # Reuse JobRecord + registry; videos poll the same /jobs/{id}/status endpoint
       ...
   ```

3. Reuse `_run_job_to_completion` if the polling pattern matches.

## Endpoints we already know about (but haven't wrapped yet)

| Endpoint | Method | Purpose |
|---|---|---|
| `/concurrent-boost-credits/state` | GET | Concurrent slot tier info ✅ wrapped |
| `/jobs/{model}` | POST | Submit generation ✅ wrapped (image) |
| `/jobs/{job_id}/status` | GET | Poll job ✅ wrapped |
| `/workspaces` | GET | TBD — discover via interceptor |
| `/media/upload` | POST | TBD — for img2img source uploads |
| `/generations` | GET | TBD — list past generations |

## Adding a new model

Models are just string IDs passed in the URL. To register one in `list_models`:

```python
# config.py
KNOWN_MODELS = {
    ...
    "your_new_model_id": "Friendly description",
}
```

The client doesn't validate model IDs against this list — it's just for discovery via the `list_models` tool.

## Adding a new aspect ratio

```python
# config.py
ASPECT_DIMENSIONS["21:9"] = (1680, 720)
```

That's it — `resolve_dimensions` will pick it up automatically.

## Testing changes locally

The MCP runs over stdio. To test without a full Claude Code session:

```bash
# Quick smoke test:
python -c "
import asyncio
from higgsfield_unlimited_mcp.config import Settings
from higgsfield_unlimited_mcp.auth import TokenManager
import aiohttp

async def main():
    s = Settings.from_env()
    async with aiohttp.ClientSession() as http:
        tm = TokenManager(s.session_id, s.clerk_cookie)
        token = await tm.get_token(http)
        print('JWT prefix:', token[:30])

asyncio.run(main())
"
```

For interactive MCP testing, use the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):
```bash
npx @modelcontextprotocol/inspector python -m higgsfield_unlimited_mcp
```
