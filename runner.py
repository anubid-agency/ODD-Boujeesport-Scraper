#!/usr/bin/env python3
"""
Isolated multi-source odds pipeline runner for GitHub Actions.
Uses Scrapling's Fetcher (fast HTTP) and optional StealthyFetcher (browser fallback)
with safe worker concurrency, timeout isolation, JSON/HTML extraction, and metadata filtering
to write out/matches.jsonl and out/health.json.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from scrapling.fetchers import Fetcher, StealthyFetcher


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event_id(source: str, league: str, home: str, away: str, start_time: Optional[str]) -> str:
    raw = f"{source}|{league}|{home}|{away}|{start_time or ''}".lower().strip()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def looks_like_metadata_payload(text: str) -> bool:
    """Reject obvious translation, dictionary, menu, jurisdiction, or config blobs."""
    head = text[:2500].lower()
    metadata_markers = (
        '"translations"',
        '"dictionary"',
        '"jurisdictions"',
        '"parametervaluemap"',
        '"sportmenu"',
        '"sport_menu"',
        '"betlines"',
    )
    return sum(1 for m in metadata_markers if m in head) >= 2


def parse_json_or_html_body(raw_body: Any) -> Tuple[Optional[Any], str]:
    if raw_body is None:
        return None, ""
    if isinstance(raw_body, (dict, list)):
        return raw_body, json.dumps(raw_body, ensure_ascii=False)
    if isinstance(raw_body, bytes):
        text = raw_body.decode("utf-8", errors="replace")
    else:
        text = str(raw_body)
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped), text
        except json.JSONDecodeError:
            return None, text
    return None, text


def normalize_match(
    *,
    source: str,
    home: str,
    away: str,
    sport: str = "football",
    league: str = "",
    start_time: Optional[str] = None,
    is_live: bool = False,
    markets: Optional[Dict[str, Any]] = None,
    event_id: Optional[str] = None,
    fetched_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    home_clean = str(home or "").strip()
    away_clean = str(away or "").strip()
    if not home_clean or not away_clean:
        return None
    ts = fetched_at or utc_now_iso()
    eid = str(event_id).strip() if event_id else make_event_id(source, league, home_clean, away_clean, start_time)
    return {
        "source": source,
        "event_id": eid,
        "sport": (sport or "football").strip().lower(),
        "league": str(league or "").strip(),
        "home": home_clean,
        "away": away_clean,
        "start_time": start_time,
        "is_live": bool(is_live),
        "markets": markets if isinstance(markets, dict) else {},
        "fetched_at": ts,
    }


def extract_matches_from_payload(source: str, raw_body: Any, fetched_at: str) -> List[Dict[str, Any]]:
    """
    Generic structural extractor for sportsbook JSON feeds and embedded state:
    - KBet-style list of match dicts with home_team / away_team / league / is_live / odds
    - SemaBet / D247Bet-style data.sports -> events
    - Forzza-style list items with matchOffers / competitors / teams
    - Generic recursive scan for objects with home/away or competitor pairs
    """
    parsed, text = parse_json_or_html_body(raw_body)
    if parsed is None:
        return []
    if looks_like_metadata_payload(text):
        raise ValueError("Payload identified as metadata/config rather than match feed")

    matches: List[Dict[str, Any]] = []
    seen_keys = set()

    def add_record(
        home: Any,
        away: Any,
        league: Any = "",
        sport: Any = "football",
        start_time: Any = None,
        is_live: Any = False,
        markets: Any = None,
        event_id: Any = None,
    ) -> None:
        rec = normalize_match(
            source=source,
            home=str(home or ""),
            away=str(away or ""),
            sport=str(sport or "football"),
            league=str(league or ""),
            start_time=str(start_time) if start_time else None,
            is_live=bool(is_live),
            markets=markets if isinstance(markets, dict) else {},
            event_id=str(event_id) if event_id is not None else None,
            fetched_at=fetched_at,
        )
        if not rec:
            return
        dedupe_key = (rec["event_id"], rec["home"].lower(), rec["away"].lower())
        if dedupe_key not in seen_keys:
            seen_keys.add(dedupe_key)
            matches.append(rec)

    def walk(node: Any, ctx_sport: str = "football", ctx_league: str = "", depth: int = 0) -> None:
        if depth > 7 or node is None:
            return
        if isinstance(node, dict):
            sport_val = node.get("sportName") or node.get("sport_name") or node.get("sport") or ctx_sport
            if isinstance(sport_val, dict):
                sport_val = sport_val.get("name") or ctx_sport
            league_val = (
                node.get("league")
                or node.get("leagueName")
                or node.get("competition")
                or node.get("tournament")
                or ctx_league
            )
            if isinstance(league_val, dict):
                league_val = league_val.get("name") or ctx_league

            home = node.get("home_team") or node.get("homeTeam") or node.get("home") or node.get("team1")
            away = node.get("away_team") or node.get("awayTeam") or node.get("away") or node.get("team2")

            if not (home and away):
                comps = node.get("competitors") or node.get("teams")
                if isinstance(comps, list) and len(comps) >= 2:
                    c0, c1 = comps[0], comps[1]
                    home = c0.get("name") if isinstance(c0, dict) else c0
                    away = c1.get("name") if isinstance(c1, dict) else c1

            if not (home and away):
                title = node.get("name") or node.get("matchName") or node.get("eventName")
                if isinstance(title, str):
                    parts = re.split(r"\s+(?:vs\.?|v|-)\s+", title, maxsplit=1)
                    if len(parts) == 2 and all(len(p.strip()) > 1 for p in parts):
                        home, away = parts[0].strip(), parts[1].strip()

            if home and away:
                odds_raw = node.get("odds") or node.get("markets") or node.get("matchOffers") or node.get("outcomes")
                markets_obj: Dict[str, Any] = {}
                if isinstance(odds_raw, dict):
                    markets_obj = odds_raw
                elif isinstance(odds_raw, list):
                    markets_obj = {"raw_offers": odds_raw}

                add_record(
                    home=home,
                    away=away,
                    league=league_val,
                    sport=sport_val,
                    start_time=node.get("date") or node.get("startTime") or node.get("start_time"),
                    is_live=node.get("is_live") or node.get("isLive") or node.get("live") or False,
                    markets=markets_obj,
                    event_id=node.get("id") or node.get("eventId") or node.get("matchId"),
                )

            for k, v in node.items():
                if k in ("odds", "markets", "matchOffers", "outcomes"):
                    continue
                walk(v, str(sport_val or "football"), str(league_val or ""), depth + 1)

        elif isinstance(node, list):
            for item in node:
                walk(item, ctx_sport, ctx_league, depth + 1)

    walk(parsed)
    return matches


def extract_html_matches(source: str, raw_body: Any, fetched_at: str) -> List[Dict[str, Any]]:
    """Extract match-pair patterns from HTML pages or SSR content."""
    _, html_text = parse_json_or_html_body(raw_body)
    if not html_text or len(html_text) < 200:
        return []
    matches: List[Dict[str, Any]] = []
    seen = set()
    for m in re.finditer(r">([^<]{2,45}?)\s+(?:vs\.?|[-–])\s+([^<]{2,45}?)<", html_text):
        home = re.sub(r"\s+", " ", m.group(1)).strip()
        away = re.sub(r"\s+", " ", m.group(2)).strip()
        if not home or not away or home.lower() == away.lower():
            continue
        rec = normalize_match(
            source=source,
            home=home,
            away=away,
            sport="football",
            fetched_at=fetched_at,
        )
        if rec and rec["event_id"] not in seen:
            seen.add(rec["event_id"])
            matches.append(rec)
    return matches


def fetch_with_scrapling(url: str, use_browser: bool, timeout_sec: int = 25) -> Tuple[int, Any]:
    """
    Fetch using Scrapling's Fetcher for fast HTTP or StealthyFetcher when browser mode is enabled.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; TECNO CAMON 20) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "application/json, text/html, */*;q=0.8",
        "Accept-Language": "en-UG,en;q=0.9",
    }

    if use_browser:
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=True,
            timeout=timeout_sec * 1000,
            extra_headers=headers,
        )
    else:
        page = Fetcher.get(
            url,
            headers=headers,
            timeout=timeout_sec,
            stealthy_headers=True,
        )

    status = int(getattr(page, "status", 0) or 0)
    body = getattr(page, "body", None)
    if body is None:
        body = getattr(page, "text", None) or getattr(page, "html_content", None) or ""
    return status, body


