"""
Step 1: can GitHub Actions reach each bookmaker?

Does NOT scrape odds yet. For each site it records:
  - did the request work, HTTP status, how long it took
  - page size
  - whether it looks like a bot-check / block page
  - whether the page mentions odds (a hint that data is in the HTML)
  - whether a normal fetch or the stealth fetch worked

Every site runs on its own. One failing never stops the others.
The script always exits 0, so GitHub never sends a failure email.
Results are written to results.json and printed as a table.
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from scrapling.fetchers import Fetcher

# --- EDIT THESE: open each site in your browser and paste the real URL ---
# Best to use the page that lists football matches / odds, not the home page.
SITES = {
    "BetPawa":     "https://www.betpawa.ug/",
    "22Bet":       "https://22bet.com/",
    "1xBet":       "https://1xbet.com/",
    "Betika":      "https://www.betika.com/",
    "ForteBet":    "https://fortebet.ug/",
    "ChampionBet": "https://championbet.ug/",
    "AbaBet":      "https://ababet.com/",
    "BangBet":     "https://www.bangbet.com/",
    "GSB":         "https://gsb.ug/",
    "Kagwirawo":   "https://kagwirawo.com/",
}

TIMEOUT_SECONDS = 25      # per request
SITE_DEADLINE = 90        # hard limit per site, including the stealth retry
USE_STEALTH_RETRY = True  # try the browser-based fetcher if the normal one is blocked

BLOCK_MARKERS = [
    "just a moment", "attention required", "access denied", "cf-chl",
    "captcha", "verify you are human", "checking your browser",
    "not available in your country", "unusual traffic", "request blocked",
]


def looks_blocked(status, text):
    if status in (401, 403, 429, 451, 503):
        return True
    low = (text or "").lower()
    return any(m in low for m in BLOCK_MARKERS)


def read_page(page):
    status = getattr(page, "status", None)
    try:
        text = page.html_content
    except Exception:
        text = str(getattr(page, "body", b"")[:200000])
    return status, text or ""


def try_normal(url):
    page = Fetcher.get(url, timeout=TIMEOUT_SECONDS, stealthy_headers=True)
    return read_page(page)


def try_stealth(url):
    from scrapling.fetchers import StealthyFetcher
    page = StealthyFetcher.fetch(url, headless=True, network_idle=True,
                                 timeout=TIMEOUT_SECONDS * 1000)
    return read_page(page)


def test_site(name, url):
    result = {
        "name": name, "url": url, "ok": False, "method": None,
        "status": None, "seconds": None, "size": 0,
        "blocked": None, "mentions_odds": False, "error": None,
    }
    start = time.time()

    attempts = [("normal", try_normal)]
    if USE_STEALTH_RETRY:
        attempts.append(("stealth", try_stealth))

    for method, fn in attempts:
        if time.time() - start > SITE_DEADLINE:
            result["error"] = "deadline reached"
            break
        try:
            status, text = fn(url)
            blocked = looks_blocked(status, text)
            result.update({
                "method": method,
                "status": status,
                "size": len(text),
                "blocked": blocked,
                "mentions_odds": "odds" in text.lower(),
                "error": None,
            })
            if status and status < 400 and not blocked:
                result["ok"] = True
                break
        except Exception as e:
            result["method"] = method
            result["error"] = f"{type(e).__name__}: {str(e)[:150]}"

    result["seconds"] = round(time.time() - start, 1)
    return result


def main():
    started = datetime.now(timezone.utc).isoformat()
    results = []

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(test_site, n, u): n for n, u in SITES.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # never let one site break the run
                results.append({"name": name, "ok": False,
                                "error": f"{type(e).__name__}: {e}"})

    results.sort(key=lambda r: r["name"])

    with open("results.json", "w", encoding="utf-8") as f:
        json.dump({"run_at": started, "results": results}, f, indent=2)

    print(f"\nRun at {started}\n")
    print(f"{'Site':<13}{'OK':<6}{'Method':<9}{'Status':<8}{'Secs':<7}{'Size':<9}{'Blocked':<9}{'Odds?':<7}Error")
    print("-" * 100)
    for r in results:
        print(f"{r['name']:<13}{str(r.get('ok')):<6}{str(r.get('method')):<9}"
              f"{str(r.get('status')):<8}{str(r.get('seconds')):<7}{str(r.get('size')):<9}"
              f"{str(r.get('blocked')):<9}{str(r.get('mentions_odds')):<7}{r.get('error') or ''}")

    worked = sum(1 for r in results if r.get("ok"))
    print(f"\n{worked}/{len(results)} sites reachable")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Unexpected error: {e}")
    sys.exit(0)  # always 0 so GitHub never emails a failure
