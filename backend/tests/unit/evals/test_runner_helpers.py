from evals.runner import count_leaks


def test_count_leaks() -> None:
    lines = ["x jane.doe@example.com y", "z"]
    assert count_leaks(lines, ["jane.doe@example.com", "555-0142"]) == 1


def test_count_leaks_counts_every_occurrence() -> None:
    assert count_leaks(["555-0142 and 555-0142"], ["555-0142"]) == 2
