"""Licence validation, deactivation, onboarding and the Gumroad webhook.

Replaces the Supabase plpgsql RPCs. The behaviour is carried over; the debris
is not — the original logged every licence key in the table on every call,
built its lookup SQL with format() and execute(), and threaded a dead
p_device_id parameter through three functions.
"""

import json
import logging
import os
import re
import secrets
import time
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


# The single definition of "this key identifies this licence". Every endpoint
# that resolves a key uses it: three hand-copied predicates drifting apart
# would be a silent change to how licences are matched. $1 is the key.
KEY_MATCH = "normalize_license_key(license_key) = normalize_license_key($1)"

# Field names match the Swift Codable decoders in LicenseModels.swift and are
# not free to change.
LOOKUP_SQL = f"""
    select id, full_name, email, country, is_active, created_at,
           has_completed_onboarding
    from licenses
    where {KEY_MATCH}
"""


# Longest possible textual IPv6 address (an IPv4-mapped form with a zone id).
# Anything longer is not an address and must never become a dict key.
_MAX_KEY_LENGTH = 45

# The characters an IPv4 or IPv6 literal can be spelt with: hex digits, the
# IPv4 dot, the IPv6 colon, and % introducing a zone id.
_IP_SHAPED = re.compile(r"^[0-9A-Fa-f.:%]+$")


def _is_ip_shaped(value: str) -> bool:
    """Cheap plausibility check, not a parser.

    The point is not to validate addresses, it is to keep an attacker from
    choosing arbitrary dictionary keys: whatever survives this is short and
    drawn from a small alphabet, so the key space a spoofer can reach is
    bounded in both length and content.
    """
    return (
        bool(value)
        and len(value) <= _MAX_KEY_LENGTH
        and _IP_SHAPED.match(value) is not None
    )


class RateLimiter:
    """Per-key token bucket with a bounded number of keys.

    In-process, so it protects a single instance only. If this service ever
    runs more than one replica this needs to move to shared state.

    The bucket dict is attacker-keyed (see _client_key: X-Forwarded-For is
    spoofable by design), so it must not be allowed to grow without limit --
    an unauthenticated flood of distinct keys would otherwise exhaust the
    container's memory and take /search down with it.
    """

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = 10_000,
    ) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.clock = clock
        self.max_buckets = max_buckets
        # Insertion order is recency order: allow() re-inserts every key it
        # touches, so the oldest entry is the least recently seen.
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        tokens, last = self._buckets.pop(key, (float(self.capacity), now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)

        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            allowed = False
        else:
            self._buckets[key] = (tokens - 1.0, now)
            allowed = True

        if len(self._buckets) > self.max_buckets:
            self._evict(now)
        return allowed

    def reset(self) -> None:
        """Forget every bucket. For tests; the limiter is module-global."""
        self._buckets.clear()

    def _evict(self, now: float) -> None:
        # Trim to a low-water mark rather than to exactly the cap. Stopping at
        # the cap would mean this O(n) scan ran on every single request once
        # the dict was full -- which is exactly when the process is under the
        # flood this exists to survive. Freeing a tenth at a time amortises it.
        target = self.max_buckets - max(1, self.max_buckets // 10)

        # A bucket that has refilled to capacity is indistinguishable from a
        # key that has never been seen, so dropping it changes no answer --
        # the key simply gets a fresh full bucket next time it appears.
        for key, (tokens, last) in list(self._buckets.items()):
            if len(self._buckets) <= target:
                return
            refilled = tokens + (now - last) * self.refill_per_second
            if refilled >= self.capacity:
                del self._buckets[key]

        # Still over: everything left is mid-consumption, so drop the least
        # recently touched. Insertion order is recency order, and the key just
        # touched is at the end, so an active client is never the one dropped.
        while len(self._buckets) > target:
            del self._buckets[next(iter(self._buckets))]


# 30 validations per minute per IP. The app validates once a day; anything
# near this ceiling is not a real client.
_validate_limiter = RateLimiter(capacity=30, refill_per_second=0.5)

# The two mutating endpoints are far rarer than validation -- onboarding fires
# once in a licence's life and deactivation is a manual act -- but both answer
# "does this key exist?", which is exactly what validation is limited to
# protect. Separate buckets so abuse of one cannot lock a user out of the
# other. 10 burst, 6/minute sustained.
_deactivate_limiter = RateLimiter(capacity=10, refill_per_second=0.1)
_onboarding_limiter = RateLimiter(capacity=10, refill_per_second=0.1)


def _client_key(request: Request) -> str:
    """Bucket key for rate limiting.

    Railway terminates TLS at an edge proxy and uvicorn runs without
    --proxy-headers, so request.client.host is the proxy for every user.
    Keying on that would put all customers in one bucket. X-Forwarded-For
    is client-spoofable, which is an accepted trade-off: this limiter is a
    courtesy guard on an endpoint whose real secret is the licence key, and
    starving legitimate users is the worse failure.

    Spoofable, however, must not mean unbounded: anything that is not shaped
    like an IP address is discarded rather than turned into a dict key, so a
    caller cannot spend the process's memory a header at a time.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if _is_ip_shaped(first):
            return first
    peer = request.client.host if request.client else None
    if peer and _is_ip_shaped(peer):
        return peer
    return "unknown"


def _enforce_rate_limit(limiter: RateLimiter, request: Request) -> None:
    if not limiter.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail="Too many requests")


async def _pool_or_503() -> asyncpg.Pool:
    """The pool, or a 503 the client can tell apart from a business answer.

    Startup pool init is non-fatal and acquire_pool() re-tries lazily, so a
    request can legitimately arrive with no usable database. Letting that
    escape as a 500 tells the client "we broke", when the truthful answer is
    "ask again later" -- and the macOS app must not treat it as a verdict on
    the licence.
    """
    try:
        return await db.acquire_pool()
    except Exception as exc:  # asyncpg errors, DNS failures, missing DSN
        logger.error("licensing database unavailable: %s", exc)
        raise HTTPException(
            status_code=503, detail="Licensing temporarily unavailable"
        ) from exc


@router.post("/license/validate", response_model=ValidateResponse)
async def validate_license(payload: ValidateRequest, request: Request) -> ValidateResponse:
    _enforce_rate_limit(_validate_limiter, request)

    pool = await _pool_or_503()
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
async def deactivate_license(
    payload: DeactivateRequest, request: Request
) -> MutationResponse:
    # Unauthenticated and state-mutating: limited for the same reason
    # /license/validate is.
    _enforce_rate_limit(_deactivate_limiter, request)

    pool = await _pool_or_503()
    async with pool.acquire() as conn:
        # Email is matched alongside the key so that possession of a key alone
        # cannot deactivate a licence.
        updated = await conn.fetchval(
            f"""
            update licenses
            set is_active = false
            where {KEY_MATCH}
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
async def update_onboarding(
    payload: OnboardingRequest, request: Request
) -> MutationResponse:
    # This answers "does this key exist?" on a key alone -- the same oracle
    # /license/validate is limited to protect, with a write attached.
    _enforce_rate_limit(_onboarding_limiter, request)

    pool = await _pool_or_503()
    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            f"""
            update licenses
            set has_completed_onboarding = $2
            where {KEY_MATCH}
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

    pool = await _pool_or_503()
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
