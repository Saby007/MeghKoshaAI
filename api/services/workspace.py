"""Anonymous browser capabilities isolate uploaded reports without a user login."""

import hashlib
import hmac
import os
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, Response

from services import assessment_store
from services.auth import ClientPrincipal

COOKIE_NAME = "mkai_workspace"
current: ContextVar[str | None] = ContextVar("manual_focus_workspace", default=None)


def namespace() -> str:
    value = current.get()
    if value is None:
        raise RuntimeError("An active FOCUS workspace is required.")
    return value


def lifetime_days() -> int:
    days = int(os.environ.get("WORKSPACE_TTL_DAYS", "7"))
    if not 1 <= days <= 90:
        raise ValueError("WORKSPACE_TTL_DAYS must be between 1 and 90.")
    return days


@dataclass(frozen=True)
class Session:
    workspace_id: str
    capability: str = field(repr=False)

    @property
    def csrf_token(self) -> str:
        return hashlib.sha256(f"csrf:{self.capability}".encode("ascii")).hexdigest()

    @property
    def principal(self) -> ClientPrincipal:
        return ClientPrincipal(
            user_id=self.workspace_id, user_details="FOCUS workspace",
            tenant_id=self.workspace_id, entra_object_id=self.workspace_id,
        )


async def resolve(request: Request) -> Session:
    value = request.cookies.get(COOKIE_NAME, "")
    try:
        identifier, capability = value.split(".", 1)
        workspace_id = UUID(identifier)
        if len(capability) != 43 or not capability.isascii():
            raise ValueError("Invalid capability.")
    except (ValueError, AttributeError):
        raise HTTPException(401, "Open the application to initialize your report workspace.") from None
    row = await (await assessment_store.pool(for_read=False)).fetchrow(
        "SELECT capability_hash FROM assessments.workspaces WHERE workspace_id=$1 AND expires_at>now()",
        workspace_id,
    )
    digest = hashlib.sha256(capability.encode("ascii")).hexdigest()
    if row is None or not hmac.compare_digest(row["capability_hash"], digest):
        raise HTTPException(401, "This report workspace expired. Open the application and upload your report again.")
    return Session(str(workspace_id), capability)


def require_csrf(request: Request, session: Session) -> None:
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied.isascii() or not hmac.compare_digest(supplied, session.csrf_token):
        raise HTTPException(403, "The workspace request could not be verified. Reload the application and retry.")


async def initialize(request: Request, response: Response) -> dict:
    from agents.cost_agent import narration_status

    session = None
    if request.cookies.get(COOKIE_NAME):
        try:
            session = await resolve(request)
        except HTTPException as error:
            if error.status_code != 401:
                raise
    if session is None:
        session = Session(str(uuid4()), secrets.token_urlsafe(32))
        db = await assessment_store.pool(for_read=False)
        async with db.acquire(timeout=10) as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(74027263)")
            count = await connection.fetchval("SELECT count(*) FROM assessments.workspaces WHERE expires_at>now()")
            maximum = int(os.environ.get("WORKSPACE_MAX_ACTIVE", "250"))
            if not 1 <= maximum <= 10000:
                raise ValueError("WORKSPACE_MAX_ACTIVE must be between 1 and 10000.")
            if count >= maximum:
                raise HTTPException(503, "Workspace capacity is full. Contact the deployment operator.",
                                    headers={"Retry-After": "60"})
            await connection.execute(
                "INSERT INTO assessments.workspaces(workspace_id,capability_hash,expires_at) "
                "VALUES($1,$2,now()+make_interval(days=>$3))",
                UUID(session.workspace_id), hashlib.sha256(session.capability.encode("ascii")).hexdigest(),
                lifetime_days(),
            )
        response.set_cookie(
            COOKIE_NAME, f"{session.workspace_id}.{session.capability}", httponly=True,
            secure=os.environ.get("WORKSPACE_COOKIE_SECURE", "true").lower() != "false",
            samesite="strict", max_age=lifetime_days() * 86400, path="/",
        )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    return {
        "userId": session.workspace_id, "tenantId": session.workspace_id,
        "userDetails": "FOCUS workspace", "csrfToken": session.csrf_token,
        "features": {"assessmentMode": "postgres", "manualFocus": True,
                     "aiNarration": narration_status() == "configured"},
    }
