#!/usr/bin/env python3
"""
Devin usage guard and reporting.
Uses Devin API v3 consumption and metrics endpoints.
Falls back to static ACU tiers for estimates when the API/key scope is missing.
Labels every figure as (measured) or (static fallback) so you know what you are reading.

Devin Pro self-serve tracks a daily/weekly usage allowance, not a monthly ACU quota.
This module therefore guards on two axes:
  1. Session counts per day / week (DEVIN_DAILY_SESSION_LIMIT, DEVIN_WEEKLY_SESSION_LIMIT)
  2. Optional monthly ACU cap (DEVIN_MONTHLY_ACU_QUOTA) if you track ACUs manually.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

V3_BASE = "https://api.devin.ai/v3"
V1_BASE = "https://api.devin.ai/v1"

SIZE_MULTIPLIERS = {"small": 0.5, "medium": 1.0, "large": 2.0}
STATIC_ESTIMATES = {"small": 25, "medium": 75, "large": 200}


def get_env(name, required=False):
    value = os.environ.get(name, "")
    if required and not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def api_request(url, api_key, method="GET", payload=None, query=None):
    import urllib.request
    import urllib.error
    import urllib.parse

    if query:
        q = {k: v for k, v in query.items() if v is not None}
        if q:
            url = url + "?" + urllib.parse.urlencode(q)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"_http_error": e.code, "_http_body": body}
    except Exception as e:
        return {"_http_error": 0, "_http_body": str(e)}


def get_billing_start_pst():
    """Return a datetime for the start of the current billing cycle in PST.
    Devin billing cycles use midnight PST. We assume calendar-month billing here.
    Override by setting BILLING_START_UTC as an ISO timestamp if your cycle differs.
    """
    import zoneinfo

    override = os.environ.get("BILLING_START_UTC")
    if override:
        return datetime.fromisoformat(override)
    now = datetime.now(zoneinfo.ZoneInfo("America/Los_Angeles"))
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def get_window_start_pst(window):
    """Return start of today/this week/this month in PST as a timezone-aware datetime."""
    import zoneinfo

    now = datetime.now(zoneinfo.ZoneInfo("America/Los_Angeles"))
    if window == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "week":
        return (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if window == "month":
        return get_billing_start_pst()
    raise ValueError(f"Unknown window: {window}")


def _float_or_none(value):
    try:
        if value:
            return float(value)
    except (TypeError, ValueError):
        pass
    return None


def build_usage_status():
    api_key = get_env("DEVIN_API_KEY")
    org_id = get_env("DEVIN_ORG_ID")
    daily_limit = _float_or_none(get_env("DEVIN_DAILY_SESSION_LIMIT"))
    weekly_limit = _float_or_none(get_env("DEVIN_WEEKLY_SESSION_LIMIT"))
    quota = _float_or_none(get_env("DEVIN_MONTHLY_ACU_QUOTA"))

    if not api_key or not org_id:
        return {
            "ok": False,
            "error": "DEVIN_API_KEY and DEVIN_ORG_ID are required for measured usage.",
            "total_acus": None,
            "daily_sessions": None,
            "weekly_sessions": None,
            "monthly_sessions": None,
            "daily_limit": daily_limit,
            "weekly_limit": weekly_limit,
            "quota": quota,
            "avg_acu_per_session": None,
            "prs_created": None,
            "prs_merged": None,
            "searches": None,
        }

    now_utc = datetime.now(timezone.utc)
    time_before = int(now_utc.timestamp())

    month_start = get_window_start_pst("month")
    week_start = get_window_start_pst("week")
    day_start = get_window_start_pst("day")

    consumption = api_request(
        f"{V3_BASE}/organizations/{org_id}/consumption/daily",
        api_key,
        query={"time_after": int(month_start.timestamp()), "time_before": time_before},
    )

    def fetch_metrics(start):
        return api_request(
            f"{V3_BASE}/organizations/{org_id}/metrics/usage",
            api_key,
            query={"time_after": int(start.timestamp()), "time_before": time_before},
        )

    month_metrics = fetch_metrics(month_start)
    week_metrics = fetch_metrics(week_start)
    day_metrics = fetch_metrics(day_start)

    total_acus = None
    if "_http_error" not in consumption:
        total_acus = consumption.get("total_acus")

    sessions_day = None
    sessions_week = None
    sessions_month = None
    prs_created = None
    prs_merged = None
    searches = None

    if "_http_error" not in day_metrics:
        sessions_day = day_metrics.get("sessions_count")
    if "_http_error" not in week_metrics:
        sessions_week = week_metrics.get("sessions_count")
    if "_http_error" not in month_metrics:
        sessions_month = month_metrics.get("sessions_count")
        prs_created = month_metrics.get("prs_created_count")
        prs_merged = month_metrics.get("prs_merged_count")
        searches = month_metrics.get("searches_count")

    avg = None
    if total_acus is not None and sessions_month:
        avg = total_acus / sessions_month

    error_msg = None
    if "_http_error" in consumption:
        error_msg = f"consumption API returned {consumption['_http_error']}: {consumption.get('_http_body','')[:200]}"
    if "_http_error" in day_metrics:
        error_msg = (error_msg or "") + f" day metrics API returned {day_metrics['_http_error']}: {day_metrics.get('_http_body','')[:200]}"
    if "_http_error" in week_metrics:
        error_msg = (error_msg or "") + f" week metrics API returned {week_metrics['_http_error']}: {week_metrics.get('_http_body','')[:200]}"
    if "_http_error" in month_metrics:
        error_msg = (error_msg or "") + f" month metrics API returned {month_metrics['_http_error']}: {month_metrics.get('_http_body','')[:200]}"

    return {
        "ok": error_msg is None,
        "error": error_msg,
        "total_acus": total_acus,
        "daily_sessions": sessions_day,
        "weekly_sessions": sessions_week,
        "monthly_sessions": sessions_month,
        "daily_limit": daily_limit,
        "weekly_limit": weekly_limit,
        "quota": quota,
        "avg_acu_per_session": avg,
        "prs_created": prs_created,
        "prs_merged": prs_merged,
        "searches": searches,
    }


def format_status(status):
    lines = []
    if status.get("error"):
        lines.append(f"API error: {status['error']}")

    total = status.get("total_acus")
    sessions_day = status.get("daily_sessions")
    sessions_week = status.get("weekly_sessions")
    sessions_month = status.get("monthly_sessions")
    avg = status.get("avg_acu_per_session")
    daily_limit = status.get("daily_limit")
    weekly_limit = status.get("weekly_limit")
    quota = status.get("quota")

    if total is None:
        lines.append("Total ACUs this cycle: unknown")
    else:
        lines.append(f"Total ACUs this cycle: {total} (measured)")

    for label, value in [
        ("Today sessions", sessions_day),
        ("This week sessions", sessions_week),
        ("This cycle sessions", sessions_month),
    ]:
        if value is None:
            lines.append(f"{label}: unknown")
        else:
            lines.append(f"{label}: {value} (measured)")

    if daily_limit is not None and sessions_day is not None:
        lines.append(f"Daily session limit: {daily_limit} ({(sessions_day / daily_limit) * 100:.1f}% used)")
    elif daily_limit is not None:
        lines.append(f"Daily session limit: {daily_limit} (current usage unknown)")

    if weekly_limit is not None and sessions_week is not None:
        lines.append(f"Weekly session limit: {weekly_limit} ({(sessions_week / weekly_limit) * 100:.1f}% used)")
    elif weekly_limit is not None:
        lines.append(f"Weekly session limit: {weekly_limit} (current usage unknown)")

    if quota is not None:
        if total is not None:
            lines.append(f"Monthly ACU cap: {quota} ({(total / quota) * 100:.1f}% used)")
        else:
            lines.append(f"Monthly ACU cap: {quota} (usage unknown)")

    if avg is None:
        lines.append("Avg ACU/session: unavailable (will use static fallback for estimates)")
    else:
        lines.append(f"Avg ACU/session: {avg:.1f} (measured)")

    prs_c = status.get("prs_created")
    prs_m = status.get("prs_merged")
    if prs_c is not None:
        lines.append(f"PRs created: {prs_c} (measured)")
    if prs_m is not None:
        lines.append(f"PRs merged: {prs_m} (measured)")

    if daily_limit is None and weekly_limit is None and quota is None:
        lines.append(
            "No limits set. Set DEVIN_DAILY_SESSION_LIMIT, DEVIN_WEEKLY_SESSION_LIMIT, or "
            "DEVIN_MONTHLY_ACU_QUOTA to enable the usage guard."
        )
    return "\n".join(lines)


def classify_size(prompt, explicit=None):
    if explicit:
        return explicit
    p = prompt.lower()
    if any(w in p for w in ["small", "tiny", "quick", "fix typo", "one line", "one-line", "minor"]):
        return "small"
    if any(w in p for w in ["large", "huge", "big", "full", "complex", "architecture", "refactor everything", "rewrite"]):
        return "large"
    return "medium"


def estimate_task(prompt, explicit_size=None):
    status = build_usage_status()
    size = classify_size(prompt, explicit_size)

    # Each iMessage prompt creates one Devin session.
    estimate_sessions = 1.0

    source = "measured"
    if status.get("avg_acu_per_session"):
        estimate_acus = status["avg_acu_per_session"] * SIZE_MULTIPLIERS.get(size, 1.0)
    else:
        estimate_acus = STATIC_ESTIMATES.get(size, STATIC_ESTIMATES["medium"])
        source = "static fallback"

    daily_limit = status.get("daily_limit")
    weekly_limit = status.get("weekly_limit")
    quota = status.get("quota")
    daily_sessions = status.get("daily_sessions")
    weekly_sessions = status.get("weekly_sessions")
    total_acus = status.get("total_acus")

    result = {
        "size": size,
        "estimate_sessions": estimate_sessions,
        "estimate_acus": estimate_acus,
        "source": source,
        "daily_limit": daily_limit,
        "weekly_limit": weekly_limit,
        "quota": quota,
        "daily_after": None,
        "weekly_after": None,
        "monthly_acu_after": None,
        "percent_of_daily": None,
        "percent_of_weekly": None,
        "percent_of_quota": None,
        "ok": status.get("ok", True),
        "error": status.get("error"),
        "would_exceed": False,
    }

    if daily_limit is not None and daily_sessions is not None:
        result["daily_after"] = daily_sessions + estimate_sessions
        result["percent_of_daily"] = (estimate_sessions / daily_limit) * 100
        if result["daily_after"] > daily_limit:
            result["would_exceed"] = True

    if weekly_limit is not None and weekly_sessions is not None:
        result["weekly_after"] = weekly_sessions + estimate_sessions
        result["percent_of_weekly"] = (estimate_sessions / weekly_limit) * 100
        if result["weekly_after"] > weekly_limit:
            result["would_exceed"] = True

    if quota is not None and total_acus is not None:
        result["monthly_acu_after"] = total_acus + estimate_acus
        result["percent_of_quota"] = (estimate_acus / quota) * 100
        if result["monthly_acu_after"] > quota:
            result["would_exceed"] = True

    return result


def format_estimate(result):
    lines = [
        f"Estimated size: {result['size']}",
        f"Estimated sessions this task will use: {result['estimate_sessions']:.0f}",
    ]
    if result.get("error"):
        lines.append(f"API warning: {result['error']}")

    lines.append(f"Estimated ACUs: {result['estimate_acus']:.1f} ({result['source']})")

    if result["daily_limit"] is not None:
        if result["daily_after"] is not None:
            lines.append(
                f"Daily session limit after running: {result['daily_after']:.0f} / {result['daily_limit']} "
                f"({result['percent_of_daily']:.1f}% of one session)"
            )
        else:
            lines.append(f"Daily session limit: {result['daily_limit']} (current usage unknown)")

    if result["weekly_limit"] is not None:
        if result["weekly_after"] is not None:
            lines.append(
                f"Weekly session limit after running: {result['weekly_after']:.0f} / {result['weekly_limit']} "
                f"({result['percent_of_weekly']:.1f}% of one session)"
            )
        else:
            lines.append(f"Weekly session limit: {result['weekly_limit']} (current usage unknown)")

    if result["quota"] is not None:
        if result["monthly_acu_after"] is not None:
            lines.append(
                f"Monthly ACU cap after running: {result['monthly_acu_after']:.1f} / {result['quota']} "
                f"({result['percent_of_quota']:.1f}%)"
            )
        else:
            lines.append(f"Monthly ACU cap: {result['quota']} (usage unknown)")

    if result["would_exceed"]:
        lines.append("This task would exceed at least one of your configured limits. Reply GO to run anyway.")
    elif result["daily_limit"] is None and result["weekly_limit"] is None and result["quota"] is None:
        lines.append(
            "No limits set. Set DEVIN_DAILY_SESSION_LIMIT, DEVIN_WEEKLY_SESSION_LIMIT, or "
            "DEVIN_MONTHLY_ACU_QUOTA to enable the usage guard."
        )
    return "\n".join(lines)


_credits_cache: dict = {"ts": 0.0, "ok": None, "reason": ""}
_CREDITS_CACHE_TTL = 300  # re-check at most every 5 minutes


def devin_credits_ok() -> tuple[bool, str]:
    """Return (True, '') if Devin appears to have available capacity, or
    (False, reason_string) if the configured limits are exceeded.

    Checks against DEVIN_DAILY_SESSION_LIMIT, DEVIN_WEEKLY_SESSION_LIMIT, and
    DEVIN_MONTHLY_ACU_QUOTA.  Results are cached for 5 minutes so this is cheap
    to call on every incoming message.  If the API is unreachable the last known
    result is returned (defaults to available=True so one network blip doesn't
    block all coding tasks)."""
    import time

    now = time.time()
    if _credits_cache["ok"] is not None and (now - _credits_cache["ts"]) < _CREDITS_CACHE_TTL:
        return _credits_cache["ok"], _credits_cache["reason"]

    try:
        status = build_usage_status()
        daily_limit = status.get("daily_limit")
        weekly_limit = status.get("weekly_limit")
        quota = status.get("quota")
        daily_sessions = status.get("daily_sessions")
        weekly_sessions = status.get("weekly_sessions")
        total_acus = status.get("total_acus")

        if daily_limit is not None and daily_sessions is not None and daily_sessions >= daily_limit:
            reason = f"Devin daily session limit reached ({int(daily_sessions)}/{int(daily_limit)} sessions used today)."
            _credits_cache.update({"ts": now, "ok": False, "reason": reason})
            return False, reason

        if weekly_limit is not None and weekly_sessions is not None and weekly_sessions >= weekly_limit:
            reason = f"Devin weekly session limit reached ({int(weekly_sessions)}/{int(weekly_limit)} sessions used this week)."
            _credits_cache.update({"ts": now, "ok": False, "reason": reason})
            return False, reason

        if quota is not None and total_acus is not None and total_acus >= quota:
            reason = f"Devin monthly ACU quota reached ({total_acus:.0f}/{quota:.0f} ACUs used)."
            _credits_cache.update({"ts": now, "ok": False, "reason": reason})
            return False, reason

        _credits_cache.update({"ts": now, "ok": True, "reason": ""})
        return True, ""
    except Exception:
        # API error: don't change cached state; default to available if never checked.
        if _credits_cache["ok"] is None:
            return True, ""
        return _credits_cache["ok"], _credits_cache["reason"]


def invalidate_credits_cache():
    """Call this after a Devin session ends so the next request gets fresh data."""
    _credits_cache["ts"] = 0.0


def get_session(session_id):
    api_key = get_env("DEVIN_API_KEY")
    org_id = get_env("DEVIN_ORG_ID")
    if not api_key:
        return "DEVIN_API_KEY is required."
    if not org_id:
        return "DEVIN_ORG_ID is required for v3 session lookup."

    devin_id = session_id if session_id.startswith("devin-") else f"devin-{session_id}"
    resp = api_request(f"{V3_BASE}/organizations/{org_id}/sessions/{devin_id}", api_key)
    if "_http_error" in resp:
        return f"Session lookup failed ({resp['_http_error']}): {resp.get('_http_body','')[:500]}"

    lines = [f"Session: {resp.get('session_id', session_id)}"]
    lines.append(f"Status: {resp.get('status')} ({resp.get('status_detail')})")
    if resp.get("url"):
        lines.append(f"URL: {resp['url']}")
    prs = resp.get("pull_requests") or []
    if prs:
        lines.append("PRs:")
        for pr in prs:
            lines.append(f"- {pr.get('pr_url')} ({pr.get('pr_state')})")
    else:
        lines.append("PRs: none yet")
    created = resp.get("created_at")
    if created:
        try:
            dt = datetime.fromtimestamp(created)
            lines.append(f"Created: {dt.isoformat()}")
        except Exception:
            pass
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Devin usage guard")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show measured usage and session limits")
    p_est = sub.add_parser("estimate", help="Estimate ACU and session cost of a task")
    p_est.add_argument("task", help="Task description")
    p_est.add_argument("--size", choices=["small", "medium", "large"], help="Override size guess")
    p_sess = sub.add_parser("session", help="Get session details")
    p_sess.add_argument("id", help="Devin session ID")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "status":
        print(format_status(build_usage_status()))
    elif args.command == "estimate":
        print(format_estimate(estimate_task(args.task, args.size)))
    elif args.command == "session":
        print(get_session(args.id))


if __name__ == "__main__":
    main()
