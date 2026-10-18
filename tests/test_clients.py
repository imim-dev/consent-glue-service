import json
from datetime import datetime, timezone

import httpx
import pytest

from glue.clients import (ConsentClient, EmailClient, PermanentError,
                          TransientError, email_state)
from glue.policy import OPTED_IN, WITHDRAWN, SourceState

AT = datetime(2026, 10, 18, 10, 5, tzinfo=timezone.utc)


def email_client(handler):
    return EmailClient("http://email", transport=httpx.MockTransport(handler))


def consent_client(handler):
    return ConsentClient("http://consent", transport=httpx.MockTransport(handler))


def test_429_is_transient_and_keeps_retry_after():
    client = email_client(lambda r: httpx.Response(429, headers={"Retry-After": "2"}))
    with pytest.raises(TransientError) as exc:
        client.set_status("a@example.com", WITHDRAWN, AT, "s")
    assert exc.value.retry_after == 2.0


def test_500_is_transient_without_retry_after():
    client = email_client(lambda r: httpx.Response(500))
    with pytest.raises(TransientError) as exc:
        client.set_status("a@example.com", WITHDRAWN, AT, "s")
    assert exc.value.retry_after is None


def test_timeout_is_transient():
    def handler(request):
        raise httpx.ReadTimeout("boom")
    with pytest.raises(TransientError):
        email_client(handler).set_status("a@example.com", WITHDRAWN, AT, "s")


def test_422_is_permanent():
    client = consent_client(lambda r: httpx.Response(422, text="bad timestamp"))
    with pytest.raises(PermanentError) as exc:
        client.set_status(1, WITHDRAWN, AT, "s")
    assert "422" in str(exc.value)


def test_delete_treats_404_as_already_done():
    assert email_client(lambda r: httpx.Response(204)).delete("a@example.com") is True
    assert email_client(lambda r: httpx.Response(404)).delete("a@example.com") is False


def test_set_status_sends_wire_format():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    email_client(handler).set_status("a@example.com", WITHDRAWN, AT, "sync_from_consent")
    assert seen["method"] == "PUT"
    assert seen["body"] == {"status": "unsubscribed",
                            "changed_at": "2026-10-18T10:05:00.000000Z",
                            "source": "sync_from_consent"}


def test_paging_follows_next_offset():
    pages = {0: {"items": [{"n": 1}, {"n": 2}], "next_offset": 2},
             2: {"items": [{"n": 3}], "next_offset": None}}

    def handler(request):
        return httpx.Response(200, json=pages[int(request.url.params["offset"])])

    items = consent_client(handler).fetch_changes(None)
    assert [i["n"] for i in items] == [1, 2, 3]


def test_error_messages_do_not_leak_the_email_address():
    client = email_client(lambda r: httpx.Response(422, text="invalid"))
    with pytest.raises(PermanentError) as exc:
        client.set_status("secret@example.com", WITHDRAWN, AT, "s")
    assert "secret" not in str(exc.value)


def test_email_state_maps_wire_status_to_canonical():
    item = {"status": "unsubscribed", "status_changed_at": "2026-10-18T10:05:00.000000Z"}
    assert email_state(item) == SourceState(WITHDRAWN, AT)
    item["status"] = "subscribed"
    assert email_state(item).status == OPTED_IN
