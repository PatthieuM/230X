"""The in-loop budget: parse → plan under 200 µs p50 (asserted with a generous bound)."""
from algo1.bench import micro


def test_in_loop_budget():
    rows, total = micro(n=300)
    assert total < 1000, f"in-loop p50 sum {total:.0f} µs (budget 200, bound 1000)"
