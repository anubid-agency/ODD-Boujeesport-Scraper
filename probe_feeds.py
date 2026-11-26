#!/usr/bin/env python3
"""
Feed probe: finds where each bookmaker site loads its match list and odds from.

It does what you would do by hand with F12 -> Network, but automatically:
  1. opens each site in a real (stealth) browser,
  2. accepts cookie banners and clicks Football / Upcoming,
  3. records every JSON request the page makes (and any websocket address),
  4. scores each one by how much it looks like a match + odds feed,
  5. saves the best candidates so they can be studied and turned into parsers.

It does NOT scrape odds for your site. It only discovers the data sources.
Every site runs on its own; one failing never stops the others.
Always exits 0, so GitHub never emails a failure.

Output (folder out/probe/):
  summary.json         ranked candidate feeds for every site
  <site>_top1..3.json  full response body of the 3 best candidates per site

Optional env vars:
  PROBE_ONLY   comma list of site names to probe, e.g. "Forzza,Sahara Games"
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(os.environ.get("PROBE_OUT", "out/probe"))

# Pages to open. Edit freely: use the page that shows football matches if you know it.
SITES = {
    "Forzza":       "https://forzza1.com/",
    "Sahara Games": "https://ug.saharagames.com/",
    "SemaBet":      "https://d247bet.ug/",
    "BetPawa":      "https://www.betpawa.ug/",
    "ChampionBet":  "https://championbet.ug/",
    "Betika":       "https://www.betika.com/en-ke/",
    "22Bet":        "https://22bet.ug/",
}

NAV_TIMEOUT_MS = 45000
SETTLE_MS = 8000          # wait after the page opens, for first requests
CLICK_SETTLE_MS = 6000    # wait after each click
SCROLL_SETTLE_MS = 4000
MAX_CAPTURED = 400        # responses kept per site
MAX_BODY_BYTES = 3_000_000
MAX_SAVED_BYTES = 400_000
SITE_DEADLINE_S = 150

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

COOKIE_RE = re.compile(r"^\s*(accept( all)?|i agree|agree|got it|ok|allow( all)?)\s*$", re.I)
CLICK_STEPS = [
    re.compile(r"^\s*(football|soccer)\s*$", re.I),
    re.compile(r"^\s*(upcoming|prematch|pre-match|today)\s*$", re.I),
]

KEYWORDS = ("home", "away", "competitor", "odds", "team", "match", "event",
            "league", "price", "outcome", "market", "fixture", "1x2")
DECIMAL_RE = re.compile(r"(?<![\d.])(?:[1-9]|1\d|2\d)\.\d{2}(?!\d)")
NOISE_MARKERS = ('"translations"', '"dictionary"', '"jurisdictions"', '"i18n"', '"locale"')
SENSITIVE_PARAM = re.compile(r"(token|key|auth|sess|sid|sign|secret|pass)", re.I)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def redact_url(url):
    """Hide values of token-like query parameters (the artifact may be public)."""
    def repl(m):
        name = m.group(2)
        val = "[redacted]" if SENSITIVE_PARAM.search(name) else m.group(3)
        return f"{m.group(1)}{name}={val}"
    return re.sub(r"([?&])([^=&]+)=([^&]*)", repl, url)


def score_text(text):
    chunk = text[:200000]
    low = chunk.lower()
    kw = sum(1 for k in KEYWORDS if k in low)
    dec = min(len(DECIMAL_RE.findall(chunk)), 60)
    score = kw * 3 + dec
    if any(m in low[:2500] for m in NOISE_MARKERS):
        score -= 15
    return score


def looks_json(text):
    s = text.lstrip()[:1]
    return s in ("{", "[")


def header_names(req):
    try:
        names = [h.lower() for h in req.headers.keys()]
    except Exception:
        return []
    keep = [n for n in names if n.startswith("x-") or n in ("authorization", "content-type", "origin", "referer")]
    return sorted(set(keep))


def try_click(page, pattern, settle_ms):
    try:
        page.get_by_text(pattern).first.click(timeout=3000)
        page.wait_for_timeout(settle_ms)
        return True
    except Exception:
        return False


def probe_site(sync_playwright_ctx, browser, name, url):
    start = time.time()
    info = {"name": name, "url": url, "ok": False, "error": None,
            "final_url": None, "candidates": [], "websockets": [], "seconds": None,
            "responses_seen": 0}
    captured = []
    sockets = []
    context = None
    try:
        context = browser.new_context(
            user_agent=USER_AGENT, locale="en-UG", timezone_id="Africa/Kampala",
            viewport={"width": 1366, "height": 768},
        )
        page = context.new_page()

        def on_response(resp):
            try:
                if len(captured) >= MAX_CAPTURED:
                    return
                req = resp.request
                ct = (resp.headers.get("content-type") or "").lower()
                if req.resource_type in ("xhr", "fetch") or "json" in ct:
                    captured.append(resp)
            except Exception:
                pass

        page.on("response", on_response)
        page.on("websocket", lambda ws: sockets.append(redact_url(ws.url)))

        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_MS)
        try_click(page, COOKIE_RE, 1500)
        for pattern in CLICK_STEPS:
            if time.time() - start > SITE_DEADLINE_S:
                break
            try_click(page, pattern, CLICK_SETTLE_MS)
        try:
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(SCROLL_SETTLE_MS)
        except Exception:
            pass

        info["final_url"] = redact_url(page.url)
        info["responses_seen"] = len(captured)

        seen_keys = set()
        for resp in captured:
            try:
                req = resp.request
                key = (req.method, resp.url.split("?")[0])
                body = resp.body()
            except Exception:
                continue
            if not body or len(body) > MAX_BODY_BYTES:
                continue
            text = body.decode("utf-8", errors="replace")
            if key in seen_keys:
                continue
            seen_keys.add(key)
            post = None
            try:
                post = (req.post_data or "")[:500] or None
            except Exception:
                pass
            info["candidates"].append({
                "method": req.method,
                "url": redact_url(resp.url),
                "status": resp.status,
                "content_type": (resp.headers.get("content-type") or "")[:60],
                "size": len(body),
                "is_json": looks_json(text),
                "score": score_text(text),
                "post_data": post,
                "custom_header_names": header_names(req),
                "sample": text[:500],
                "_full": text,
            })

        info["candidates"].sort(key=lambda c: c["score"], reverse=True)
        info["websockets"] = sorted(set(sockets))
        info["ok"] = bool(info["candidates"])
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
    info["seconds"] = round(time.time() - start, 1)
    return info


def save_results(results, engine):
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {"run_at": utc_now(), "engine": engine, "sites": []}
    for info in results:
        safe = re.sub(r"[^a-z0-9]+", "_", info["name"].lower()).strip("_")
        top = info["candidates"][:3]
        for i, cand in enumerate(top, start=1):
            (OUT / f"{safe}_top{i}.json").write_text(
                json.dumps({
                    "site": info["name"], "url": cand["url"], "method": cand["method"],
                    "post_data": cand["post_data"], "score": cand["score"],
                    "body": cand["_full"][:MAX_SAVED_BYTES],
                }, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        slim = dict(info)
        slim["candidates"] = [{k: v for k, v in c.items() if k != "_full"}
                              for c in info["candidates"][:12]]
        summary["sites"].append(slim)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                      encoding="utf-8")


def print_report(results):
    print(f"\nProbe finished at {utc_now()}\n")
    for info in results:
        print("=" * 100)
        print(f"{info['name']}  ({info['seconds']}s)  ok={info['ok']}  "
              f"responses_seen={info['responses_seen']}  final={info['final_url']}")
        if info["error"]:
            print(f"  ERROR: {info['error']}")
        for ws in info["websockets"][:5]:
            print(f"  WEBSOCKET: {ws}")
        for c in info["candidates"][:6]:
            print(f"  score={c['score']:<4} {c['method']:<5} {c['status']} "
                  f"{c['size']:>8}B json={c['is_json']!s:<5} {c['url'][:110]}")
    print("=" * 100)


def run_engine(engine_name, sync_playwright, sites):
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for name, url in sites.items():
                results.append(probe_site(p, browser, name, url))
        finally:
            try:
                browser.close()
            except Exception:
                pass
    return results


def main():
    only = [s.strip().lower() for s in (os.environ.get("PROBE_ONLY") or "").split(",") if s.strip()]
    sites = {n: u for n, u in SITES.items() if not only or n.lower() in only}

    engines = []
    try:
        from patchright.sync_api import sync_playwright as pr
        engines.append(("patchright", pr))
    except Exception:
        pass
    try:
        from playwright.sync_api import sync_playwright as pw
        engines.append(("playwright", pw))
    except Exception:
        pass
    if not engines:
        print("No browser library found (patchright or playwright).")
        return

    results, used = [], None
    for engine_name, sp in engines:
        try:
            results = run_engine(engine_name, sp, sites)
            used = engine_name
            break
        except Exception as exc:
            print(f"Engine {engine_name} could not run: {type(exc).__name__}: {str(exc)[:200]}")
    if used is None:
        print("No browser engine could start. Check the browser install step.")
        return

    save_results(results, used)
    print_report(results)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Unexpected error: {type(e).__name__}: {e}")
    sys.exit(0)  # always 0 so GitHub never emails a failure
