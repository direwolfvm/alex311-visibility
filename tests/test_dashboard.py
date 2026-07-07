import pytest
from fastapi import HTTPException

from dashboard.app import _parse_polygon


def test_parse_polygon_builds_pg_literal():
    lit = _parse_polygon("-77.05,38.80;-77.04,38.81;-77.06,38.82")
    assert lit == "((-77.05,38.8),(-77.04,38.81),(-77.06,38.82))"


@pytest.mark.parametrize("bad", [
    "not-a-polygon",
    "-77.05,38.80;-77.04,38.81",           # only 2 vertices
    "-77.05,38.80;-77.04,38.81;190,38.82",  # lng out of range
    "-77.05;38.80;-77.04;38.81;-77.06;38.82",
    ";".join(f"-77.0{i % 10},38.8" for i in range(201)),  # too many vertices
])
def test_parse_polygon_rejects_garbage(bad):
    with pytest.raises(HTTPException):
        _parse_polygon(bad)