def classify_http_failure(status: int) -> str:
    if status == 403:
        return "blocked_403"
    if status == 521:
        return "upstream_down_521"
    if 500 <= status <= 599:
        return f"http_{status}"
    if status >= 400:
        return f"http_{status}"
    return "empty_or_invalid_response"


SOURCES: List[Dict[str, Any]] = [
    {
        "name": "ababet",
        "url": os.getenv("SOURCE_URL_ABABET", "https://ababet.ug/"),
        "prefer_browser": False,
        "parser": "html_matches",
    },
    {
        "name": "kagwirawo-kbet",
        "url": os.getenv("SOURCE_URL_KBET", "https://kbet.ug/api/v1/events/upcoming"),
        "prefer_browser": False,
        "parser": "generic_json",
    },
    {
        "name": "fortebet",
        "url": os.getenv("SOURCE_URL_FORTEBET", "https://www.fortebet.ug/api/web/v1/offer/full-prematch-en"),
        "prefer_browser": False,
        "parser": "generic_json",
    },
    {
        "name": "semabet-d247bet",
        "url": os.getenv("SOURCE_URL_SEMABET", "https://d247bet.ug/"),
        "prefer_browser": False,
        "parser": "auto",
        "timeout_sec": 35,
    },
    {
        "name": "forzza",
        "url": os.getenv("SOURCE_URL_FORZZA", "https://forzzalogin.com/"),
        "prefer_browser": True,
        "parser": "auto",
        "timeout_sec": 35,
    },
    {
        "name": "championbet",
        "url": os.getenv("SOURCE_URL_CHAMPIONBET", "https://championbet.ug/"),
        "prefer_browser": True,
        "parser": "auto",
        "timeout_sec": 35,
    },
    {
        "name": "betpawa",
        "url": os.getenv("SOURCE_URL_BETPAWA", "https://www.betpawa.ug/"),
        "prefer_browser": True,
        "parser": "auto",
        "timeout_sec": 35,
    },
    {
        "name": "22bet",
        "url": os.getenv("SOURCE_URL_22BET", "https://22bet.com/"),
        "prefer_browser": True,
        "parser": "auto",
        "timeout_sec": 35,
    },
    {
        "name": "betika",
        "url": os.getenv("SOURCE_URL_BETIKA", "https://www.betika.com/en-ke/"),
        "prefer_browser": False,
        "parser": "auto",
    },
    {
        "name": "sahara-games",
        "url": os.getenv("SOURCE_URL_SAHARA", "https://ug.saharagames.com/"),
        "prefer_browser": False,
        "parser": "auto",
    },
]


