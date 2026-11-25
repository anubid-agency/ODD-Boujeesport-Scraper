"""
Bookmaker reachability test (v2).

Fixes over v1:
  - Detects block pages by FINAL URL too (e.g. 1xbet.com/en/block), so a
    redirect to a block page is no longer counted as "OK".
  - Uses the BOOKMAKER_PROXY secret (if set) as a fallback, only for sites
    that fail or get blocked when going direct.
  - Fewer retries per attempt, so one dead site (AbaBet took 155s) can't drag on.
  - Proxy credentials are masked in all output.
  - Real listing URLs found in the last run (GSB, ChampionBet).

Every site runs on its own. One failing never stops the others.
The script always exits 0, so GitHub never sends a failure email.
Writes results.json and prints a table.

Attempt order per site (stops at the first success):
  1. normal   (direct)
  2. normal   via proxy      (only if BOOKMAKER_PROXY is set)
  3. stealth  (direct, real browser)
  4. stealth  via proxy      (only if BOOKMAKER_PROXY is set)

BOOKMAKER_PROXY format: http://user:password@host:port
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from scrapling.fetchers import Fetcher

# --- EDIT THESE: use the page that lists football matches / odds ---
# Batch 1 was tested already (6 of 10 reachable). Kept here for re-testing.
BATCH_1 = {
    "BetPawa":     "https://www.betpawa.ug/",
    "22Bet":       "https://22bet.com/",
    "1xBet":       "https://1xbet.com/",
    "Betika":      "https://www.betika.com/",
    "ForteBet":    "https://fortebet.ug/",
    "ChampionBet": "https://www.championbet.ug/desk/",
    "AbaBet":      "https://ababet.com/",
    "BangBet":     "https://www.bangbet.com/",
    "GSB":         "https://gsb.ug/sportsbook/upcoming",
    "Kagwirawo":   "https://kagwirawo.com/",
}

# Batch 2. These addresses are GUESSES (searching did not confirm them).
# If a site shows "address not found", the URL is wrong: open the real site
# in your browser, copy its address, and paste it here.
BATCH_2 = {
    "BongoBongo":  "https://bongobongo.ug/",
    "BetVita":     "https://betvita.com/",
    "BetPaka":     "https://betpaka.com/",
    "Forzza":      "https://forzza.ug/",
    "UgandaBet":   "https://ugandabet.com/",
    "SemaBet":     "https://semabet.co.ug/",
    "SmartBet":    "https://smartbet.co.ug/",
    "TopBet":      "https://topbets.ug/",
    "Premier Bet": "https://premierbet.ug/",
    "Sahara Games": "https://saharagames.com/",
}

# Batch 3: corrected addresses supplied by the owner (re-test of batch 2 failures).
# SemaBet and Forzza each list a backup address, in case the first one fails.
# SmartBet dropped (no official site). UgandaBet and BetPaka still need real URLs.
BATCH_3 = {
    "Sahara Games":    "https://ug.saharagames.com/",
    "SemaBet":         "https://semabet.com/",
    "SemaBet (.ug)":   "https://www.semabet.ug/",
    "Forzza":          "https://forzza1.com/",
    "Forzza (mobile)": "https://m.forzza1.com/",
}

# Which batch to test. Default is batch 3.
# Set the env var SITES_BATCH to 1 or 2 to re-run the older batches.
_BATCHES = {"1": BATCH_1, "2": BATCH_2, "3": BATCH_3}
SITES = _BATCHES.get(os.environ.get("SITES_BATCH", "3").strip(), BATCH_3)

PROXY = (os.environ.get("BOOKMAKER_PROXY") or "").strip() or None

TIMEOUT_SECONDS = 25      # per request
SITE_DEADLINE = 120       # hard limit per site, all attempts together
RETRIES = 1               # retries inside one attempt (kept low on purpose)
USE_STEALTH_RETRY = True  # try the real-browser fetcher if normal fails
MAX_WORKERS = 5

BLOCK_TEXT_MARKERS = [
    "just a moment", "attention required", "access denied", "cf-chl",
    "captcha", "verify you are human", "checking your browser",
    "not available in your country", "unusual traffic", "request blocked",
    "not available in your region", "restricted in your",
]
BLOCK_URL_MARKERS = ["/block", "blocked", "forbidden", "geo-restrict", "not-available"]


def scrub(msg):
    """Remove proxy credentials from any text before printing or saving."""
    msg = str(msg)
    if PROXY:
        msg = msg.replace(PROXY, "[proxy]")
    return re.sub(r"://[^@/\s]+@", "://***@", msg)


def looks_blocked(status, text, final_url):
    if status in (401, 403, 429, 451, 503):
        return True
    url = (final_url or "").lower()
    if any(m in url for m in BLOCK_URL_MARKERS):
        return True
    low = (text or "").lower()
    return any(m in low for m in BLOCK_TEXT_MARKERS)


def read_page(page, requested_url):
    status = getattr(page, "status", None)
    final_url = str(getattr(page, "url", "") or requested_url)
    try:
        text = page.html_content
    except Exception:
        try:
            text = bytes(getattr(page, "body", b"")[:200000]).decode("utf-8", "ignore")
        except Exception:
            text = ""
    return status, text or "", final_url


def call_dropping_retries(fn, url, **kwargs):
    """Some Scrapling versions may not accept 'retries'; drop it if so."""
    try:
        return fn(url, **kwargs)
    except TypeError:
        kwargs.pop("retries", None)
        return fn(url, **kwargs)


def try_normal(url, proxy):
    kwargs = {"timeout": TIMEOUT_SECONDS, "stealthy_headers": True, "retries": RETRIES}
    if proxy:
        kwargs["proxy"] = proxy
    page = call_dropping_retries(Fetcher.get, url, **kwargs)
    return read_page(page, url)


def try_stealth(url, proxy):
    from scrapling.fetchers import StealthyFetcher
    kwargs = {"headless": True, "network_idle": True,
              "timeout": TIMEOUT_SECONDS * 1000, "retries": RETRIES}
    if proxy:
        kwargs["proxy"] = proxy
    page = call_dropping_retries(StealthyFetcher.fetch, url, **kwargs)
    return read_page(page, url)


def build_attempts():
    attempts = [("normal", try_normal, None)]
    if PROXY:
        attempts.append(("normal+proxy", try_normal, PROXY))
    if USE_STEALTH_RETRY:
        attempts.append(("stealth", try_stealth, None))
        if PROXY:
            attempts.append(("stealth+proxy", try_stealth, PROXY))
    return attempts


def test_site(name, url):
    result = {
        "name": name, "url": url, "ok": False, "method": None,
        "status": None, "final_url": None, "seconds": None, "size": 0,
        "blocked": None, "mentions_odds": False, "error": None,
    }
    start = time.time()

    for method, fn, proxy in build_attempts():
        if time.time() - start > SITE_DEADLINE:
            result["error"] = "deadline reached"
            break
        try:
            status, text, final_url = fn(url, proxy)
            blocked = looks_blocked(status, text, final_url)
            result.update({
                "method": method,
                "status": status,
                "final_url": scrub(final_url),
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
            result["error"] = scrub(f"{type(e).__name__}: {str(e)[:150]}")

    err = (result.get("error") or "").lower()
    if any(k in err for k in ("could not resolve", "name_not_resolved", "name or service not known")):
        result["error"] = "address not found - check the URL. " + result["error"]

    result["seconds"] = round(time.time() - start, 1)
    return result


def short(s, n=60):
    s = str(s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def main():
    started = datetime.now(timezone.utc).isoformat()
    results = []
    print(f"Proxy configured: {'yes' if PROXY else 'no'}")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(test_site, n, u): n for n, u in SITES.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # never let one site break the run
                results.append({"name": name, "ok": False,
                                "error": scrub(f"{type(e).__name__}: {e}")})

    results.sort(key=lambda r: r["name"])

    with open("results.json", "w", encoding="utf-8") as f:
        json.dump({"run_at": started, "proxy_used": bool(PROXY), "results": results},
                  f, indent=2)

    print(f"\nRun at {started}\n")
    print(f"{'Site':<13}{'OK':<7}{'Method':<15}{'Status':<8}{'Secs':<7}{'Size':<9}"
          f"{'Blocked':<9}{'Odds?':<7}Final URL / Error")
    print("-" * 120)
    for r in results:
        tail = r.get("error") or r.get("final_url") or ""
        print(f"{r['name']:<13}{str(r.get('ok')):<7}{str(r.get('method')):<15}"
              f"{str(r.get('status')):<8}{str(r.get('seconds')):<7}{str(r.get('size')):<9}"
              f"{str(r.get('blocked')):<9}{str(r.get('mentions_odds')):<7}{short(tail, 70)}")

    worked = sum(1 for r in results if r.get("ok"))
    print(f"\n{worked}/{len(results)} sites reachable")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Unexpected error: {scrub(e)}")
    sys.exit(0)  # always 0 so GitHub never emails a failure
