"""Phase 0 spike: prove headless (no-browser) access to the Alex311 Aura API.

Steps:
  1. GET the service-requests page -> guest cookies + inline auraConfig (fwuid, context, token).
  2. POST getServiceRequestList_v2 for a 1-week window.
  3. POST getServiceRequest for one case number from the list.
  4. Download one photo from media_url using the same cookie jar.

Stdlib only (urllib), Python 3.14.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = "https://alex311.alexandriava.gov"
BOOTSTRAP_URL = BASE + "/customer/s/service-requests?servicerequested=all"
AURA_URL = BASE + "/customer/s/sfsites/aura"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

jar = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
opener.addheaders = [("User-Agent", UA)]


def bootstrap():
    with opener.open(BOOTSTRAP_URL, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")
    m = re.search(r"var auraConfig = ", html)
    if not m:
        sys.exit("FATAL: no auraConfig in bootstrap page")
    cfg, _ = json.JSONDecoder().raw_decode(html, m.end())
    ctx = cfg["context"]
    token = cfg.get("token")  # None for guests (csrfV2 path)
    token_cookie_name = cfg.get("eikoocnekot")
    if token_cookie_name:
        for c in jar:
            if c.name == token_cookie_name:
                token = c.value
    print(f"bootstrap ok: fwuid={ctx['fwuid'][:20]}... app={ctx.get('app')}")
    print(f"  token from config: {token!r}  token-cookie name: {token_cookie_name!r}")
    print(f"  cookies: {[c.name for c in jar]}")
    # Minimal aura.context the endpoint accepts
    aura_context = {
        "mode": ctx.get("mode", "PROD"),
        "fwuid": ctx["fwuid"],
        "app": ctx.get("app", "siteforce:communityApp"),
        "loaded": ctx.get("loaded", {}),
        "dn": [],
        "globals": {},
        "uad": False,
    }
    return aura_context, (token if token else "null")


def aura_call(aura_context, token, inner_method, inner_params, attempt_label):
    message = {"actions": [{
        "id": "1;a",
        "descriptor": "aura://ApexActionController/ACTION$execute",
        "callingDescriptor": "UNKNOWN",
        "params": {
            "namespace": "Incap311CZ",
            "classname": "Base311CZ_Service_Wrapper",
            "method": "handleRemoteWithoutCache",
            "params": {
                "method": inner_method,
                "params": json.dumps(inner_params),
            },
            "cacheable": False,
            "isContinuation": False,
        },
    }]}
    body = urllib.parse.urlencode({
        "message": json.dumps(message),
        "aura.context": json.dumps(aura_context),
        "aura.pageURI": "/customer/s/service-requests?servicerequested=all",
        "aura.token": token,
    }).encode()
    req = urllib.request.Request(AURA_URL, data=body, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": BOOTSTRAP_URL,
        "Origin": BASE,
        "X-Requested-With": "XMLHttpRequest",
    })
    with opener.open(req, timeout=60) as r:
        text = r.read().decode("utf-8", errors="replace")
    if text.startswith("*/"):  # anti-hijack prefix some orgs emit
        text = text.lstrip("*/ \n")
    env = json.loads(text)
    action = env["actions"][0]
    if action["state"] != "SUCCESS":
        errs = action.get("error") or []
        msg = errs[0].get("message") if errs else json.dumps(action)[:500]
        raise RuntimeError(f"{attempt_label}: state={action['state']}: {msg}")
    rv = action["returnValue"]["returnValue"]
    return json.loads(rv) if isinstance(rv, str) else rv


def main():
    aura_context, token = bootstrap()
    time.sleep(0.2)

    # --- Step 2: list, 1-week window ---
    list_params = [{
        "status": "Open,Closed",
        "createdBy": "Everyone",
        "page": 1,
        "page_size": 50,
        "my_service_request": False,
        "sort_field": "Incap311__Adjusted_Date_Time__c",
        "language": "EN",
        "start_date": "2026-06-29T04:00:00.000Z",
        "end_date": "2026-07-06T03:59:00.000Z",
    }]
    records = aura_call(aura_context, token, "getServiceRequestList_v2",
                        list_params, "getServiceRequestList_v2")
    if not isinstance(records, list) or not records:
        sys.exit(f"FATAL: list returned unexpected shape: {str(records)[:300]}")
    r0 = records[0]
    print(f"\nLIST OK: {len(records)} records on page 1")
    print(f"  first: {r0.get('service_request_id')} | {r0.get('service_name')} | "
          f"{r0.get('status')} | {r0.get('requested_datetime')} | {r0.get('address')!r}")
    json.dump(records, open("spike_list.json", "w"), indent=1)

    # pick a record with photos if possible, else the first
    with_media = next((r for r in records if r.get("media_url")), None)
    target = with_media or r0
    case_no = target["service_request_id"]
    time.sleep(0.3)

    # --- Step 3: detail ---
    detail = aura_call(aura_context, token, "getServiceRequest",
                       [{"language": "EN", "serviceRequestId": case_no}],
                       "getServiceRequest")
    if isinstance(detail, list):
        detail = detail[0]
    print(f"\nDETAIL OK: {case_no}")
    print(f"  description: {str(detail.get('description'))[:120]!r}")
    print(f"  contact (PII check, must be empty): {detail.get('contact')!r}")
    print(f"  media_url count: {len(detail.get('media_url') or [])}")
    json.dump(detail, open("spike_detail.json", "w"), indent=1)

    # --- Step 4: photo ---
    media = detail.get("media_url") or target.get("media_url") or []
    if not media:
        # scan other records' details? keep spike cheap: scan list records
        for r in records:
            if r.get("media_url"):
                media = r["media_url"]
                case_no = r["service_request_id"]
                break
    if media:
        m0 = media[0]
        url = m0["url"]
        print(f"\nPHOTO: {m0.get('fileName')} private={m0.get('private')} from {case_no}")
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": BOOTSTRAP_URL})
        with opener.open(req, timeout=60) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type")
        open("spike_photo.bin", "wb").write(data)
        magic = data[:4].hex()
        print(f"PHOTO OK: {len(data)} bytes, Content-Type={ctype}, magic={magic}")
    else:
        print("\nPHOTO: no record with media_url found in this window (inconclusive)")

    print("\n=== SPIKE PASSED (headless) ===")


if __name__ == "__main__":
    main()
