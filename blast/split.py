"""Worker split: pick k in 1..8 minimising busiest-load + 2*k (all in ms, like the scorer)."""
from __future__ import annotations

import heapq
from typing import Dict, Iterable, List

WORKER_COST_MS = 2000
MAX_WORKERS = 8


def split(numbers: Iterable[int], ms: Dict[int, int]) -> List[List[int]]:
    nums = sorted(set(numbers), key=lambda n: -ms[n])
    if not nums:
        return []
    best = None
    for k in range(1, min(MAX_WORKERS, len(nums)) + 1):
        heap = [(0, w) for w in range(k)]
        heapq.heapify(heap)
        buckets: List[List[int]] = [[] for _ in range(k)]
        for n in nums:
            load, w = heapq.heappop(heap)
            buckets[w].append(n)
            heapq.heappush(heap, (load + ms[n], w))
        cost = max(l for l, _ in heap) + WORKER_COST_MS * k
        if best is None or cost < best[0]:
            best = (cost, buckets)
    return [b for b in best[1] if b]
