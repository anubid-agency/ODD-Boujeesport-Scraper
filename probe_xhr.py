import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from patchright.async_api import async_playwright

# Update this list with the exact bookmaker URLs you want to inspect
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
MAX_BODY_BYTES = 1_500_000  # 1.5 MB per saved JSON file to keep artifacts manageable
MAX_FILES_PER_SITE = 60


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
    """Return (looks_like_odds_data, matched_keys, estimated_item_count)."""
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


async def probe_site(playwright, site: dict, proxy_cfg: dict | None) -> dict:
    name = site["name"]
    url = site["url"]
    site_slug = slugify(name)
    site_dir = OUT_DIR / site_slug
    site_dir.mkdir(parents=True, exist_ok=True)

    start = time.perf_counter()
    captures = []
    error_msg = ""
    final_url = url
    page_title = ""
    saved_count = 0

    browser = None
    try:
        launch_kwargs = {"headless": True}
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg

        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            ignore_https_errors=True,
        )
        page = await context.new_page()
        page.set_default_timeout(25000)

        async def handle_response(response):
            nonlocal saved_count
            try:
                req = response.request
                res_url = response.url
                if res_url.startswith("data:") or res_url.startswith("blob:"):
                    return
                if is_noise_url(res_url):
                    return

                headers = response.headers or {}
                ctype = (headers.get("content-type") or "").lower()
                resource_type = req.resource_type

                if resource_type not in ("xhr", "fetch") and "json" not in ctype:
                    return

                body_bytes = await response.body()
                if not body_bytes:
                    return

                text_preview = body_bytes[:4096].decode("utf-8", errors="replace").lstrip()
                looks_json = "json" in ctype or text_preview.startswith("{") or text_preview.startswith("[")
                if not looks_json:
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
                if parsed_json is not None and len(body_bytes) <= MAX_BODY_BYTES and saved_count < MAX_FILES_PER_SITE:
                    saved_count += 1
                    idx = f"{saved_count:03d}"
                    parsed_u = urlparse(res_url)
                    path_tail = re.sub(r"[^a-zA-Z0-9_-]+", "_", (parsed_u.path or "root").strip("/"))[:40] or "root"
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
                # Ignore responses whose bodies were discarded or closed mid-navigation
                return

        page.on("response", lambda res: asyncio.create_task(handle_response(res)))

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        # Brief settle window for delayed XHR/fetch odds calls
        await page.wait_for_timeout(3000)

        # Let any queued response tasks finish writing
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        final_url = page.url or url
        try:
            page_title = (await page.title())[:100]
        except Exception:
            page_title = ""

        await context.close()
    except Exception as exc:
        error_msg = str(exc).splitlines()[0][:120]
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

    elapsed = round(time.perf_counter() - start, 1)
    odds_hits = [c for c in captures if c["odds_signal"]]
    top_candidates = sorted(
        odds_hits or captures,
        key=lambda x: (x["odds_signal"], len(x["matched_keys"]), x["bytes"]),
        reverse=True,
    )[:5]

    return {
        "site": name,
        "start_url": url,
        "final_url": final_url,
        "title": page_title,
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

    print(f"XHR/JSON Probe at {run_ts}")
    print(f"Proxy configured: {'yes' if proxy_cfg else 'no'}\n")

    header = f"{'Site':<20} {'Secs':<6} {'JSONs':<7} {'OddsJSON':<9} {'Final URL / Error'}"
    print(header)
    print("-" * len(header))

    site_reports = []
    async with async_playwright() as p:
        for site in SITES:
            rep = await probe_site(p, site, proxy_cfg)
            site_reports.append(rep)
            status_or_url = f"ERR: {rep['error']}" if rep["error"] else rep["final_url"]
            print(
                f"{rep['site']:<20} {rep['secs']:<6} "
                f"{rep['json_requests_captured']:<7} {rep['odds_candidate_requests']:<9} "
                f"{status_or_url}"
            )

    summary = {
        "run_at": run_ts,
        "proxy_configured": bool(proxy_cfg),
        "sites_probed": len(site_reports),
        "sites_with_json": sum(1 for r in site_reports if r["json_requests_captured"] > 0),
        "sites_with_odds_json": sum(1 for r in site_reports if r["odds_candidate_requests"] > 0),
        "sites": site_reports,
    }

    (OUT_DIR / "xhr_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nSaved summary to {OUT_DIR / 'xhr_summary.json'}")


if __name__ == "__main__":
    asyncio.run(main())
