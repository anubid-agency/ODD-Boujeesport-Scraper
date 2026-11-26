#!/usr/bin/env python3
"""
Feed probe (v2): finds where each bookmaker site loads its match list and odds from.

It does what you would do by hand with F12 -> Network, but automatically:
  1. opens each site in a real (stealth) browser,
  2. accepts cookie banners and clicks Football / Upcoming,
  3. records every JSON request the page makes (and any websocket address),
  4. scores each one by how much it looks like a match + odds feed,
  5. saves the best candidates so they can be studied and turned into parsers.

New in v2:
  - EXTRA_URLS: feed addresses we already know, fetched directly inside the
    browser session (ForteBet, KBet) so their real payloads get saved.
  - SAVE_HTML: also saves the rendered page HTML (for AbaBet, which has no JSON feed).
  - Big feeds are trimmed for saving: the largest list inside the JSON is cut to
    the first 40 items, and its full length and path are recorded (paging check).
  - summary.json is MERGED by site name, so probing 3 sites never erases the
    results of the others.

It does NOT scrape odds for your site. It only discovers the data sources.
Every site runs on its own; one failing never stops the others.
Always exits 0, so GitHub never emails a failure.

Output (folder PROBE_OUT, default out/probe/):
  summary.json         ranked candidate feeds for every site (merged)
  <site>_top1..3.json  trimmed body of the 3 best candidates per site
  <site>_page.html     rendered page (only for sites in SAVE_HTML)

Optional env vars:
  PROBE_ONLY   comma list of site names to probe, e.g. "ForteBet,KBet,AbaBet"
  PROBE_OUT    output folder
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
    "ForteBet":     "https://www.fortebet.ug/",
    "KBet":         "https://kbet.ug/",
    "AbaBet":       "https://ababet.ug/",
}

# Known feed addresses, fetched directly after the page has loaded (same browser session).
EXTRA_URLS = {
    "ForteBet": ["https://www.fortebet.ug/api/web/v1/offer/full-prematch-en"],
    "KBet":     ["https://kbet.ug/api/v1/events/upcoming"],
}

# Sites whose rendered HTML should also be saved (no clear JSON feed).
SAVE_HTML = {"AbaBet"}

NAV_TIMEOUT_MS = 45000
SETTLE_MS = 8000          # wait after the page opens, for first requests
CLICK_SETTLE_MS = 6000    # wait after each click
SCROLL_SETTLE_MS = 4000
MAX_CAPTURED = 400        # responses kept per site
MAX_BODY_BYTES = 15_000_000
MAX_SAVED_BYTES = 400_000
TRIM_LIST_TO = 40         # items kept from the largest list in a big JSON body
MAX_HTML_BYTES = 300_000
SITE_DEADLINE_S = 180

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
    """Hide values of token-like query parameters (the results may be public)."""
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
    return text.lstrip()[:1] in ("{", "[")


def find_largest_list(node, path="$", depth=0, best=None):
    """Return (path, length) of the longest list of dicts, searching a few levels deep."""
    if best is None:
        best = ["", 0]
    if depth > 5:
        return best
    if isinstance(node, list):
        if node and isinstance(node[0], dict) and len(node) > best[1]:
            best[0], best[1] = path, len(node)
        for i, item in enumerate(node[:3]):
            find_largest_list(item, f"{path}[{i}]", depth + 1, best)
    elif isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                find_largest_list(v, f"{path}.{k}", depth + 1, best)
    return best


def trim_json_text(text):
    """If the body is big JSON, cut its largest list to the first TRIM_LIST_TO items.
    Returns (text_to_save, list_path, list_len, trimmed_flag)."""
    if not looks_json(text):
        return text[:MAX_SAVED_BYTES], "", 0, len(text) > MAX_SAVED_BYTES
    try:
        data = json.loads(text)
    except Exception:
        return text[:MAX_SAVED_BYTES], "", 0, len(text) > MAX_SAVED_BYTES
    path, n = find_largest_list(data)
    if len(text) <= MAX_SAVED_BYTES or not path:
        return text[:MAX_SAVED_BYTES] if len(text) > MAX_SAVED_BYTES else text, path, n, False

    # cut the largest list down; walk the path (only simple $.a.b[0].c forms are cut)
    try:
        tokens = re.findall(r"\.([^.\[\]]+)|\[(\d+)\]", path)
        ref = data
        for key, idx in tokens[:-1]:
            ref = ref[key] if key else ref[int(idx)]
        last_key, last_idx = tokens[-1]
        if last_key:
            ref[last_key] = ref[last_key][:TRIM_LIST_TO]
        else:
            ref[int(last_idx)] = ref[int(last_idx)][:TRIM_LIST_TO]
        out = json.dumps(data, ensure_ascii=False)
        return out[:MAX_SAVED_BYTES * 3], path, n, True
    except Exception:
        return text[:MAX_SAVED_BYTES], path, n, True


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


def make_candidate(method, url, status, content_type, body, post_data, hdr_names, source):
    text = body.decode("utf-8", errors="replace")
    is_json = looks_json(text)
    path, n = ("", 0)
    if is_json:
        try:
            path, n = find_largest_list(json.loads(text))
        except Exception:
            pass
    return {
        "source": source,
        "method": method,
        "url": redact_url(url),
        "status": status,
        "content_type": (content_type or "")[:60],
        "size": len(body),
        "is_json": is_json,
        "score": score_text(text),
        "largest_list_path": path,
        "largest_list_len": n,
        "post_data": post_data,
        "custom_header_names": hdr_names,
        "sample": text[:500],
        "_full": text,
    }


def probe_site(name, url, context_factory):
    start = time.time()
    info = {"name": name, "url": url, "ok": False, "error": None,
            "final_url": None, "candidates": [], "websockets": [], "seconds": None,
            "responses_seen": 0, "html": None}
    captured = []
    sockets = []
    context = None
    try:
        context = context_factory()
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

        if name in SAVE_HTML:
            try:
                info["html"] = page.content()[:MAX_HTML_BYTES]
            except Exception:
                pass

        seen_keys = set()
        for resp in captured:
            try:
                req = resp.request
                key = (req.method, resp.url.split("?")[0])
                body = resp.body()
            except Exception:
                continue
            if not body or len(body) > MAX_BODY_BYTES or key in seen_keys:
                continue
            seen_keys.add(key)
            post = None
            try:
                post = (req.post_data or "")[:500] or None
            except Exception:
                pass
            info["candidates"].append(make_candidate(
                req.method, resp.url, resp.status,
                resp.headers.get("content-type"), body, post, header_names(req), "page"))

        # Known feed addresses, fetched inside the same browser session.
        for extra in EXTRA_URLS.get(name, []):
            try:
                r = context.request.get(extra, timeout=40000)
                body = r.body()
                if body and len(body) <= MAX_BODY_BYTES:
                    info["candidates"].append(make_candidate(
                        "GET", extra, r.status, r.headers.get("content-type"),
                        body, None, [], "direct"))
            except Exception as exc:
                info.setdefault("direct_errors", []).append(
                    f"{redact_url(extra)}: {type(exc).__name__}: {str(exc)[:120]}")

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
    summary_path = OUT / "summary.json"

    # Merge with an existing summary so probing a few sites never erases the others.
    merged = {}
    if summary_path.exists():
        try:
            old = json.loads(summary_path.read_text(encoding="utf-8"))
            for s in old.get("sites", []):
                merged[s["name"]] = s
        except Exception:
            pass

    for info in results:
        safe = re.sub(r"[^a-z0-9]+", "_", info["name"].lower()).strip("_")
        # direct (known-feed) candidates are saved first, then the best page ones
        ranked = sorted(info["candidates"], key=lambda c: (c["source"] != "direct", -c["score"]))
        for i, cand in enumerate(ranked[:3], start=1):
            body, path, n, trimmed = trim_json_text(cand["_full"])
            (OUT / f"{safe}_top{i}.json").write_text(
                json.dumps({
                    "site": info["name"], "url": cand["url"], "method": cand["method"],
                    "source": cand["source"], "post_data": cand["post_data"],
                    "score": cand["score"], "original_size": cand["size"],
                    "largest_list_path": path, "largest_list_len": n,
                    "trimmed": trimmed, "body": body,
                }, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        if info.get("html"):
            (OUT / f"{safe}_page.html").write_text(info["html"], encoding="utf-8")
        slim = {k: v for k, v in info.items() if k != "html"}
        slim["candidates"] = [{k: v for k, v in c.items() if k != "_full"} for c in ranked[:12]]
        slim["probed_at"] = utc_now()
        merged[info["name"]] = slim

    summary = {"run_at": utc_now(), "engine": engine, "sites": list(merged.values())}
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


def print_report(results):
    print(f"\nProbe finished at {utc_now()}\n")
    for info in results:
        print("=" * 100)
        print(f"{info['name']}  ({info['seconds']}s)  ok={info['ok']}  "
              f"responses_seen={info['responses_seen']}  final={info['final_url']}")
        if info["error"]:
            print(f"  ERROR: {info['error']}")
        for err in info.get("direct_errors", []):
            print(f"  DIRECT FETCH FAILED: {err}")
        for ws in info["websockets"][:5]:
            print(f"  WEBSOCKET: {ws}")
        ranked = sorted(info["candidates"], key=lambda c: (c["source"] != "direct", -c["score"]))
        for c in ranked[:6]:
            lst = f" list={c['largest_list_len']}" if c["largest_list_len"] else ""
            print(f"  [{c['source']:<6}] score={c['score']:<4} {c['method']:<5} {c['status']} "
                  f"{c['size']:>9}B json={c['is_json']!s:<5}{lst} {c['url'][:100]}")
    print("=" * 100)


def make_context_factory(browser):
    def factory():
        return browser.new_context(
            user_agent=USER_AGENT, locale="en-UG", timezone_id="Africa/Kampala",
            viewport={"width": 1366, "height": 768},
        )
    return factory


def run_engine(engine_name, sync_playwright, sites):
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        factory = make_context_factory(browser)
        try:
            for name, url in sites.items():
                results.append(probe_site(name, url, factory))
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
