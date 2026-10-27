"""Admission and request budgets for the opt-in PostgreSQL API mode."""

import asyncio
import logging

import asyncpg
from azure.core.exceptions import AzureError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from services.assessment_limits import AssessmentLimitError
from services.assessment_settings import scalable_mode, settings
from services.assessment_store import QueueFullError, DatabaseConfigurationError
from services.assessment_request_context import dashboard_read

logger = logging.getLogger(__name__)


class AssessmentHTTPBudget:
    def __init__(self, app: ASGIApp):
        self.app = app
        self.semaphore: asyncio.Semaphore | None = None
        self.work_semaphore: asyncio.Semaphore | None = None
        self.heavy_semaphore = asyncio.Semaphore(2)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope["path"] in {"/api/health", "/api/ready", "/api/auth/config"}
                or not scope["path"].startswith("/api/") or not scalable_mode()):
            await self.app(scope, receive, send)
            return
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(settings().api_concurrency)
            self.work_semaphore = asyncio.Semaphore(settings().work_concurrency)
        path = scope["path"]
        read = scope["method"] == "GET" and (
            path in {"/api/report/latest", "/api/auth/me", "/api/subscriptions"}
            or path.startswith("/api/assessments/") and not path.endswith(("/records", "/archive", "/interactive"))
        )
        semaphore = self.semaphore if read else self.work_semaphore
        if semaphore is None:
            raise RuntimeError("API admission budgets were not initialized.")
        heavy = (scope["path"].startswith(("/api/report/", "/api/chat", "/api/finops"))
                 and scope["path"] != "/api/report/latest") or scope["path"].endswith("/interactive")
        heavy_acquired = False
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=0.1 if read else 1.0)
        except TimeoutError:
            logger.warning("Assessment API admission limit reached")
            await JSONResponse({"detail": "API is busy. Retry shortly; your saved report is unchanged."},
                               status_code=429, headers={"Retry-After": "2"})(scope, receive, send)
            return
        started = False
        context_token = dashboard_read.set(read)

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                if not any(key.lower() == b"cache-control" for key, _ in headers):
                    headers.append((b"cache-control", b"private, no-store"))
                if not any(key.lower() == b"vary" for key, _ in headers):
                    headers.append((b"vary", b"Authorization"))
                message["headers"] = headers
            await send(message)

        try:
            if heavy:
                try:
                    await asyncio.wait_for(self.heavy_semaphore.acquire(), timeout=0.1)
                    heavy_acquired = True
                except TimeoutError:
                    logger.warning("Full-snapshot interactive API budget reached")
                    await JSONResponse({"detail": "Report export/chat capacity is busy. Retry shortly."},
                                       status_code=429, headers={"Retry-After": "2"})(scope, receive, send)
                    return
            if scope["method"] in {"POST", "PUT", "PATCH"}:
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > 65536:
                        logger.warning("Assessment API request body budget exceeded")
                        await JSONResponse({"detail": "Request exceeds the 64 KiB input budget."},
                                           status_code=413)(scope, receive, send)
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
                delivered = False

                async def replay() -> Message:
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, replay, tracked_send)
            else:
                await self.app(scope, receive, tracked_send)
        except QueueFullError as error:
            logger.warning("Assessment queue admission refused: %s", error)
            if started:
                raise
            await JSONResponse({"detail": str(error)}, status_code=429,
                               headers={"Retry-After": "30"})(scope, receive, send)
        except AssessmentLimitError as error:
            logger.warning("Assessment API capacity limit: %s", error)
            if started:
                raise
            await JSONResponse({"detail": str(error)}, status_code=413)(scope, receive, send)
        except (asyncpg.PostgresError, AzureError, ConnectionError, TimeoutError, DatabaseConfigurationError):
            logger.exception("Assessment persistence or identity service is unavailable")
            if started:
                raise
            await JSONResponse({"detail": "Assessment service is unavailable. Retry later; saved data is unchanged."},
                               status_code=503, headers={"Retry-After": "10"})(scope, receive, send)
        finally:
            if heavy_acquired:
                self.heavy_semaphore.release()
            dashboard_read.reset(context_token)
            semaphore.release()
