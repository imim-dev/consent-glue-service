"""Simulated consent management platform (System A).

Imitates a typical SaaS API: list changes since a timestamp, paging,
set consent state, erasure requests. Data is in memory and re-seeded on start.
"""
import itertools
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, Field

from simulators.faults import Faults

app = FastAPI(title="Consent platform simulator")
faults = Faults()

FMT = "%Y-%m-%dT%H:%M:%S.%fZ"  # fixed width, so strings sort like timestamps

contacts: dict[int, dict] = {}
erasure_requests: dict[int, dict] = {}
_contact_ids = itertools.count(1)
_request_ids = itertools.count(1)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(FMT)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def add_contact(email: str, status: str) -> dict:
    now = now_iso()
    contact = {
        "id": next(_contact_ids),
        "email": email.strip().lower(),
        "consent_status": status,
        "consent_effective_at": now,
        "consent_source": "seed",
        "updated_at": now,
    }
    contacts[contact["id"]] = contact
    return contact


def seed() -> None:
    global _contact_ids, _request_ids
    contacts.clear()
    erasure_requests.clear()
    _contact_ids = itertools.count(1)
    _request_ids = itertools.count(1)
    for email, status in [
        ("alice@example.com", "opted_in"),
        ("bob@example.com", "opted_in"),
        ("carol@example.com", "withdrawn"),
        ("dave@example.com", "opted_in"),
        ("erin@example.com", "opted_in"),
    ]:
        add_contact(email, status)


seed()


class NewContact(BaseModel):
    email: str
    consent_status: Literal["opted_in", "withdrawn"] = "opted_in"


class ConsentUpdate(BaseModel):
    status: Literal["opted_in", "withdrawn"]
    effective_at: AwareDatetime  # timezone is required, naive times get a 422
    source: str


class NewErasure(BaseModel):
    email: str


class FaultConfig(BaseModel):
    mode: Literal["500", "429", "422", "timeout", "drop_response"]
    probability: float = Field(1.0, ge=0, le=1)
    remaining: int | None = None
    hang_seconds: float = 10.0


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/contacts")
async def list_contacts(
    updated_since: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    await faults.before()
    rows = sorted(contacts.values(), key=lambda c: (c["updated_at"], c["id"]))
    if updated_since:  # inclusive on purpose: the glue tolerates duplicates
        rows = [c for c in rows if c["updated_at"] >= updated_since]
    page = rows[offset:offset + limit]
    next_offset = offset + limit if offset + limit < len(rows) else None
    return {"items": page, "next_offset": next_offset}


@app.get("/contacts/{contact_id}")
async def get_contact(contact_id: int):
    await faults.before()
    if contact_id not in contacts:
        raise HTTPException(404, "contact not found")
    return contacts[contact_id]


@app.post("/contacts", status_code=201)
async def create_contact(body: NewContact):
    await faults.before()
    email = body.email.strip().lower()
    if any(c["email"] == email for c in contacts.values()):
        raise HTTPException(409, "contact already exists")
    contact = add_contact(email, body.consent_status)
    await faults.after()
    return contact


@app.post("/contacts/{contact_id}/consent")
async def set_consent(contact_id: int, body: ConsentUpdate):
    await faults.before()
    contact = contacts.get(contact_id)
    if contact is None:
        raise HTTPException(404, "contact not found")
    # "set state" semantics: repeating the same call is harmless
    contact["consent_status"] = body.status
    contact["consent_effective_at"] = to_iso(body.effective_at)
    contact["consent_source"] = body.source
    contact["updated_at"] = now_iso()
    await faults.after()
    return contact


@app.post("/erasure-requests", status_code=201)
async def create_erasure_request(body: NewErasure):
    await faults.before()
    now = datetime.now(timezone.utc)
    request = {
        "id": next(_request_ids),
        "email": body.email.strip().lower(),
        "status": "pending",
        "requested_at": to_iso(now),
        "deadline_at": to_iso(now + timedelta(days=30)),
        "completed_at": None,
    }
    erasure_requests[request["id"]] = request
    return request


@app.get("/erasure-requests")
async def list_erasure_requests(status: str | None = None):
    await faults.before()
    rows = list(erasure_requests.values())
    if status:
        rows = [r for r in rows if r["status"] == status]
    return {"items": rows}


@app.post("/erasure-requests/{request_id}/complete")
async def complete_erasure_request(request_id: int):
    await faults.before()
    request = erasure_requests.get(request_id)
    if request is None:
        raise HTTPException(404, "erasure request not found")
    if request["status"] != "completed":
        request["status"] = "completed"
        request["completed_at"] = now_iso()
    await faults.after()
    return request


@app.post("/admin/faults")
async def set_faults(cfg: FaultConfig):
    faults.configure(cfg.mode, cfg.probability, cfg.remaining, cfg.hang_seconds)
    return faults.state()


@app.delete("/admin/faults")
async def clear_faults():
    faults.reset()
    return faults.state()


@app.post("/admin/reset")
async def reset_data():
    seed()
    return {"status": "reseeded", "contacts": len(contacts)}
