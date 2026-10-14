import base64
import math
import threading
import time
import uuid
from collections import deque
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

EMAIL = "24f2005862@ds.study.iitm.ac.in"
TOTAL_ORDERS = 46
RATE_LIMIT = 16
WINDOW_SECONDS = 10.0

app = FastAPI(title="TDS GA2 Orders API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
    allow_credentials=False,
)

lock = threading.Lock()

CATALOG = [
    {"id": i, "item": f"Order item {i}", "amount": round(10.0 + i * 2.5, 2)}
    for i in range(1, TOTAL_ORDERS + 1)
]

idempotency_store = {}
request_times = {}


class OrderRequest(BaseModel):
    item: str = Field(default="Standard item", min_length=1)
    quantity: int = Field(default=1, ge=1)


def enforce_rate_limit(client_id: str):
    now = time.monotonic()

    with lock:
        timestamps = request_times.setdefault(client_id, deque())

        while timestamps and now - timestamps[0] >= WINDOW_SECONDS:
            timestamps.popleft()

        if len(timestamps) >= RATE_LIMIT:
            retry_after = max(
                1,
                math.ceil(WINDOW_SECONDS - (now - timestamps[0])),
            )
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

        timestamps.append(now)


@app.middleware("http")
async def client_rate_limit(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)

    client_id = request.headers.get("X-Client-Id", "").strip() or "anonymous"

    try:
        enforce_rate_limit(client_id)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    return await call_next(request)


@app.get("/")
def health():
    return {"status": "ok", "email": EMAIL}


@app.post("/orders", status_code=201)
def create_order(
    body: OrderRequest,
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key"
    ),
):
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    key = idempotency_key.strip()

    with lock:
        existing = idempotency_store.get(key)
        if existing is not None:
            return existing

        order = {
            "id": str(uuid.uuid4()),
            "item": body.item,
            "quantity": body.quantity,
            "status": "created",
        }
        idempotency_store[key] = order
        return order


@app.get("/orders")
def list_orders(
    limit: int = Query(default=10, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
):
    offset = 0

    if cursor:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            offset = int(base64.urlsafe_b64decode(padded.encode()).decode())
            if offset < 0 or offset > TOTAL_ORDERS:
                raise ValueError
        except (ValueError, UnicodeError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid cursor")

    items = CATALOG[offset:offset + limit]
    next_offset = offset + len(items)

    next_cursor = None
    if next_offset < TOTAL_ORDERS:
        next_cursor = base64.urlsafe_b64encode(
            str(next_offset).encode()
        ).decode().rstrip("=")

    return {"items": items, "next_cursor": next_cursor}
