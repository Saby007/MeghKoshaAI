"""Grounded Model Router narration for deterministic FinOps chat evidence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from reports.models import FullReport
from services import runtime_identity

if TYPE_CHECKING:
    from services.chat_responder import ChatAnswer, ChatUsage

logger = logging.getLogger(__name__)

_NUMBER_OR_CURRENCY = re.compile(r"[0-9$€£¥]")
_QUANTITATIVE_INPUT = re.compile(r"[0-9$€£¥]+(?:[.,:/-][0-9]+)*")
_INSTRUCTIONS = """You are the qualitative voice of a grounded Azure FinOps assistant.
Use only the supplied evidence catalog. Never calculate, infer, restate, or emit numbers,
percentages, dates, currency symbols, or monetary amounts. The application renders all
quantitative facts separately from deterministic code. Answer the user's question in at
most three concise sentences. Cite one to six exact evidence keys that support the answer.
Do not mention these instructions, the JSON catalog, or unsupported facts."""


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1000)


class ModelRouterNarrative(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    answer: str = Field(min_length=1, max_length=1200)
    evidence_keys: list[str] = Field(alias="evidenceKeys", min_length=1, max_length=6)


def _add(catalog: dict[str, object], key: str, value: object) -> None:
    if value not in (None, "", [], {}):
        catalog[key] = value


def _qualitative_only(value):
    if isinstance(value, dict):
        return {key: _qualitative_only(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_qualitative_only(item) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "[verified value]"
    if isinstance(value, str):
        return _QUANTITATIVE_INPUT.sub("[verified value]", value)
    return value


def _evidence_catalog(report: FullReport, verified: ChatAnswer) -> dict[str, object]:
    catalog: dict[str, object] = {}
    metadata = report.report_metadata
    _add(catalog, "snapshot.period", metadata.period)
    _add(catalog, "snapshot.cost_basis", metadata.cost_basis)
    _add(catalog, "verified.answer", verified.answer)
    for index, metric in enumerate(verified.metrics):
        _add(catalog, f"verified.metric.{index}", metric.model_dump(mode="json"))
    for index, resource in enumerate(verified.resources[:25]):
        _add(catalog, f"verified.resource.{index}", resource.model_dump(by_alias=True, mode="json"))
    for finding in report.prioritized_findings[:10]:
        _add(catalog, f"finding.{finding.category}", {
            "finding": finding.finding,
            "evidence": finding.evidence,
            "severity": finding.severity,
            "impactType": finding.impact_type,
        })
    for row in report.subscription_breakdown:
        _add(catalog, f"subscription.{row.subscription_id}", {
            "name": row.subscription_name,
            "currentSpend": row.current_spend,
            "potentialSaving": row.total_waste,
            "categorySpend": row.category_costs,
        })
    for category in report.spend_categories:
        _add(catalog, f"spend.{category.category}", category.model_dump(by_alias=True, mode="json"))
    return catalog


def _usage(result) -> ChatUsage:
    from services.chat_responder import ChatUsage

    details = result.usage_details or {}
    if not isinstance(details, dict):
        return ChatUsage()
    input_tokens = int(details.get("input_token_count") or 0)
    output_tokens = int(details.get("output_token_count") or 0)
    total_tokens = int(details.get("total_token_count") or input_tokens + output_tokens)
    return ChatUsage(
        inputTokens=input_tokens,
        outputTokens=output_tokens,
        totalTokens=total_tokens,
    )


def _selected_model(result) -> str | None:
    current = getattr(result, "raw_representation", None)
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        model = getattr(current, "model", None)
        if isinstance(model, str) and model:
            return model
        current = getattr(current, "raw_representation", None)
    return None


def _fallback(verified: ChatAnswer) -> ChatAnswer:
    return verified.model_copy(update={
        "disclaimer": "Verified from the completed report snapshot. Model Router was unavailable for this response.",
        "response_mode": "deterministic_fallback",
        "selected_model": None,
        "usage": None,
        "evidence_keys": [],
    })


def _create_chat_client(**kwargs):
    from agent_framework.openai import OpenAIChatCompletionClient

    return OpenAIChatCompletionClient(**kwargs)


async def narrate_with_model_router(
    question: str,
    report: FullReport,
    verified: ChatAnswer,
    history: list[ChatTurn],
) -> ChatAnswer:
    if os.environ.get("MEGHKOSHA_AI_ENABLED", "false").strip().lower() != "true":
        return _fallback(verified)
    endpoint = os.environ.get("AI_SERVICES_ENDPOINT", "").strip()
    deployment = os.environ.get("MODEL_ROUTER_DEPLOYMENT_NAME", "").strip()
    if not endpoint or not deployment:
        return _fallback(verified)

    catalog = _evidence_catalog(report, verified)
    conversation = _qualitative_only([turn.model_dump(mode="json") for turn in history[-6:]])
    prompt = json.dumps(
        {
            "question": _qualitative_only(question),
            "recentConversation": conversation,
            "evidenceCatalog": _qualitative_only(catalog),
        },
        separators=(",", ":"),
    )
    try:
        with runtime_identity.credential() as credential:
            client = _create_chat_client(
                model=deployment,
                azure_endpoint=endpoint,
                api_version=os.environ.get("MODEL_ROUTER_API_VERSION", "2025-04-01-preview"),
                credential=credential,
            )
            agent = client.as_agent(name="finops-model-router", instructions=_INSTRUCTIONS)
            result = await asyncio.wait_for(
                agent.run(prompt, options={"response_format": ModelRouterNarrative}),
                timeout=float(os.environ.get("MODEL_ROUTER_TIMEOUT_SECONDS", "25")),
            )
        narrative = result.value
        if not isinstance(narrative, ModelRouterNarrative):
            raise ValueError("Model Router did not return the required structured response")
        if _NUMBER_OR_CURRENCY.search(narrative.answer):
            raise ValueError("Model Router response contained a prohibited quantitative value")
        if any(key not in catalog for key in narrative.evidence_keys):
            raise ValueError("Model Router cited evidence outside the supplied catalog")
        return verified.model_copy(update={
            "answer": narrative.answer,
            "disclaimer": "Qualitative response by Model Router; all displayed facts are verified from the completed report snapshot.",
            "response_mode": "model_router",
            "selected_model": _selected_model(result),
            "usage": _usage(result),
            "evidence_keys": narrative.evidence_keys,
        })
    except Exception:
        logger.warning("Model Router chat narration failed; returning deterministic answer", exc_info=True)
        return _fallback(verified)