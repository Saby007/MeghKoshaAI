from datetime import date

import pytest

from services.assessment_queries import CostQuery, date_windows, normalized_tags, row_predicate


@pytest.mark.parametrize(("window", "previous"), [
    ({"startDate": "2026-08-01", "endDate": "2026-08-31"}, ("2026-07-01", "2026-07-31")),
    ({"startDate": "2026-03-01", "endDate": "2026-03-31"}, ("2026-02-01", "2026-02-28")),
    ({"startDate": "2024-03-01", "endDate": "2024-03-31"}, ("2024-02-01", "2024-02-29")),
    ({"startDate": "2026-10-01", "endDate": "2026-10-04", "monthToDate": True}, ("2026-09-01", "2026-09-04")),
    ({"startDate": "2026-10-01", "endDate": "2026-10-07"}, ("2026-09-24", "2026-09-30")),
])
def test_cost_windows_preserve_original_calendar_and_mtd_comparisons(window, previous):
    query = CostQuery(kind="groups", window=window)
    current, compared = date_windows(query)
    assert (str(current[0]), str(current[-1])) == (window["startDate"], window["endDate"])
    assert (str(compared[0]), str(compared[-1])) == previous


def test_explicit_comparison_and_date_validation():
    query = CostQuery(kind="groups", window={"startDate": "2026-08-04", "endDate": "2026-08-04"},
                      previous={"startDate": "2026-07-04", "endDate": "2026-07-04"})
    assert date_windows(query) == ([date(2026, 8, 4)], [date(2026, 7, 4)])
    with pytest.raises(ValueError, match="366"):
        date_windows(CostQuery(kind="groups", window={"startDate": "2024-01-01", "endDate": "2026-01-01"}))
    with pytest.raises(ValueError, match="ordered"):
        date_windows(CostQuery(kind="groups", window={"startDate": "2026-10-02", "endDate": "2026-10-01"}))


def test_tag_normalization_preserves_first_matching_original_key():
    assert normalized_tags({"  Cost_Center ": "first", "cost-center": "second", "Owner": "team"}) == {
        "cost center": "first", "owner": "team",
    }


def test_cost_filters_are_parameterized_and_always_snapshot_scoped():
    arguments = ["tenant", "snapshot"]
    predicate = row_predicate([{"subscriptionId": "ABC", "tagKey": "Cost_Center", "tagValue": "x' OR true --",
                               "resourceGroup": "RG-ONE"}], arguments)
    assert "r.tenant_id=$1" in predicate and "r.snapshot_id=$2" in predicate
    assert "x' OR true --" not in predicate
    assert "ABC" not in predicate and "RG-ONE" not in predicate
    assert "cost center" in arguments and "x' OR true --" in arguments and "rg-one" in arguments


def test_unknown_filters_and_malformed_required_tags_are_not_silently_ignored():
    with pytest.raises(ValueError, match="Unsupported"):
        row_predicate([{"unknownFilter": "x"}], ["tenant", "snapshot"])
    with pytest.raises(ValueError, match="requiredTagKeys"):
        row_predicate([{"requiredTagKeys": '{"not":"an array"}'}], ["tenant", "snapshot"])
