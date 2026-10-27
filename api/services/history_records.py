"""Replay retained billing rows without keeping every raw record in memory."""

import json
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from itertools import islice
from pathlib import Path
from typing import overload
from uuid import uuid4

from anomalies.models import DailyCostRecord
from services.assessment_limits import AssessmentBudget


class ReplayableRecords(Sequence[DailyCostRecord]):
    @overload
    def __getitem__(self, index: int) -> DailyCostRecord: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[DailyCostRecord]: ...

    def __getitem__(self, index: int | slice) -> DailyCostRecord | Sequence[DailyCostRecord]:
        if isinstance(index, slice):
            return _RecordSlice(self, range(len(self))[index])
        position = index + len(self) if index < 0 else index
        if not 0 <= position < len(self):
            raise IndexError("History record index is out of range")
        return next(islice(iter(self), position, position + 1))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Sequence):
            return NotImplemented
        return len(self) == len(other) and all(left == right for left, right in zip(self, other))


class _RecordSlice(ReplayableRecords):
    def __init__(self, source: Sequence[DailyCostRecord], positions: range):
        self.source = source
        self.positions = positions

    def __len__(self) -> int:
        return len(self.positions)

    def __iter__(self) -> Iterator[DailyCostRecord]:
        if self.positions.step > 0:
            yield from islice(self.source, self.positions.start, self.positions.stop, self.positions.step)
        else:
            for position in self.positions:
                yield self.source[position]


class HistorySegment(ReplayableRecords):
    def __init__(self, budget: AssessmentBudget):
        self.budget = budget
        self.path = budget.spool_directory / f"history-{uuid4().hex}.jsonl"
        self.count = 0
        self.pending: list[str] = []
        self.pending_bytes = 0

    def append(self, record: DailyCostRecord) -> None:
        line = json.dumps(asdict(record), separators=(",", ":"), allow_nan=False) + "\n"
        size = len(line.encode("utf-8"))
        self.budget.consume("spool_bytes", size)
        if self.pending_bytes + size > 262144:
            self.flush()
        self.pending.append(line)
        self.pending_bytes += size
        self.count += 1
        if self.pending_bytes >= 262144:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.writelines(self.pending)
        self.pending.clear()
        self.pending_bytes = 0

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[DailyCostRecord]:
        self.flush()
        if not self.count:
            return
        with self.path.open(encoding="utf-8") as stream:
            for line in stream:
                yield DailyCostRecord(**json.loads(line))


class HistoryRecords(ReplayableRecords):
    def __init__(self, segments: Sequence[Sequence[DailyCostRecord]]):
        self.segments = tuple(segments)
        self.count = sum(len(segment) for segment in segments)

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[DailyCostRecord]:
        for segment in self.segments:
            yield from segment