def parse_by_type(source: str, parser_type: str, body: Any, fetched_at: str) -> List[Dict[str, Any]]:
    if parser_type == "html_matches":
        return extract_html_matches(source=source, raw_body=body, fetched_at=fetched_at)
    if parser_type == "generic_json":
        return extract_matches_from_payload(source=source, raw_body=body, fetched_at=fetched_at)
    json_records = extract_matches_from_payload(source=source, raw_body=body, fetched_at=fetched_at)
    if json_records:
        return json_records
    return extract_html_matches(source=source, raw_body=body, fetched_at=fetched_at)


def run_single_source(spec: Dict[str, Any], allow_browser: bool, default_timeout_sec: int = 35) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    name = spec["name"]
    url = (spec.get("url") or "").strip()
    prefer_browser = bool(spec.get("prefer_browser", False))
    parser_type = spec.get("parser", "auto")
    src_timeout = int(spec.get("timeout_sec") or default_timeout_sec)
    started = time.monotonic()
    fetched_at = utc_now_iso()

    if not url:
        return [], {
            "source": name,
            "status": "not_configured",
            "mode": "none",
            "http_status": None,
            "match_count": 0,
            "duration_sec": round(time.monotonic() - started, 2),
            "error": "No endpoint URL configured yet",
            "updated_at": fetched_at,
        }

    attempts: List[Tuple[str, bool]] = []
    if prefer_browser and allow_browser:
        attempts.append(("browser", True))
    else:
        attempts.append(("http", False))
        if allow_browser:
            attempts.append(("browser", True))

    last_status_code: Optional[int] = None
    last_error = "Unknown fetch error"
    last_mode = attempts[0][0]

    for mode, use_browser in attempts:
        last_mode = mode
        try:
            code, body = fetch_with_scrapling(url=url, use_browser=use_browser, timeout_sec=src_timeout)
            last_status_code = code

            if code and 200 <= code < 300:
                records = parse_by_type(source=name, parser_type=parser_type, body=body, fetched_at=fetched_at)
                if records:
                    return records, {
                        "source": name,
                        "status": "ok",
                        "mode": mode,
                        "http_status": code,
                        "match_count": len(records),
                        "duration_sec": round(time.monotonic() - started, 2),
                        "error": None,
                        "updated_at": fetched_at,
                    }

                last_error = "HTTP 200 received but zero normalized matches extracted (empty or non-event payload)"
                if not use_browser and allow_browser:
                    continue

                return [], {
                    "source": name,
                    "status": "empty_feed",
                    "mode": mode,
                    "http_status": code,
                    "match_count": 0,
                    "duration_sec": round(time.monotonic() - started, 2),
                    "error": last_error,
                    "updated_at": fetched_at,
                }

            last_error = f"HTTP {code}"
            if code in (403, 429, 521) and not use_browser and allow_browser:
                continue

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            if not use_browser and allow_browser:
                continue

    status_label = classify_http_failure(last_status_code or 0) if last_status_code else "error"
    if "metadata" in last_error.lower():
        status_label = "metadata_only"

    return [], {
        "source": name,
        "status": status_label,
        "mode": last_mode,
        "http_status": last_status_code,
        "match_count": 0,
        "duration_sec": round(time.monotonic() - started, 2),
        "error": last_error,
        "updated_at": fetched_at,
    }


