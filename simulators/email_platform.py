"""Simulated email marketing platform (System B).

Subscribers are keyed by email. Customers can unsubscribe here through the
link in each email, which is how this system and the consent platform drift apart.
"""
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import AwareDatetime, BaseModel, Field

from simulators.faults import Faults

app = FastAPI(title="Email platform simulator")
faults = Faults()

FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

subscribers: dict[str, dict] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(FMT)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def upsert(email: str, status: str, changed_at: str, source: str) -> dict:
    email = email.strip().lower()
    record = {
        "email": email,
        "status": status,
        "status_changed_at": changed_at,
        "source": source,
        "updated_at": now_iso(),
    }
    subscribers[email] = record
    return record


def seed() -> None:
    subscribers.clear()
    now = now_iso()
    # Deliberate differences against the consent platform seed data:
    #   carol  -> consent platform says withdrawn, here still subscribed (conflict)
    #   erin   -> exists only in the consent platform (glue should create it)
    #   frank  -> exists only here (unknown contact, goes to a review list)
    for email in ["alice@example.com", "bob@example.com", "carol@example.com",
                  "dave@example.com", "frank@example.com"]:
        upsert(email, "subscribed", now, "import")


seed()


class StatusUpdate(BaseModel):
    status: Literal["subscribed", "unsubscribed"]
    changed_at: AwareDatetime  # timezone required, naive times get a 422
    source: str


class FaultConfig(BaseModel):
    mode: Literal["500", "429", "422", "timeout", "drop_response"]
    probability: float = Field(1.0, ge=0, le=1)
    remaining: int | None = None
    hang_seconds: float = 10.0


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/subscribers")
async def list_subscribers(
    changed_since: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    await faults.before()
    rows = sorted(subscribers.values(), key=lambda s: (s["updated_at"], s["email"]))
    if changed_since:  # inclusive on purpose: the glue tolerates duplicates
        rows = [s for s in rows if s["updated_at"] >= changed_since]
    page = rows[offset:offset + limit]
    next_offset = offset + limit if offset + limit < len(rows) else None
    return {"items": page, "next_offset": next_offset}


@app.get("/subscribers/{email}")
async def get_subscriber(email: str):
    await faults.before()
    record = subscribers.get(email.strip().lower())
    if record is None:
        raise HTTPException(404, "subscriber not found")
    return record


@app.put("/subscribers/{email}/status")
async def set_status(email: str, body: StatusUpdate):
    await faults.before()
    # "set state" semantics (upsert): repeating the same call is harmless
    record = upsert(email, body.status, to_iso(body.changed_at), body.source)
    await faults.after()
    return record


@app.delete("/subscribers/{email}", status_code=204)
async def delete_subscriber(email: str):
    await faults.before()
    if subscribers.pop(email.strip().lower(), None) is None:
        raise HTTPException(404, "subscriber not found")
    await faults.after()
    return Response(status_code=204)


@app.post("/admin/simulate-unsubscribe/{email}")
async def simulate_unsubscribe(email: str):
    """Demo helper: a customer clicks the unsubscribe link in an email."""
    key = email.strip().lower()
    if key not in subscribers:
        raise HTTPException(404, "subscriber not found")
    return upsert(key, "unsubscribed", now_iso(), "unsubscribe_link")


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
    return {"status": "reseeded", "subscribers": len(subscribers)}
