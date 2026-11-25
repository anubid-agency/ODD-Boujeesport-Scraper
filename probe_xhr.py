import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from patchright.async_api import async_playwright

SITES = [
    {"name": "Forzza", "url": "https://forzza1.com/"},
    {"name": "Sahara Games", "url": "https://ug.saharagames.com/"},
    {"name": "SemaBet (d247bet)", "url": "https://d247bet.ug/"},
    {"name": "AbaBet", "url": "https://ababet.ug/"},
    {"name": "Kagwirawo (KBet)", "url": "https://kbet.ug/"},
    {"name": "22Bet", "url": "https://22bet.ug/"},
    {"name": "BetPawa", "url": "https://www.betpawa.ug/"},
    {"name": "Betika", "url": "https://www.betika.com/ug/"},
    {"name": "BangBet", "url": "https://www.bangbet.com/ug/"},
    {"name": "ForteBet", "url": "https://www.fortebet.ug/"},
    {"name": "GSB", "url": "https://www.gsb.ug/"},
    {"name": "ChampionBet", "url": "https://championbet.ug/"},
    {"name": "BetVita", "url": "https://betvita.ug/"},
    {"name": "Premier Bet", "url": "https://www.premierbet.co.ug/"},
    {"name": "TopBet", "url": "https://topbet.co.ug/"},
]

NOISE_DOMAINS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.com",
    "connect.facebook.net",
    "hotjar.com",
    "sentry.io",
    "segment.io",
    "amplitude.com",
    "mixpanel.com",
    "posthog.com",
    "clarity.ms",
    "newrelic.com",
    "trustpilot.com",
)

ODDS_KEYS = (
    "odds",
    "odd",
    "markets",
    "market",
    "outcomes",
    "outcome",
    "events",
    "event",
    "matches",
    "match",
    "fixtures",
    "fixture",
    "competitors",
    "competitor",
    "selections",
    "selection",
    "price",
    "factor",
    "coef",
    "1x2",
    "handicap",
    "total",
)

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-access-token",
    "x-csrf-token",
}

OUT_DIR = Path("xhr_captures")
MAX_BODY_BYTES = 1_500_000
MAX_FILES_PER_SITE = 50
PER_SITE_HARD_TIMEOUT = 35.0
CONCURRENCY = 4


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "site"


def redact_headers(headers: dict) -> dict:
    cleaned = {}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk in SENSITIVE_HEADERS or "token" in lk or "secret" in lk or "auth" in lk:
            cleaned[k] = "[REDACTED]"
        else:
            cleaned[k] = v
    return cleaned


def is_noise_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in NOISE_DOMAINS)


def score_json_payload(parsed) -> tuple[bool, list[str], int]:
    matched_keys = set()
    item_count = 0

    def walk(obj, depth=0):
        nonlocal item_count
        if depth > 6:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower()
                for token in ODDS_KEYS:
                    if token in lk:
                        matched_keys.add(token)
                if isinstance(v, list):
                    item_count += len(v)
                walk(v, depth + 1)
        elif isinstance(obj, list):
            item_count += len(obj)
            for item in obj[:15]:
                walk(item, depth + 1)

    walk(parsed)
    has_signal = len(matched_keys) >= 2 or ("odds" in matched_keys and item_count > 0)
    return has_signal, sorted(matched_keys), item_count


def parse_proxy(proxy_str: str | None) -> dict | None:
    if not proxy_str:
        return None
    parsed = urlparse(proxy_str)
    if not parsed.hostname or not parsed.port:
        return {"server": proxy_str}
    cfg = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


