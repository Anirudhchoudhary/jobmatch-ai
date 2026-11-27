import pytest

from evals.thresholds import check, load_thresholds


def test_check_reports_missing_metric():
    assert check({}, {"judge:faithfulness": ("min", 0.8)}) == ["missing metric judge:faithfulness"]


def test_check_min_and_max():
    msgs = check(
        {"skill_f1": 0.41, "fp:any": 0.2},
        {"skill_f1": ("min", 0.5), "fp:any": ("max", 0.1)},
    )
    assert msgs == ["skill_f1 0.41 < min 0.50", "fp:any 0.20 > max 0.10"]


def test_check_passes_at_boundary():
    assert check({"a": 0.5, "b": 0.1}, {"a": ("min", 0.5), "b": ("max", 0.1)}) == []


def test_load_thresholds_profile(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text(
        "live:\n  skill_f1: {min: 0.7}\n  pii_leaks: {max: 0}\noffline:\n  pii_leaks: {max: 0}\n"
    )
    assert load_thresholds(path, "live") == {"skill_f1": ("min", 0.7), "pii_leaks": ("max", 0.0)}
    with pytest.raises(KeyError):
        load_thresholds(path, "nope")


def test_load_thresholds_rejects_bad_bound(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text("live:\n  x: {avg: 1}\n")
    with pytest.raises(ValueError):
        load_thresholds(path, "live")


def test_repo_thresholds_file_loads():
    from evals.dataset import DATASET_DIR

    path = DATASET_DIR.parent / "thresholds.yaml"
    assert load_thresholds(path, "offline")["pii_leaks"] == ("max", 0.0)
    assert load_thresholds(path, "live")["skill_f1"] == ("min", 0.7)
