"""Per-assessment input budgets; worker inputs are spooled rather than readall()."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from services.assessment_settings import AssessmentSettings


class AssessmentLimitError(RuntimeError):
    pass


@dataclass
class AssessmentBudget:
    settings: AssessmentSettings
    spool_directory: Path
    used: dict[str, int] = field(default_factory=dict)

    def consume(self, category: str, count: int) -> None:
        total = self.used.get(category, 0) + count
        if total > getattr(self.settings, category):
            raise AssessmentLimitError(
                f"Assessment {category} budget exceeded. Narrow the subscription/history scope "
                "or have the operator increase the worker budget after capacity validation."
            )
        self.used[category] = total
        if category in {"csv_rows", "history_rows", "inventory_rows"} and total % 1000 < count:
            current = Path("/sys/fs/cgroup/memory.current")
            maximum = Path("/sys/fs/cgroup/memory.max")
            if current.exists() and maximum.exists():
                ceiling = maximum.read_text().strip()
                if ceiling != "max" and int(current.read_text()) > int(ceiling) * 0.85:
                    raise AssessmentLimitError(
                        "Assessment worker reached its memory safety budget. Narrow the scope "
                        "or increase the worker memory before retrying."
                    )


active_budget: ContextVar[AssessmentBudget | None] = ContextVar("assessment_budget", default=None)


def consume(category: str, count: int = 1) -> None:
    budget = active_budget.get()
    if budget is not None:
        budget.consume(category, count)