async def _probe_site_inner(browser, site: dict, state: dict) -> None:
    url = site["url"]
    site_dir = state["site_dir"]
    captures = state["captures"]
    bg_tasks: set[asyncio.Task] = set()

    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        ignore_https_errors=True,
    )
    try:
        page = await context.new_page()
        page.set_default_timeout(15000)

        async def handle_response(response):
            try:
                req = response.request
                res_url = response.url
                if res_url.startswith(("data:", "blob:")) or is_noise_url(res_url):
                    return

                headers = response.headers or {}
                ctype = (headers.get("content-type") or "").lower()
                resource_type = req.resource_type
                if resource_type not in ("xhr", "fetch") and "json" not in ctype:
                    return

                body_bytes = await asyncio.wait_for(response.body(), timeout=5.0)
                if not body_bytes:
                    return

                text_preview = body_bytes[:4096].decode("utf-8", errors="replace").lstrip()
                if not ("json" in ctype or text_preview.startswith(("{", "["))):
                    return

                parsed_json = None
                odds_signal = False
                matched_keys = []
                item_count = 0
                try:
                    parsed_json = json.loads(body_bytes.decode("utf-8", errors="replace"))
                    odds_signal, matched_keys, item_count = score_json_payload(parsed_json)
                except Exception:
                    pass

                saved_file = None
                if (
                    parsed_json is not None
                    and len(body_bytes) <= MAX_BODY_BYTES
                    and state["saved_count"] < MAX_FILES_PER_SITE
                ):
                    state["saved_count"] += 1
                    idx = f"{state['saved_count']:03d}"
                    parsed_u = urlparse(res_url)
                    path_tail = (
                        re.sub(r"[^a-zA-Z0-9_-]+", "_", (parsed_u.path or "root").strip("/"))[:40]
                        or "root"
                    )
                    fname = f"{idx}_{response.status}_{path_tail}.json"
                    fpath = site_dir / fname
                    fpath.write_text(
                        json.dumps(
                            {
                                "url": res_url,
                                "method": req.method,
                                "status": response.status,
                                "request_headers": redact_headers(req.headers),
                                "response_headers": redact_headers(headers),
                                "odds_signal": odds_signal,
                                "matched_keys": matched_keys,
                                "item_count": item_count,
                                "data": parsed_json,
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    saved_file = str(fpath)

                captures.append(
                    {
                        "url": res_url,
                        "method": req.method,
                        "resource_type": resource_type,
                        "status": response.status,
                        "content_type": ctype.split(";")[0],
                        "bytes": len(body_bytes),
                        "odds_signal": odds_signal,
                        "matched_keys": matched_keys,
                        "item_count": item_count,
                        "saved_file": saved_file,
                    }
                )
            except Exception:
                return

        def on_response(res):
            task = asyncio.create_task(handle_response(res))
            bg_tasks.add(task)
            task.add_done_callback(bg_tasks.discard)

        page.on("response", on_response)

        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)

        if bg_tasks:
            await asyncio.wait(list(bg_tasks), timeout=3.0)
            for t in list(bg_tasks):
                if not t.done():
                    t.cancel()

        state["final_url"] = page.url or url
        try:
            state["title"] = (await page.title())[:100]
        except Exception:
            pass
    finally:
        try:
            await asyncio.wait_for(context.close(), timeout=5.0)
        except Exception:
            pass


async def probe_site(browser, site: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        name = site["name"]
        url = site["url"]
        site_dir = OUT_DIR / slugify(name)
        site_dir.mkdir(parents=True, exist_ok=True)

        state = {
            "site_dir": site_dir,
            "captures": [],
            "saved_count": 0,
            "final_url": url,
            "title": "",
        }
        error_msg = ""
        start = time.perf_counter()

        try:
            await asyncio.wait_for(_probe_site_inner(browser, site, state), timeout=PER_SITE_HARD_TIMEOUT)
        except asyncio.TimeoutError:
            error_msg = f"Hard timeout after {int(PER_SITE_HARD_TIMEOUT)}s"
        except Exception as exc:
            error_msg = str(exc).splitlines()[0][:120]

        elapsed = round(time.perf_counter() - start, 1)
        captures = state["captures"]
        odds_hits = [c for c in captures if c["odds_signal"]]
        top_candidates = sorted(
            odds_hits or captures,
            key=lambda x: (x["odds_signal"], len(x["matched_keys"]), x["bytes"]),
            reverse=True,
        )[:5]

        status_or_url = f"ERR: {error_msg}" if error_msg else state["final_url"]
        print(
            f"{name:<20} {elapsed:<6} {len(captures):<7} {len(odds_hits):<9} {status_or_url}",
            flush=True,
        )

        return {
            "site": name,
            "start_url": url,
            "final_url": state["final_url"],
            "title": state["title"],
            "secs": elapsed,
            "error": error_msg,
            "json_requests_captured": len(captures),
            "odds_candidate_requests": len(odds_hits),
            "top_candidates": top_candidates,
            "captures": captures,
        }


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    proxy_raw = os.environ.get("BOOKMAKER_PROXY") or None
    proxy_cfg = parse_proxy(proxy_raw)
    run_ts = datetime.now(timezone.utc).isoformat()

    print(f"XHR/JSON Probe at {run_ts}", flush=True)
    print(f"Proxy configured: {'yes' if proxy_cfg else 'no'}\n", flush=True)

    header = f"{'Site':<20} {'Secs':<6} {'JSONs':<7} {'OddsJSON':<9} {'Final URL / Error'}"
    print(header, flush=True)
    print("-" * len(header), flush=True)

    sem = asyncio.Semaphore(CONCURRENCY)
    launch_kwargs = {"headless": True}
    if proxy_cfg:
        launch_kwargs["proxy"] = proxy_cfg

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            tasks = [probe_site(browser, site, sem) for site in SITES]
            site_reports = await asyncio.gather(*tasks)
        finally:
            try:
                await asyncio.wait_for(browser.close(), timeout=10.0)
            except Exception:
                pass

    summary = {
        "run_at": run_ts,
        "proxy_configured": bool(proxy_cfg),
        "sites_probed": len(site_reports),
        "sites_with_json": sum(1 for r in site_reports if r["json_requests_captured"] > 0),
        "sites_with_odds_json": sum(1 for r in site_reports if r["odds_candidate_requests"] > 0),
        "sites": site_reports,
    }

    (OUT_DIR / "xhr_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nSaved summary to {OUT_DIR / 'xhr_summary.json'}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