def main() -> None:
    out_dir = Path(os.getenv("OUTPUT_DIR", "out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    matches_path = out_dir / "matches.jsonl"
    health_path = out_dir / "health.json"

    allow_browser = os.getenv("RUN_BROWSER", "false").strip().lower() in ("1", "true", "yes")
    per_source_timeout = int(os.getenv("PER_SOURCE_TIMEOUT_SEC", "50"))
    # Keep concurrency conservative when headless browsers are enabled to avoid runner OOM/CPU stalls
    default_workers = 2 if allow_browser else 4
    max_workers = int(os.getenv("MAX_WORKERS", str(default_workers)))

    all_matches: List[Dict[str, Any]] = []
    health_entries: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        future_map = {
            pool.submit(run_single_source, spec, allow_browser, per_source_timeout): spec
            for spec in SOURCES
        }
        for future, spec in future_map.items():
            src_name = spec["name"]
            try:
                records, health = future.result(timeout=per_source_timeout)
                all_matches.extend(records)
                health_entries.append(health)
            except FuturesTimeoutError:
                health_entries.append({
                    "source": src_name,
                    "status": "timeout",
                    "mode": "browser" if allow_browser else "http",
                    "http_status": None,
                    "match_count": 0,
                    "duration_sec": per_source_timeout,
                    "error": f"Source timed out after {per_source_timeout}s",
                    "updated_at": utc_now_iso(),
                })
            except Exception as exc:
                health_entries.append({
                    "source": src_name,
                    "status": "crashed",
                    "mode": "browser" if allow_browser else "http",
                    "http_status": None,
                    "match_count": 0,
                    "duration_sec": None,
                    "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                    "updated_at": utc_now_iso(),
                })

    health_entries.sort(key=lambda item: item["source"])

    with matches_path.open("w", encoding="utf-8") as mf:
        for row in all_matches:
            mf.write(json.dumps(row, ensure_ascii=False) + "\n")

    ok_sources = [h for h in health_entries if h["status"] == "ok"]
    health_payload = {
        "generated_at": utc_now_iso(),
        "browser_fallback_enabled": allow_browser,
        "total_sources": len(health_entries),
        "working_sources": len(ok_sources),
        "total_matches": len(all_matches),
        "sources": health_entries,
    }
    health_path.write_text(json.dumps(health_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"Done: {len(ok_sources)}/{len(health_entries)} sources ok | "
        f"{len(all_matches)} total matches -> {matches_path}, {health_path}"
    )


if __name__ == "__main__":
    main()
