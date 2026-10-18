"""HTTP clients for the two systems.

Their job: talk to the API and classify failures, so the rest of the code
never deals with raw status codes.
  TransientError  retry later (429, 5xx, 408, timeout, connection problems)
  PermanentError  do not retry (other 4xx), the operation goes to dead-letter
404 is returned to the caller, because its meaning depends on the call.

Error messages never include URLs or bodies, because the email address is
part of the URL in the email platform and must not reach logs.
"""
from urllib.parse import quote

import httpx

from glue.db import from_iso, to_iso
from glue.policy import OPTED_IN, WITHDRAWN, SourceState


class TransientError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    pass


def _parse_retry_after(value) -> float | None:
    """Seconds only. The HTTP-date form of Retry-After is not supported."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


class BaseClient:
    name = "base"

    def __init__(self, base_url: str, *, timeout: float = 5.0, transport=None):
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            resp = self._http.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise TransientError(f"{self.name}: timeout ({type(exc).__name__})")
        except httpx.TransportError as exc:
            raise TransientError(f"{self.name}: connection problem ({type(exc).__name__})")
        code = resp.status_code
        if code == 429:
            raise TransientError(f"{self.name}: HTTP 429 rate limited",
                                 _parse_retry_after(resp.headers.get("Retry-After")))
        if code >= 500 or code == 408:
            raise TransientError(f"{self.name}: HTTP {code}")
        if code >= 400 and code != 404:
            raise PermanentError(f"{self.name}: HTTP {code} {resp.text[:200]}")
        return resp

    def _paged(self, url: str, since_param: str, since: str | None) -> list[dict]:
        items, offset = [], 0
        while offset is not None:
            params = {"limit": 100, "offset": offset}
            if since:
                params[since_param] = since
            data = self._request("GET", url, params=params).json()
            items.extend(data["items"])
            offset = data["next_offset"]
        return items


class ConsentClient(BaseClient):
    name = "consent"

    def fetch_changes(self, since: str | None) -> list[dict]:
        return self._paged("/contacts", "updated_since", since)

    def set_status(self, contact_id, status: str, at, source: str) -> None:
        self._request("POST", f"/contacts/{contact_id}/consent",
                      json={"status": status, "effective_at": to_iso(at), "source": source})

    def get(self, contact_id) -> dict | None:
        resp = self._request("GET", f"/contacts/{contact_id}")
        return None if resp.status_code == 404 else resp.json()

    def pending_erasures(self) -> list[dict]:
        return self._request("GET", "/erasure-requests", params={"status": "pending"}).json()["items"]

    def complete_erasure(self, request_id) -> None:
        self._request("POST", f"/erasure-requests/{request_id}/complete")


_TO_EMAIL = {OPTED_IN: "subscribed", WITHDRAWN: "unsubscribed"}
_FROM_EMAIL = {v: k for k, v in _TO_EMAIL.items()}


class EmailClient(BaseClient):
    name = "email"

    def fetch_changes(self, since: str | None) -> list[dict]:
        return self._paged("/subscribers", "changed_since", since)

    def get(self, email: str) -> dict | None:
        resp = self._request("GET", f"/subscribers/{quote(email, safe='')}")
        return None if resp.status_code == 404 else resp.json()

    def set_status(self, email: str, status: str, at, source: str) -> None:
        """Upsert: also creates the subscriber. Repeating it is harmless."""
        self._request("PUT", f"/subscribers/{quote(email, safe='')}/status",
                      json={"status": _TO_EMAIL[status], "changed_at": to_iso(at),
                            "source": source})

    def delete(self, email: str) -> bool:
        """True if deleted now, False if it was already gone (idempotent)."""
        resp = self._request("DELETE", f"/subscribers/{quote(email, safe='')}")
        return resp.status_code != 404


def consent_state(item: dict) -> SourceState:
    return SourceState(item["consent_status"], from_iso(item["consent_effective_at"]))


def email_state(item: dict) -> SourceState:
    return SourceState(_FROM_EMAIL[item["status"]], from_iso(item["status_changed_at"]))
