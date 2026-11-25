import json
import os
import re
import time
from datetime import datetime, timezone

from scrapling.fetchers import Fetcher, StealthyFetcher

SITES = [
    {"name": "Forzza", "url": "https://forzza1.com/", "use_stealth": True},
    {"name": "Forzza (mobile)", "url": "https://m.forzza1.com/", "use_stealth": True},
    {"name": "Sahara Games", "url": "https://ug.saharagames.com/", "use_stealth": False},
    {"name": "SemaBet", "url": "https://semabet.com/", "use_stealth": False},
    {"name": "SemaBet (.ug)", "url": "https://semabet.ug/", "use_stealth": False},
    {"name": "22Bet", "url": "https://22bet.ug/", "use_stealth": True},
    {"name": "AbaBet", "url": "https://ababet.ug/", "use_stealth": True},
    {"name": "Kagwirawo (KBet)", "url": "https://kbet.ug/", "use_stealth": True},
    {"name": "Kagwirawo (.ug)", "url": "https://www.kagwirawo.ug/", "use_stealth": False},
]

BLOCK_MARKERS = [
    "cf-browser-verification",
    "cf_chl_opt",
    "attention required! | cloudflare",
    "just a moment...",
    "ddos-guard",
    "incapsula",
    "access denied",
    "bot protection",
    "403 forbidden",
]

ODDS_DECIMAL_RE = re.compile(r"\b\d+\.\d{2}\b")
ODDS_KEYWORDS = ("odds", "1x2", "handicap", "over/under", "draw", "prematch", "live", "sports")


def looks_blocked(status_code: int | None, html_text: str) -> bool:
    if status_code in (401, 403, 407, 429, 503):
        return True
    sample = (html_text or "")[:12000].lower()
    if len(html_text or "") < 6000 and any(m in sample for m in BLOCK_MARKERS):
        return True
    if status_code == 200 and len(html_text or "") < 600 and "just a moment..." in sample:
        return True
    return False


def has_odds_signals(html_text: str) -> bool:
    if not html_text:
        return False
    lower = html_text.lower()
    decimal_hits = len(ODDS_DECIMAL_RE.findall(html_text))
    keyword_hits = sum(1 for kw in ODDS_KEYWORDS if kw in lower)
    return decimal_hits >= 4 and keyword_hits >= 1


def extract_body_text(page) -> str:
    raw = getattr(page, "html_content", None) or getattr(page, "body", "") or ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def run_stealth(url: str, fallback_url: str, proxy: str | None):
    stealth_kwargs = {
        "headless": True,
        "network_idle": True,
        "timeout": 35000,
    }
    if proxy:
        stealth_kwargs["proxy"] = proxy

    page = StealthyFetcher.fetch(url, **stealth_kwargs)
    status = getattr(page, "status", 200)
    final_url = str(getattr(page, "url", fallback_url) or fallback_url)
    html_text = extract_body_text(page)
    blocked = looks_blocked(status, html_text)
    return status, final_url, html_text, blocked


def probe_site(site: dict, proxy: str | None) -> dict:
    name = site["name"]
    url = site["url"]
    force_stealth = site.get("use_stealth", False)

    start = time.perf_counter()
    method_used = "stealth" if force_stealth else "normal"
    blocked = False
    error_msg = ""
    html_text = ""
    status = None
    final_url = url

    try:
        if not force_stealth:
            fetcher_kwargs = {
                "headers": {
                    "Referer": "https://www.google.com/",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                "follow_redirects": True,
                "timeout": 20,
            }
            if proxy:
                fetcher_kwargs["proxy"] = proxy

            page = Fetcher.get(url, **fetcher_kwargs)
            status = getattr(page, "status", 200)
            final_url = str(getattr(page, "url", url) or url)
            html_text = extract_body_text(page)

            if looks_blocked(status, html_text) or (status == 200 and len(html_text) < 2500 and not has_odds_signals(html_text)):
                method_used = "stealth (fallback)"
                status, final_url, html_text, blocked = run_stealth(final_url, final_url, proxy)
        else:
            status, final_url, html_text, blocked = run_stealth(url, url, proxy)

    except Exception as exc:
        error_msg = str(exc).splitlines()[0][:80]

    elapsed = time.perf_counter() - start
    size_bytes = len(html_text.encode("utf-8"))
    odds_found = has_odds_signals(html_text)
    ok = (not error_msg) and (not blocked) and (status in (200, 201, 202)) and (size_bytes > 500)

    return {
        "site": name,
        "ok": ok,
        "method": method_used,
        "status": status if status is not None else "-",
        "secs": round(elapsed, 1),
        "size": size_bytes,
        "blocked": blocked,
        "odds": odds_found,
        "final": error_msg if error_msg else final_url,
    }


def main() -> None:
    proxy = os.environ.get("BOOKMAKER_PROXY") or None
    run_ts = datetime.now(timezone.utc).isoformat()
    print(f"Run at {run_ts}")
    print(f"Proxy configured: {'yes' if proxy else 'no'}\n")

    header = (
        f"{'Site':<18} {'OK':<6} {'Method':<18} {'Status':<7} "
        f"{'Secs':<6} {'Size':<9} {'Blocked':<8} {'Odds?':<6} {'Final URL / Error'}"
    )
    print(header)
    print("-" * len(header))

    results = []
    for site in SITES:
        row = probe_site(site, proxy)
        results.append(row)
        print(
            f"{row['site']:<18} {str(row['ok']):<6} {row['method']:<18} "
            f"{str(row['status']):<7} {str(row['secs']):<6} {row['size']:<9} "
            f"{str(row['blocked']):<8} {str(row['odds']):<6} {row['final']}"
        )

    reachable = sum(1 for r in results if r["ok"])
    with_odds = sum(1 for r in results if r["ok"] and r["odds"])
    print(f"\n{reachable}/{len(results)} sites reachable | {with_odds}/{len(results)} with odds signals")

    payload = {
        "run_at": run_ts,
        "proxy_configured": bool(proxy),
        "reachable": reachable,
        "with_odds": with_odds,
        "total": len(results),
        "results": results,
    }
    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


if __name__ == "__main__":
    main()
