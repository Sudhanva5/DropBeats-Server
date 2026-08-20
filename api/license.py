"""Licence validation, deactivation, onboarding and the Gumroad webhook.

Replaces the Supabase plpgsql RPCs. The behaviour is carried over; the debris
is not — the original logged every licence key in the table on every call,
built its lookup SQL with format() and execute(), and threaded a dead
p_device_id parameter through three functions.
"""

import json
import logging
import os
import secrets
import time
from collections import defaultdict
from typing import Callable

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import db

logger = logging.getLogger(__name__)

router = APIRouter()


class ValidateRequest(BaseModel):
    key: str


class ValidateResponse(BaseModel):
    valid: bool
    error: str | None = None
    name: str | None = None
    email: str | None = None
    country: str | None = None
    created_at: str | None = None
    has_completed_onboarding: bool | None = None


# Field names match the Swift Codable decoders in LicenseModels.swift and are
# not free to change.
LOOKUP_SQL = """
    select id, full_name, email, country, is_active, created_at,
           has_completed_onboarding
    from licenses
    where normalize_license_key(license_key) = normalize_license_key($1)
"""


class RateLimiter:
    """Per-key token bucket.

    In-process, so it protects a single instance only. If this service ever
    runs more than one replica this needs to move to shared state.
    """

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        tokens, last = self._buckets.get(key, (float(self.capacity), now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)

        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False

        self._buckets[key] = (tokens - 1.0, now)
        return True


# 30 validations per minute per IP. The app validates once a day; anything
# near this ceiling is not a real client.
_validate_limiter = RateLimiter(capacity=30, refill_per_second=0.5)


def _client_key(request: Request) -> str:
    """Bucket key for rate limiting.

    Railway terminates TLS at an edge proxy and uvicorn runs without
    --proxy-headers, so request.client.host is the proxy for every user.
    Keying on that would put all customers in one bucket. X-Forwarded-For
    is client-spoofable, which is an accepted trade-off: this limiter is a
    courtesy guard on an endpoint whose real secret is the licence key, and
    starving legitimate users is the worse failure.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


@router.post("/license/validate", response_model=ValidateResponse)
async def validate_license(payload: ValidateRequest, request: Request) -> ValidateResponse:
    if not _validate_limiter.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail="Too many requests")

    pool = await db.acquire_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(LOOKUP_SQL, payload.key)

        if row is None:
            logger.info("licence validation: unknown key")
            return ValidateResponse(valid=False, error="Invalid license key")

        if not row["is_active"]:
            logger.info("licence validation: inactive licence %s", row["id"])
            return ValidateResponse(valid=False, error="License is not active")

        await conn.execute(
            "update licenses set last_login = now() where id = $1", row["id"]
        )

        logger.info("licence validation: ok for %s", row["id"])
        return ValidateResponse(
            valid=True,
            name=row["full_name"],
            email=row["email"],
            country=row["country"],
            created_at=row["created_at"].isoformat(timespec="seconds"),
            has_completed_onboarding=row["has_completed_onboarding"],
        )


class DeactivateRequest(BaseModel):
    key: str
    email: str


class MutationResponse(BaseModel):
    success: bool
    message: str = ""
    error: str | None = None


class OnboardingRequest(BaseModel):
    key: str
    completed: bool


@router.post("/license/deactivate", response_model=MutationResponse)
async def deactivate_license(payload: DeactivateRequest) -> MutationResponse:
    pool = await db.acquire_pool()
    async with pool.acquire() as conn:
        # Email is matched alongside the key so that possession of a key alone
        # cannot deactivate a licence.
        updated = await conn.fetchval(
            """
            update licenses
            set is_active = false
            where normalize_license_key(license_key) = normalize_license_key($1)
              and lower(email) = lower($2)
            returning id
            """,
            payload.key,
            payload.email,
        )

    if updated is None:
        return MutationResponse(success=False, error="License not found")
    logger.info("licence deactivated: %s", updated)
    return MutationResponse(success=True, message="License deactivated")


@router.post("/license/onboarding", response_model=MutationResponse)
async def update_onboarding(payload: OnboardingRequest) -> MutationResponse:
    pool = await db.acquire_pool()
    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            """
            update licenses
            set has_completed_onboarding = $2
            where normalize_license_key(license_key) = normalize_license_key($1)
            returning id
            """,
            payload.key,
            payload.completed,
        )

    if updated is None:
        return MutationResponse(success=False, error="License not found")
    return MutationResponse(success=True, message="Onboarding status updated")


@router.post("/webhooks/gumroad/{secret}", response_model=MutationResponse)
async def gumroad_webhook(secret: str, request: Request) -> MutationResponse:
    """Gumroad sale notification.

    Two independent factors guard this: an unguessable path segment and a
    seller_id check. Gumroad does not sign its pings, so the secret path is
    what stands in for a signature.
    """
    expected_secret = os.environ.get("GUMROAD_WEBHOOK_SECRET", "")
    # compare_digest so the path segment cannot be recovered by timing. Compare
    # bytes, not str: compare_digest raises TypeError on non-ASCII str input,
    # and a percent-decoded path segment can contain non-ASCII characters —
    # letting that exception escape as a 500 would be a one-request oracle
    # distinguishing "route exists" from "route doesn't exist".
    if not expected_secret or not secrets.compare_digest(
        secret.encode(), expected_secret.encode()
    ):
        # 404 rather than 403: an attacker probing paths learns nothing about
        # whether this route exists.
        raise HTTPException(status_code=404, detail="Not found")

    form = await request.form()
    payload = dict(form)

    pool = await db.acquire_pool()
    async with pool.acquire() as conn:
        # Logged before any validation, so a rejected webhook is still
        # evidence. The original design got this right.
        await conn.execute(
            "insert into webhook_logs (payload, success) values ($1::jsonb, $2)",
            json.dumps(payload),
            True,
        )

        expected_seller = os.environ.get("GUMROAD_SELLER_ID", "")
        # Fail closed: an unset env var must not turn into "" == "" matching
        # an attacker's explicitly empty seller_id field.
        if not expected_seller or payload.get("seller_id") != expected_seller:
            logger.warning("gumroad webhook: seller_id mismatch")
            await conn.execute(
                "insert into webhook_logs (payload, success, error) "
                "values ($1::jsonb, false, $2)",
                json.dumps({"event": "seller_verification_failed"}),
                "Invalid seller ID",
            )
            return MutationResponse(success=False, error="Invalid seller ID")

        email = payload.get("email")
        license_key = payload.get("license_key")
        sale_id = payload.get("sale_id")
        if not email or not license_key or not sale_id:
            await conn.execute(
                "insert into webhook_logs (payload, success, error) "
                "values ($1::jsonb, false, $2)",
                json.dumps({"event": "missing_fields"}),
                "Missing email, license_key, or sale_id",
            )
            return MutationResponse(
                success=False, error="Missing email, license_key, or sale_id"
            )

        full_name = payload.get("full_name") or email.split("@")[0]

        # Upsert on sale_id makes Gumroad retries idempotent. This only covers
        # the sale_id arbiter, though: license_key carries its own unique
        # constraint (and a normalize_license_key expression index), so a
        # different sale reusing a license_key still violates a constraint
        # that ON CONFLICT (sale_id) cannot catch. That is a genuine data
        # conflict, not a transient error, but Gumroad only stops retrying on
        # a 2xx, so it must not surface as a 500.
        try:
            await conn.execute(
                """
                insert into licenses (email, full_name, country, license_key, sale_id)
                values ($1, $2, $3, $4, $5)
                on conflict (sale_id) do update
                set email      = excluded.email,
                    full_name  = excluded.full_name,
                    country    = excluded.country,
                    license_key = excluded.license_key
                """,
                email,
                full_name,
                payload.get("country_code") or "Unknown",
                license_key,
                sale_id,
            )
        except asyncpg.UniqueViolationError as exc:
            logger.warning("gumroad webhook: unique violation on upsert: %s", exc)
            await conn.execute(
                "insert into webhook_logs (payload, success, error) "
                "values ($1::jsonb, false, $2)",
                json.dumps({"event": "unique_violation"}),
                str(exc),
            )
            return MutationResponse(success=False, error="License already exists")

    logger.info("gumroad webhook: licence created or updated")
    return MutationResponse(success=True, message="License created")
