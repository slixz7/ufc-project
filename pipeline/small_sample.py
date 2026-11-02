"""Small-sample statistics shared by the README generator and the website's Insights page. Standard library only."""
from __future__ import annotations

import math


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% range for a proportion k/n (the Wilson interval, which behaves for small samples and at 0% or 100%)."""
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - r) / d, (centre + r) / d


def whole_pct(x: float) -> str:
    return f"{round(100 * x)}%"
