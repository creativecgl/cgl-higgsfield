"""Smoke test — verifies the MCP install end-to-end against the live API.

Run after install / on a teammate's machine to confirm everything wired up:

    python -m higgsfield_unlimited_mcp.verify
    python -m higgsfield_unlimited_mcp.verify --skip-generate   # no real submission
    python -m higgsfield_unlimited_mcp.verify --keep-output     # don't delete the test image

Steps (in order):
    1. Load .env / env vars and validate config
    2. Mint a Clerk JWT (proves cookie + session ID work)
    3. GET /user                        — plan info, has_unlim flag
    4. GET /workspaces/details          — current workspace
    5. GET /concurrent-boost-credits/state — slot tier
    6. GET /jobs/accessible             — recent jobs
    7. POST /jobs/nano-banana-2 (1k 1:1) — submit a tiny test image
    8. Poll /jobs/{id} until completed
    9. Download the result, confirm it's a valid PNG/JPEG
   10. (optional) Cleanup local file

Each step prints PASS/FAIL with a short diagnostic. Exits 0 on full success.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import aiohttp

from .auth import TokenManager
from .client import HiggsfieldClient
from .config import Settings
from .errors import HiggsfieldError


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")

def _fail(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")

def _info(msg: str) -> None:
    print(f"  {DIM}{msg}{RESET}")

def _step(n: int, title: str) -> None:
    print(f"\n{BOLD}[{n}] {title}{RESET}")


async def run(skip_generate: bool, keep_output: bool) -> int:
    failures: list[str] = []

    # ────── 1. Config ──────
    _step(1, "Load configuration")
    try:
        settings = Settings.from_env()
        _ok(f"session_id={settings.session_id[:12]}...")
        _ok(f"max_concurrent={settings.max_concurrent}")
        _ok(f"default_model={settings.default_model}, default_resolution={settings.default_resolution}")
    except Exception as e:
        _fail(str(e))
        return 1

    connector = aiohttp.TCPConnector(limit=10)
    timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=60)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as http:
        tokens = TokenManager(settings.session_id, settings.clerk_cookie)
        client = HiggsfieldClient(http, tokens)

        # ────── 2. Auth ──────
        _step(2, "Mint Clerk JWT")
        try:
            jwt = await tokens.get_token(http, force=True)
            _ok(f"JWT acquired ({len(jwt)} bytes)")
        except Exception as e:
            _fail(f"Auth failed: {e}")
            failures.append("auth")
            return 1

        # ────── 3. /user ──────
        _step(3, "GET /user — plan + balances")
        try:
            user = await client.get_user()
            _ok(f"plan_type={user.get('plan_type')}, has_unlim={user.get('has_unlim')}")
            _info(f"subscription_credits={user.get('subscription_credits')}, "
                  f"daily_credits={user.get('daily_credits')}")
            if not user.get("has_unlim"):
                print(f"  {YELLOW}⚠{RESET}  has_unlim=False on this account — generations will consume credits, not unlimited.")
        except HiggsfieldError as e:
            _fail(str(e)); failures.append("/user")

        # ────── 4. /workspaces/details ──────
        _step(4, "GET /workspaces/details")
        try:
            ws = await client.get_workspace_details()
            _ok(f"workspace id={ws.get('id', '?')[:12]}..., type={ws.get('type')}, role={ws.get('user_role')}")
        except HiggsfieldError as e:
            _fail(str(e)); failures.append("/workspaces/details")

        # ────── 5. /concurrent-boost-credits/state ──────
        _step(5, "GET /concurrent-boost-credits/state")
        try:
            state = await client.get_concurrent_state()
            products = state.get("products") or []
            _ok(f"{len(products)} concurrent-slot tier(s) returned")
        except HiggsfieldError as e:
            _fail(str(e)); failures.append("/concurrent-boost-credits/state")

        # ────── 6. /jobs/accessible ──────
        _step(6, "GET /jobs/accessible — recent jobs")
        try:
            recent = await client.list_recent_jobs(limit=5)
            jobs = recent.get("jobs") or []
            _ok(f"{len(jobs)} recent job(s)")
        except HiggsfieldError as e:
            _fail(str(e)); failures.append("/jobs/accessible")

        # ────── 7-9. Live generate + poll + download ──────
        if skip_generate:
            print(f"\n{DIM}--skip-generate set; skipping submission test.{RESET}")
        else:
            _step(7, "POST /jobs/nano-banana-2 (1k 1:1 test image)")
            try:
                job_id = await client.submit_image_job(
                    prompt="a single white feather, minimal pencil sketch test on white paper",
                    model="nano-banana-2",
                    width=1024, height=1024,
                    aspect_ratio="1:1", resolution="1k",
                    batch_size=1,
                )
                _ok(f"job_id={job_id}")
            except HiggsfieldError as e:
                _fail(str(e)); failures.append("submit")
                return _summary(failures)

            _step(8, f"Poll /jobs/{job_id[:12]}... until completed")
            t0 = time.time()
            try:
                final = await client.poll_job(job_id, timeout=180)
                _ok(f"completed in {time.time() - t0:.1f}s")
            except HiggsfieldError as e:
                _fail(str(e)); failures.append("poll")
                return _summary(failures)

            _step(9, "Extract result URL + download")
            urls = HiggsfieldClient.extract_result_urls(final)
            if not urls:
                _fail(f"No result URLs in payload: {list(final.get('results', {}).keys()) if isinstance(final.get('results'), dict) else type(final.get('results')).__name__}")
                failures.append("extract_urls")
            else:
                _ok(f"found {len(urls)} URL(s)")
                _info(urls[0][:90] + ("..." if len(urls[0]) > 90 else ""))
                try:
                    data = await client.download(urls[0])
                    if not data or len(data) < 1000:
                        _fail(f"download returned only {len(data)} bytes")
                        failures.append("download_size")
                    else:
                        # Sniff magic bytes
                        ext = "bin"
                        if data[:8] == b"\x89PNG\r\n\x1a\n":
                            ext = "png"
                        elif data[:3] == b"\xff\xd8\xff":
                            ext = "jpg"
                        elif data[:4] == b"RIFF":
                            ext = "webp"
                        out_path = settings.output_dir / f"verify_test.{ext}"
                        settings.output_dir.mkdir(parents=True, exist_ok=True)
                        out_path.write_bytes(data)
                        _ok(f"downloaded {len(data):,} bytes → {out_path}")
                        if not keep_output:
                            try:
                                out_path.unlink()
                                _info("(deleted; use --keep-output to keep)")
                            except Exception:
                                pass
                except Exception as e:
                    _fail(f"download failed: {e}")
                    failures.append("download")

    return _summary(failures)


def _summary(failures: list[str]) -> int:
    print()
    if not failures:
        print(f"{GREEN}{BOLD}✅ All checks passed.{RESET} Higgsfield Unlimited MCP is working.")
        return 0
    print(f"{RED}{BOLD}❌ {len(failures)} check(s) failed:{RESET} {', '.join(failures)}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="higgsfield-unlimited-verify")
    parser.add_argument("--skip-generate", action="store_true",
                        help="Skip the actual generation/poll/download (auth + GETs only)")
    parser.add_argument("--keep-output", action="store_true",
                        help="Don't delete the test image after successful download")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(run(args.skip_generate, args.keep_output)))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
