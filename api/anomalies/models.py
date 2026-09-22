"""Typed contracts for deterministic cost anomaly detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class DailyCostRecord:
    date: str
    subscription_id: str
    subscription_name: str
    service_name: str
    resource_group: str
    resource_id: str
    resource_name: str
    effective_cost: float
    service_category: str = ""
    resource_type: str = ""
    region: str = ""
    charge_category: str = ""
    list_cost: float = 0.0
    contracted_cost: float = 0.0
    commitment_discount_type: str = ""
    commitment_discount_status: str = ""
    pricing_category: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    tag_attribution_source: Literal["exported_resource_tags", "exported_resource_tags_with_current_group_fallback"] = "exported_resource_tags"


class AnomalyContributor(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    resource_id: str = Field(default="", alias="resourceId")
    cost: float


class CostAnomaly(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    anomaly_id: str = Field(alias="anomalyId")
    date: str
    first_detected_date: str = Field(alias="firstDetectedDate")
    last_detected_date: str = Field(alias="lastDetectedDate")
    duration_days: int = Field(alias="durationDays")
    anomaly_type: Literal["spike", "drop", "new_resource"] = Field(alias="anomalyType")
    dimension_type: Literal["subscription", "service", "resource_group", "resource"] = Field(
        alias="dimensionType"
    )
    dimension_name: str = Field(alias="dimensionName")
    dimension_id: str = Field(alias="dimensionId")
    subscription_id: str = Field(default="", alias="subscriptionId")
    subscription_name: str = Field(default="", alias="subscriptionName")
    actual_cost: float = Field(alias="actualCost")
    expected_cost: float = Field(alias="expectedCost")
    expected_lower: float = Field(alias="expectedLower")
    expected_upper: float = Field(alias="expectedUpper")
    absolute_delta: float = Field(alias="absoluteDelta")
    percentage_delta: float | None = Field(alias="percentageDelta")
    severity: Literal["Low", "Medium", "High"]
    baseline_samples: int = Field(alias="baselineSamples")
    contributors: list[AnomalyContributor]
    investigation_url: str = Field(alias="investigationUrl")


class AnomalyTrendPoint(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    date: str
    actual_cost: float = Field(alias="actualCost")
    expected_cost: float | None = Field(alias="expectedCost")
    expected_lower: float | None = Field(alias="expectedLower")
    expected_upper: float | None = Field(alias="expectedUpper")


class AnomalySummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    algorithm_version: str = Field(alias="algorithmVersion")
    label: str
    status: Literal["ready", "insufficient_history"]
    status_message: str = Field(alias="statusMessage")
    history_start: str = Field(alias="historyStart")
    history_end: str = Field(alias="historyEnd")
    complete_days: int = Field(alias="completeDays")
    required_days: int = Field(alias="requiredDays")
    currency: str
    generated_at: str = Field(alias="generatedAt")
    trend: list[AnomalyTrendPoint]
    anomalies: list[CostAnomaly]
    ai_anomalies: list[CostAnomaly] | None = Field(default=None, alias="aiAnomalies")
