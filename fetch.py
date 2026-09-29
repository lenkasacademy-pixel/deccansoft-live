"""Pull Deccansoft Home ad performance from the Meta Marketing API into data.json.

Runs every 5 minutes from .github/workflows/refresh.yml. Standard library only.
The token comes from the META_TOKEN environment variable (a repo secret) and is
never written to the output.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://graph.facebook.com/v21.0"
ACCOUNT = "act_1864644430272738"
START_DAY = "2026-09-22"  # reporting starts here; older days are deliberately left out
IST = timezone(timedelta(hours=5, minutes=30))
TOKEN = os.environ.get("META_TOKEN", "").strip()

FIELDS = "spend,impressions,reach,inline_link_clicks,actions"


def get(path, **params):
    params["access_token"] = TOKEN
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = json.load(e)
        err = body.get("error", {})
        sys.exit(f"Meta API error {err.get('code')}: {err.get('message')}")


def get_all(path, **params):
    out, page = [], get(path, **params)
    while True:
        out += page.get("data", [])
        nxt = page.get("paging", {}).get("next")
        if not nxt:
            return out
        with urllib.request.urlopen(nxt, timeout=60) as r:
            page = json.load(r)


def action(row, kind):
    for a in row.get("actions", []):
        if a["action_type"] == kind:
            return int(float(a["value"]))
    return 0


def metrics(row):
    return {
        "spend": round(float(row.get("spend", 0)), 2),
        "impr": int(row.get("impressions", 0)),
        "reach": int(row.get("reach", 0)),
        "clicks": int(row.get("inline_link_clicks", 0)),
        "lpv": action(row, "landing_page_view"),
        "leads": action(row, "lead"),
        "regs": action(row, "complete_registration"),
    }


def totals(since, until):
    rows = get_all(f"{ACCOUNT}/insights", level="account", fields=FIELDS,
                   time_range=json.dumps({"since": since, "until": until}))
    return metrics(rows[0]) if rows else metrics({})


def main():
    if not TOKEN:
        sys.exit("META_TOKEN is not set")
    now = datetime.now(IST)
    today = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    span = json.dumps({"since": START_DAY, "until": today})

    daily = [
        {"day": r["date_start"], **metrics(r)}
        for r in get_all(f"{ACCOUNT}/insights", level="account", fields=FIELDS,
                         time_range=span, time_increment=1, limit=100)
    ]

    def by_hour(day):
        rows = get_all(f"{ACCOUNT}/insights", level="account", fields=FIELDS.replace("reach,", ""),
                       time_range=json.dumps({"since": day, "until": day}), limit=100,
                       breakdowns="hourly_stats_aggregated_by_advertiser_time_zone")
        return [{"hour": int(r["hourly_stats_aggregated_by_advertiser_time_zone"][:2]), **metrics(r)}
                for r in rows]

    hourly = by_hour(today)
    # Yesterday up to the same clock time, so the morning is not compared with a full day.
    so_far = {k: 0 for k in metrics({})}
    for h in by_hour(yesterday):
        if h["hour"] < now.hour:
            for k in so_far:
                so_far[k] += h[k]
    so_far["spend"] = round(so_far["spend"], 2)

    # Per-ad totals since START_DAY. Insights only lists ads that delivered, so
    # names/status/thumbnails come from a second call on exactly those ids.
    ad_rows = get_all(f"{ACCOUNT}/insights", level="ad", time_range=span, limit=200,
                      fields=f"ad_id,ad_name,adset_name,campaign_name,{FIELDS}")
    today_rows = {r["ad_id"]: r for r in get_all(
        f"{ACCOUNT}/insights", level="ad", date_preset="today", limit=200,
        fields=f"ad_id,{FIELDS}")}
    info = {}
    ids = [r["ad_id"] for r in ad_rows]
    for i in range(0, len(ids), 50):
        for ad in get_all(f"{ACCOUNT}/ads", limit=50,
                          fields="effective_status,creative{thumbnail_url}",
                          filtering=json.dumps([{"field": "id", "operator": "IN",
                                                 "value": ids[i:i + 50]}])):
            info[ad["id"]] = ad
    ads = []
    for r in ad_rows:
        meta = info.get(r["ad_id"], {})
        adset = r.get("adset_name", "")
        ads.append({
            "id": r["ad_id"],
            "name": r.get("ad_name", ""),
            "adset": adset,
            "campaign": r.get("campaign_name", ""),
            "group": "quiz" if adset.lower().startswith("quiz") else
                     "webinar" if adset.lower().startswith("webinar") else "other",
            "status": meta.get("effective_status", ""),
            "img": meta.get("creative", {}).get("thumbnail_url", ""),
            "today": metrics(today_rows.get(r["ad_id"], {})),
            **metrics(r),
        })
    ads.sort(key=lambda a: -a["spend"])

    acct = get(ACCOUNT, fields="account_status,amount_spent,spend_cap")
    cap = int(acct.get("spend_cap") or 0)
    limit = {"cap": cap / 100, "spent": int(acct["amount_spent"]) / 100,
             "left": (cap - int(acct["amount_spent"])) / 100} if cap else None

    data = {
        "generated": now.isoformat(timespec="seconds"),
        "account": {"id": ACCOUNT[4:], "name": "Deccansoft Home", "currency": "INR",
                    "status": acct.get("account_status"), "spendLimit": limit},
        "startDay": START_DAY,
        "today": totals(today, today),
        "yesterday": totals(yesterday, yesterday),
        "yesterdaySoFar": {**so_far, "untilHour": now.hour},
        "total": totals(START_DAY, today),
        "daily": daily,
        "hourly": hourly,
        "ads": ads,
    }
    with open("data.json", "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"ok {now:%d %b %H:%M} IST: {len(ads)} ads, {len(daily)} days, "
          f"today spend {data['today']['spend']}")


if __name__ == "__main__":
    main()
