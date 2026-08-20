"""Continuous-mode spec validation: strict keys, defaults, mutual exclusivity."""

from __future__ import annotations

import pytest
import yaml

from pgbench_harness.cli import main
from pgbench_harness.errors import SpecError
from pgbench_harness.spec import parse_spec

from conftest import make_spec_doc


def cont_doc(**cont_over):
    doc = make_spec_doc()
    doc.pop("sweep")
    doc.pop("report", None)
    doc["continuous"] = {"threads": 16, **cont_over}
    return doc


def test_valid_continuous_defaults() -> None:
    spec = parse_spec(cont_doc())
    assert spec.is_continuous and spec.sweep is None and spec.soak is None
    c = spec.continuous
    assert c is not None
    assert c.threads == 16
    assert c.report_interval_s == 1
    assert c.restart_backoff_s == (1, 2, 5, 10, 30, 60)
    assert c.max_consecutive_failures == 0          # never give up
    assert c.segment_time_s == 21600


def test_continuous_overrides() -> None:
    c = parse_spec(cont_doc(restart_backoff_s=[2, 4, 8],
                            max_consecutive_failures=5,
                            segment_time_s=3600)).continuous
    assert c is not None
    assert c.restart_backoff_s == (2, 4, 8)
    assert c.max_consecutive_failures == 5
    assert c.segment_time_s == 3600


@pytest.mark.parametrize("other", ["sweep", "soak", "suite"])
def test_continuous_mutually_exclusive(other: str) -> None:
    doc = cont_doc()
    doc[other] = ({"threads": [1], "duration_s": 10} if other == "sweep"
                  else {"threads": 1, "duration_s": 10} if other == "soak"
                  else {"duration_s": 10})
    with pytest.raises(SpecError, match="mutually exclusive"):
        parse_spec(doc)


def test_continuous_unknown_key_fails() -> None:
    with pytest.raises(SpecError, match="unknown key.*continuous"):
        parse_spec(cont_doc(duration_s=60))       # continuous has no duration


@pytest.mark.parametrize("field,value,msg", [
    ("threads", 0, "threads"),
    ("report_interval_s", 5, "exactly 1"),
    ("max_consecutive_failures", -1, "max_consecutive_failures"),
    ("segment_time_s", 0, "segment_time_s"),
    ("segment_kill_grace_s", -1, "segment_kill_grace_s"),
])
def test_continuous_bad_values(field: str, value: int, msg: str) -> None:
    with pytest.raises(SpecError, match=msg):
        parse_spec(cont_doc(**{field: value}))


def test_continuous_backoff_must_be_nondecreasing() -> None:
    with pytest.raises(SpecError, match="non-decreasing"):
        parse_spec(cont_doc(restart_backoff_s=[10, 1]))
    with pytest.raises(SpecError, match="positive integers"):
        parse_spec(cont_doc(restart_backoff_s=[0, 1]))


def test_continuous_missing_threads() -> None:
    doc = cont_doc()
    del doc["continuous"]["threads"]
    with pytest.raises(SpecError, match="missing required key.*threads"):
        parse_spec(doc)


def test_validate_and_dry_run_cli(tmp_path, capsys) -> None:
    spec_path = tmp_path / "cont.yaml"
    spec_path.write_text(yaml.safe_dump(cont_doc()), encoding="utf-8")
    assert main(["validate", "--spec", str(spec_path)]) == 0
    out = capsys.readouterr().out
    assert "continuous" in out
    assert main(["continuous", "--spec", str(spec_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "--threads=16" in out and "until stopped" in out
    assert "backoff ladder" in out
