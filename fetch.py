"""Pull Deccansoft Home ad performance from the Meta Marketing API into data.json.

Runs every 5 minutes from .github/workflows/refresh.yml. Standard library only.
The token comes from the META_TOKEN environment variable (a repo secret) and is
never written to the output.

Lead quality (hot / warm / cold) comes from the AI-360 CRM's report endpoint,
keyed by CRM_REPORT_KEY (another repo secret). It returns counts only -- this
page is public, so no person is ever named. Without the key, or if the CRM is
down, the Meta half still publishes and the page says the CRM is not connected.
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

CRM_API = os.environ.get(
    "CRM_API", "https://ai-360-api-crm-gsazh5gwaua0f9c6.centralindia-01.azurewebsites.net/api"
).rstrip("/")
CRM_KEY = os.environ.get("CRM_REPORT_KEY", "").strip()
# The CRM recorded which ad brought each lead from this day on; earlier leads
# have no ad and can never get one. Every cost per hot lead uses spend from
# here too, so the two sides cover the same days.
CRM_SINCE = "2026-09-29"
RATINGS = ("hot", "warm", "cold", "free_only")

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


def crm_report():
    """The CRM's per-day, per-ad lead counts, or (None, why not)."""
    if not CRM_KEY:
        return None, "CRM_REPORT_KEY is not set"
    req = urllib.request.Request(
        f"{CRM_API}/internal/reports/ad-leads-daily?since={CRM_SINCE}",
        headers={"X-Report-Key": CRM_KEY},
    )
    try:
        # Generous: the App Service can take a minute to wake from idle.
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r)["rows"], None
    except urllib.error.HTTPError as e:
        return None, f"CRM answered HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - the Meta half must still publish
        return None, f"CRM unreachable ({type(e).__name__})"


def lead_quality(rows, daily, ads, ad_spend_since):
    """Join the CRM's counts to Meta's days and ads."""
    zero = lambda: {"leads": 0, "registered": 0, **{k: 0 for k in RATINGS}}  # noqa: E731
    by_day, by_ad = {}, {}
    for r in rows:
        d = by_day.setdefault(r["day"], {**zero(), "hotAds": {}})
        a = by_ad.setdefault(r["ad"], zero())
        for k in ("leads", "registered", *RATINGS):
            d[k] += r[k]
            a[k] += r[k]
        if r["hot"]:
            d["hotAds"][r["ad"]] = d["hotAds"].get(r["ad"], 0) + r["hot"]
    spend_by_day = {x["day"]: x for x in daily}
    days = []
    for day in sorted(set(by_day) | {d for d in spend_by_day if d >= CRM_SINCE}):
        m = spend_by_day.get(day, {})
        days.append({"day": day, "spend": m.get("spend", 0), "regs": m.get("regs", 0),
                     **by_day.get(day, {**zero(), "hotAds": {}})})
    names = {a["name"] for a in ads}
    for a in ads:
        a["crm"] = {**by_ad.get(a["name"], zero()), "spendSince": ad_spend_since.get(a["id"], 0)}
    unmatched = zero()
    for name, c in by_ad.items():
        if name not in names:
            for k in unmatched:
                unmatched[k] += c[k]
    return {"connected": True, "since": CRM_SINCE, "days": days, "unmatched": unmatched}


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

    rows, why_not = crm_report()
    if rows is None:
        quality = {"connected": False, "since": CRM_SINCE, "error": why_not}
    else:
        since_rows = get_all(f"{ACCOUNT}/insights", level="ad", limit=200, fields="ad_id,spend",
                             time_range=json.dumps({"since": CRM_SINCE, "until": today}))
        quality = lead_quality(rows, daily, ads,
                               {r["ad_id"]: round(float(r.get("spend", 0)), 2) for r in since_rows})

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
        "quality": quality,
    }
    with open("data.json", "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"ok {now:%d %b %H:%M} IST: {len(ads)} ads, {len(daily)} days, "
          f"today spend {data['today']['spend']}, CRM "
          + (f"{sum(d['hot'] for d in quality['days'])} hot" if quality["connected"]
             else f"not connected: {quality['error']}"))


if __name__ == "__main__":
    main()
