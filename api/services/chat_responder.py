"""Deterministic, snapshot-grounded answers for the FinOps chat experience."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reports.models import FullReport
from services.chat_model import ChatTurn, narrate_with_model_router


class ChatMetric(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    label: str
    value: str
    detail: str


class ChatResource(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    resource_name: str = Field(alias="resourceName")
    resource_id: str = Field(alias="resourceId")
    subscription_name: str = Field(alias="subscriptionName")
    monthly_cost: float | None = Field(alias="monthlyCost")
    currency: str
    detail: str


class ChatUsage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    input_tokens: int = Field(default=0, alias="inputTokens")
    output_tokens: int = Field(default=0, alias="outputTokens")
    total_tokens: int = Field(default=0, alias="totalTokens")


class ChatAnswer(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    intent: Literal["overview", "subscription_change", "unattached_disks", "score", "trend", "forecast", "help"]
    answer: str
    metrics: list[ChatMetric] = Field(default_factory=list)
    resources: list[ChatResource] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    data_as_of: str = Field(alias="dataAsOf")
    disclaimer: str
    response_mode: Literal["model_router", "deterministic_fallback"] = Field(
        default="deterministic_fallback",
        alias="responseMode",
    )
    selected_model: str | None = Field(default=None, alias="selectedModel")
    usage: ChatUsage | None = None
    evidence_keys: list[str] = Field(default_factory=list, alias="evidenceKeys")


_SUGGESTIONS = [
    "Why is subscription X spending more this month?",
    "Show all unattached disks.",
    "Show the 3, 6, and 12 month trends.",
    "What is the expected next-month spend and end-of-year projection?",
]


def _money(value: float, currency: str) -> str:
    return f"{currency} {value:,.2f}"


def _trend_metric(months, window: int, currency: str) -> ChatMetric:
    selected = months[-window:]
    if len(selected) < 2:
        return ChatMetric(label=f"{window} month trend", value="Unavailable", detail=f"Only {len(selected)} month(s) available.")
    first, last = selected[0].total, selected[-1].total
    change = ((last - first) / first * 100) if first else None
    average = sum(point.total for point in selected) / len(selected)
    direction = "up" if last > first else "down" if last < first else "flat"
    value = f"{change:+.1f}%" if change is not None else direction.title()
    return ChatMetric(
        label=f"{window} month trend",
        value=value,
        detail=f"{direction.title()} from {_money(first, currency)} to {_money(last, currency)}; average {_money(average, currency)}.",
    )


def _forecast_metrics(months, currency: str) -> list[ChatMetric]:
    if len(months) < 3:
        return [ChatMetric(label="Forecast", value="Unavailable", detail="At least 3 complete months are required.")]
    totals = [point.total for point in months]
    count = len(totals)
    midpoint = (count - 1) / 2
    denominator = sum((index - midpoint) ** 2 for index in range(count))
    slope = sum((index - midpoint) * (value - (sum(totals) / count)) for index, value in enumerate(totals)) / denominator
    intercept = (sum(totals) / count) - slope * midpoint
    next_month = max(0.0, intercept + slope * count)
    last_year, last_month = (int(value) for value in months[-1].month.split("-"))
    actual_ytd = sum(point.total for point in months if int(point.month[:4]) == last_year)
    remaining = max(0, 12 - last_month)
    projected_remaining = sum(max(0.0, intercept + slope * (count + offset)) for offset in range(remaining))
    return [
        ChatMetric(label="Expected next month", value=_money(next_month, currency), detail=f"Linear trend across {count} complete months."),
        ChatMetric(
            label=f"{last_year} projection",
            value=_money(actual_ytd + projected_remaining, currency),
            detail=f"Actual through {months[-1].month}, plus {remaining} forecast month(s).",
        ),
    ]


def _score_metric(report: FullReport) -> ChatMetric:
    score = report.advisor_score.cost_score
    if score is None:
        score = report.advisor_score.score
    if score is None:
        return ChatMetric(label="FinOps Score", value="Unavailable", detail=report.advisor_score.status)
    change = report.advisor_score.monthly_change
    detail = "Advisor-backed score"
    if change is not None:
        detail += f"; {change:+.1f} points this month"
    return ChatMetric(label="FinOps Score", value=f"{score:.0f} / 100", detail=detail)


def _subscription_answer(question: str, report: FullReport, currency: str) -> ChatAnswer | None:
    normalized = question.casefold()
    candidates = [
        row for row in report.subscription_breakdown
        if row.subscription_id.casefold() in normalized or row.subscription_name.casefold() in normalized
    ]
    if not candidates and len(report.subscription_breakdown) == 1:
        candidates = report.subscription_breakdown
    if not candidates:
        return None
    row = candidates[0]
    months = [point for point in report.spend_history.months if row.subscription_id in point.subscription_spend]
    if len(months) < 2:
        answer = f"There is not enough subscription-level history to compare {row.subscription_name}."
        return ChatAnswer(intent="subscription_change", answer=answer, dataAsOf=report.report_metadata.period, disclaimer=report.spend_history.status_message)
    previous, current = months[-2], months[-1]
    previous_total = previous.subscription_spend[row.subscription_id]
    current_total = current.subscription_spend[row.subscription_id]
    delta = current_total - previous_total
    pct = (delta / previous_total * 100) if previous_total else None
    current_categories = current.subscription_category_spend.get(row.subscription_id, {})
    previous_categories = previous.subscription_category_spend.get(row.subscription_id, {})
    changes = sorted(
        ((category, value - previous_categories.get(category, 0.0)) for category, value in current_categories.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    increases = [(category, value) for category, value in changes if value > 0][:3]
    reason = ", ".join(f"{category} ({_money(value, currency)})" for category, value in increases)
    direction = "increased" if delta > 0 else "decreased" if delta < 0 else "was unchanged"
    percent_text = f" ({pct:+.1f}%)" if pct is not None else ""
    answer = (
        f"{row.subscription_name} {direction} by {_money(abs(delta), currency)}{percent_text} from {previous.month} to {current.month}."
        + (f" The largest increases were {reason}." if reason else " No spend category increased.")
    )
    return ChatAnswer(
        intent="subscription_change",
        answer=answer,
        metrics=[
            ChatMetric(label="Previous month", value=_money(previous_total, currency), detail=previous.month),
            ChatMetric(label="Current month", value=_money(current_total, currency), detail=current.month),
        ],
        suggestions=_SUGGESTIONS,
        dataAsOf=current.month,
        disclaimer="Calculated from completed FOCUS export months; amounts are not model-generated.",
    )


def answer_question(question: str, report: FullReport) -> ChatAnswer:
    normalized = " ".join(question.casefold().split())
    currency = report.report_metadata.currency
    months = report.spend_history.months
    data_as_of = report.report_metadata.period
    deterministic = "Calculated from the completed report snapshot; no Foundry model call was used."

    if "unattached" in normalized and "disk" in normalized:
        category = next((item for item in report.tier_a_categories if item.category == "unattached_disks"), None)
        lines = category.lines if category else []
        resources = [
            ChatResource(
                resourceName=line.resource_name,
                resourceId=line.resource_id,
                subscriptionName=line.subscription_name or line.subscription_id,
                monthlyCost=line.monthly_cost,
                currency=currency,
                detail=line.detail,
            )
            for line in lines
        ]
        total = sum(line.monthly_cost or 0 for line in lines)
        return ChatAnswer(
            intent="unattached_disks",
            answer=f"Found {len(lines)} unattached managed disk(s) with {_money(total, currency)} in matched monthly cost.",
            metrics=[ChatMetric(label="Unattached disks", value=str(len(lines)), detail=f"Matched monthly cost {_money(total, currency)}")],
            resources=resources,
            suggestions=_SUGGESTIONS,
            dataAsOf=data_as_of,
            disclaimer=deterministic,
        )

    if "why" in normalized and "subscription" in normalized:
        subscription = _subscription_answer(question, report, currency)
        if subscription:
            return subscription
        return ChatAnswer(
            intent="subscription_change",
            answer="Name or paste one of the subscriptions in the current report scope so I can compare its last two complete months.",
            suggestions=[row.subscription_name for row in report.subscription_breakdown] + _SUGGESTIONS,
            dataAsOf=data_as_of,
            disclaimer=deterministic,
        )

    wants_score = "score" in normalized
    wants_trend = "trend" in normalized or any(value in normalized for value in ("3 month", "6 month", "12 month"))
    wants_forecast = "forecast" in normalized or "projection" in normalized or "next month" in normalized
    overview = "overview" in normalized or sum((wants_score, wants_trend, wants_forecast)) > 1
    if overview:
        return ChatAnswer(
            intent="overview",
            answer="Here is the latest FinOps score, cost trend, and deterministic forecast from completed FOCUS history.",
            metrics=[_score_metric(report)] + [_trend_metric(months, value, currency) for value in (3, 6, 12)] + _forecast_metrics(months, currency),
            suggestions=_SUGGESTIONS,
            dataAsOf=data_as_of,
            disclaimer=deterministic,
        )
    if wants_score:
        return ChatAnswer(intent="score", answer="The FinOps Score uses the available Azure Advisor cost score.", metrics=[_score_metric(report)], suggestions=_SUGGESTIONS, dataAsOf=data_as_of, disclaimer=deterministic)
    if wants_trend:
        return ChatAnswer(intent="trend", answer="Completed FOCUS monthly spend trends:", metrics=[_trend_metric(months, value, currency) for value in (3, 6, 12)], suggestions=_SUGGESTIONS, dataAsOf=data_as_of, disclaimer=deterministic)
    if wants_forecast:
        return ChatAnswer(intent="forecast", answer="This baseline extrapolates the linear trend in completed monthly consumption history.", metrics=_forecast_metrics(months, currency), suggestions=_SUGGESTIONS, dataAsOf=data_as_of, disclaimer="Forecasts are planning estimates, not billing commitments. " + deterministic)
    return ChatAnswer(
        intent="help",
        answer="I can explain subscription changes, list unattached disks, show the FinOps Score and trends, or forecast spend from the current report.",
        suggestions=_SUGGESTIONS,
        dataAsOf=data_as_of,
        disclaimer=deterministic,
    )


async def respond_to_question(
    question: str,
    report: FullReport,
    history: list[ChatTurn] | None = None,
) -> ChatAnswer:
    verified = answer_question(question, report)
    return await narrate_with_model_router(question, report, verified, history or [])