"""Client for the Alex311 (Alexandria, VA) Salesforce Experience Cloud portal.

Every Salesforce/Aura quirk is isolated in this module: guest bootstrap,
fwuid rotation, the double-nested Apex envelope, the ~50-record page cap,
the "data volume too large" window limit, and the ``*/`` anti-hijack prefix.

Verified behaviors this client encodes (see spike/FINDINGS.md):
- Guests have no ``aura.token``; the literal string ``"null"`` is accepted.
  If Salesforce ever enables the token-cookie path (``eikoocnekot``), the
  bootstrap picks it up automatically.
- ``fwuid`` rotates on redeploys; an out-of-sync response triggers exactly
  one re-bootstrap + retry per call.
- The list endpoint requires a date window and caps page_size around 50;
  oversized windows return a volume error, which ``fetch_range`` handles by
  splitting the window in half (down to ``min_window``).
- Sort is by last-activity time, not request time — a window must always be
  paged to exhaustion; there is no early-stop.
- The server ignores ``service_name`` filters; filter client-side.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

import httpx

log = logging.getLogger("alex311.client")

DEFAULT_BASE_URL = "https://alex311.alexandriava.gov"
PATH_PREFIX = "/customer"
LIST_PAGE_SIZE = 50
DEFAULT_WINDOW = timedelta(days=14)
MIN_WINDOW = timedelta(hours=6)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

APEX_NAMESPACE = "Incap311CZ"
APEX_CLASSNAME = "Base311CZ_Service_Wrapper"
APEX_METHOD = "handleRemoteWithoutCache"


class Alex311Error(Exception):
    """Base error for this client."""


class BootstrapError(Alex311Error):
    """The guest bootstrap page could not be fetched or parsed."""


class OutOfSyncError(Alex311Error):
    """The server rejected our fwuid/context (portal was redeployed)."""


class AuraActionError(Alex311Error):
    """The Aura action returned state=ERROR."""

    def __init__(self, message: str, state: str = "ERROR"):
        super().__init__(message)
        self.state = state


class VolumeTooLargeError(AuraActionError):
    """The requested date window holds too many records for the endpoint."""


@dataclass
class AuraSession:
    context: dict[str, Any]
    token: str
    bootstrapped_at: datetime


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Alex311Client:
    """Guest-session client. Sequential by design (municipal portal — be gentle)."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        min_interval: float = 0.25,
        timeout: float = 60.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.bootstrap_url = (
            f"{self.base_url}{PATH_PREFIX}/s/service-requests?servicerequested=all"
        )
        self.aura_url = f"{self.base_url}{PATH_PREFIX}/s/sfsites/aura"
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self._session: AuraSession | None = None
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Alex311Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------- bootstrap

    def bootstrap(self) -> AuraSession:
        """Fetch the guest page; parse auraConfig for fwuid/context/token."""
        self._throttle()
        try:
            resp = self._http.get(self.bootstrap_url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise BootstrapError(f"bootstrap page fetch failed: {e}") from e

        html = resp.text
        marker = "var auraConfig = "
        i = html.find(marker)
        if i == -1:
            raise BootstrapError("no 'var auraConfig' in bootstrap page (layout changed?)")
        try:
            cfg, _ = json.JSONDecoder().raw_decode(html, i + len(marker))
        except json.JSONDecodeError as e:
            raise BootstrapError(f"auraConfig did not parse as JSON: {e}") from e

        ctx = cfg.get("context") or {}
        if "fwuid" not in ctx:
            raise BootstrapError("auraConfig.context has no fwuid")

        token = cfg.get("token")
        # Salesforce may deliver the CSRF token via a cookie whose name is in
        # auraConfig["eikoocnekot"] ("tokencookie" reversed). Not currently
        # enabled for guests on this portal, but handle it if it appears.
        token_cookie_name = cfg.get("eikoocnekot")
        if not token and token_cookie_name:
            token = self._http.cookies.get(token_cookie_name)

        self._session = AuraSession(
            context={
                "mode": ctx.get("mode", "PROD"),
                "fwuid": ctx["fwuid"],
                "app": ctx.get("app", "siteforce:communityApp"),
                "loaded": ctx.get("loaded", {}),
                "dn": [],
                "globals": {},
                "uad": False,
            },
            token=token or "null",
            bootstrapped_at=datetime.now(timezone.utc),
        )
        log.info("bootstrap ok: fwuid=%s...", self._session.context["fwuid"][:16])
        return self._session

    # ------------------------------------------------------------ aura calls

    def _throttle(self) -> None:
        wait = self._last_request_at + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _post_action(self, inner_method: str, inner_params: list[dict]) -> Any:
        """One Aura POST. Raises OutOfSyncError if the fwuid is stale."""
        if self._session is None:
            self.bootstrap()
        assert self._session is not None

        message = {
            "actions": [
                {
                    "id": "1;a",
                    "descriptor": "aura://ApexActionController/ACTION$execute",
                    "callingDescriptor": "UNKNOWN",
                    "params": {
                        "namespace": APEX_NAMESPACE,
                        "classname": APEX_CLASSNAME,
                        "method": APEX_METHOD,
                        "params": {
                            "method": inner_method,
                            "params": json.dumps(inner_params),
                        },
                        "cacheable": False,
                        "isContinuation": False,
                    },
                }
            ]
        }
        self._throttle()
        resp = self._http.post(
            self.aura_url,
            data={
                "message": json.dumps(message),
                "aura.context": json.dumps(self._session.context),
                "aura.pageURI": f"{PATH_PREFIX}/s/service-requests?servicerequested=all",
                "aura.token": self._session.token,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": self.bootstrap_url,
                "Origin": self.base_url,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if resp.status_code in (401, 403):
            raise OutOfSyncError(f"HTTP {resp.status_code} from aura endpoint")
        resp.raise_for_status()

        text = resp.text
        if text.startswith("*/"):  # anti-hijack prefix
            text = text.lstrip("*/ \n")
        try:
            env = json.loads(text)
        except json.JSONDecodeError as e:
            if "clientOutOfSync" in text or "invalidSession" in text:
                raise OutOfSyncError("non-JSON out-of-sync response") from e
            raise Alex311Error(f"unparseable aura response: {text[:200]!r}") from e

        if env.get("exceptionEvent"):
            desc = str(env.get("event", {}).get("descriptor", ""))
            if "clientOutOfSync" in desc or "invalidSession" in desc:
                raise OutOfSyncError(f"aura exception event: {desc}")
            raise Alex311Error(f"aura exception event: {json.dumps(env)[:300]}")

        action = env["actions"][0]
        if action["state"] != "SUCCESS":
            errors = action.get("error") or []
            msg = errors[0].get("message", "") if errors else json.dumps(action)[:300]
            if "volume" in msg.lower():
                raise VolumeTooLargeError(msg)
            raise AuraActionError(msg, state=action["state"])

        rv = action["returnValue"]
        if isinstance(rv, dict) and "returnValue" in rv:
            rv = rv["returnValue"]
        return json.loads(rv) if isinstance(rv, str) else rv

    def _call(self, inner_method: str, inner_params: list[dict]) -> Any:
        """_post_action + retry policy: one re-bootstrap on out-of-sync,
        exponential backoff on transient HTTP/network errors."""
        rebootstrapped = False
        attempt = 0
        while True:
            try:
                return self._post_action(inner_method, inner_params)
            except OutOfSyncError:
                if rebootstrapped:
                    raise
                log.warning("out of sync (portal redeploy?); re-bootstrapping")
                rebootstrapped = True
                self.bootstrap()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                    raise
                attempt += 1
                if attempt > self.max_retries:
                    raise
                delay = min(2 ** attempt, 30)
                log.warning("transient error (%s); retry %d/%d in %ds",
                            e, attempt, self.max_retries, delay)
                time.sleep(delay)

    # ------------------------------------------------------------- list/detail

    def list_page(
        self,
        start: datetime,
        end: datetime,
        page: int,
        *,
        status: str = "Open,Closed",
        page_size: int = LIST_PAGE_SIZE,
    ) -> list[dict]:
        """One page (1-indexed) of the list for a date window."""
        result = self._call(
            "getServiceRequestList_v2",
            [
                {
                    "status": status,
                    "createdBy": "Everyone",
                    "page": page,
                    "page_size": page_size,
                    "my_service_request": False,
                    "sort_field": "Incap311__Adjusted_Date_Time__c",
                    "language": "EN",
                    "start_date": _iso_z(start),
                    "end_date": _iso_z(end),
                }
            ],
        )
        if result is None:
            return []
        if not isinstance(result, list):
            raise Alex311Error(f"list returned non-list: {str(result)[:200]}")
        return result

    def iter_window(self, start: datetime, end: datetime, **kw) -> Iterator[dict]:
        """All records in one window, paging to exhaustion.

        Raises VolumeTooLargeError if the window is too big — callers should
        use fetch_range, which splits adaptively.
        """
        page = 1
        while True:
            records = self.list_page(start, end, page, **kw)
            yield from records
            if len(records) < LIST_PAGE_SIZE:
                return
            page += 1

    def fetch_range(
        self,
        start: datetime,
        end: datetime,
        *,
        window: timedelta = DEFAULT_WINDOW,
        min_window: timedelta = MIN_WINDOW,
        on_window: Callable[[datetime, datetime, int], None] | None = None,
    ) -> dict[str, dict]:
        """Every record in [start, end), deduped by service_request_id.

        Slices the range into windows, pages each to exhaustion, and halves
        any window that trips the server's volume cap. Records seen twice
        (window overlap, pagination drift) keep the last-read copy.
        """
        results: dict[str, dict] = {}
        stack: list[tuple[datetime, datetime]] = []
        cursor = start
        while cursor < end:
            w_end = min(cursor + window, end)
            stack.append((cursor, w_end))
            cursor = w_end
        stack.reverse()  # process chronologically

        while stack:
            w_start, w_end = stack.pop()
            try:
                count = 0
                for rec in self.iter_window(w_start, w_end):
                    key = rec.get("service_request_id")
                    if key:
                        results[key] = rec
                        count += 1
                if on_window:
                    on_window(w_start, w_end, count)
                log.info("window %s → %s: %d records (total %d)",
                         w_start.date(), w_end.date(), count, len(results))
            except VolumeTooLargeError:
                span = w_end - w_start
                if span <= min_window:
                    raise
                mid = w_start + span / 2
                log.info("volume cap on %s → %s; splitting", w_start, w_end)
                stack.append((mid, w_end))
                stack.append((w_start, mid))
        return results

    def get_detail(self, service_request_id: str) -> dict:
        """Full record (description, media, etc.) for a plain case number."""
        result = self._call(
            "getServiceRequest",
            [{"language": "EN", "serviceRequestId": service_request_id}],
        )
        if isinstance(result, list):
            if not result:
                raise Alex311Error(f"empty detail for {service_request_id}")
            result = result[0]
        if not isinstance(result, dict):
            raise Alex311Error(f"detail returned non-dict: {str(result)[:200]}")
        return result

    # ----------------------------------------------------------------- media

    def fetch_media(self, url: str) -> tuple[bytes, str]:
        """Download a media file server-side (no CORS) with the guest jar."""
        if self._session is None:
            self.bootstrap()
        attempt = 0
        while True:
            self._throttle()
            try:
                resp = self._http.get(url, headers={"Referer": self.bootstrap_url})
                resp.raise_for_status()
                return resp.content, resp.headers.get("Content-Type", "")
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                    raise
                attempt += 1
                if attempt > self.max_retries:
                    raise
                time.sleep(min(2 ** attempt, 30))

    # ------------------------------------------------------------------ misc

    @staticmethod
    def deep_link(service_request_id: str, base_url: str = DEFAULT_BASE_URL) -> str:
        """Stable public URL for one request (plain case number works)."""
        return (
            f"{base_url}{PATH_PREFIX}/s/service-request-details"
            f"?c__prePageName=service_requests__c"
            f"&c__srNumber={service_request_id}&servicerequested=all"
        )
