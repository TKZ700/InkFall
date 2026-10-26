"""B.3.7 history-copy elimination: allocation behavior + splice borders.

Proves the append path no longer rebinds history lists in steady
state (in-place tail splices) while keeping exact oracle equivalence,
including a corrupted-state fallback for the outline builder.
"""

import math
import random

from annotations.ink_geometry import (
    LiveCenterline,
    LiveOutline,
    build_outline,
    smooth_centerline,
)

BASE_W = 8.5039


def _oracle_outline(raw):
    sm, sw = smooth_centerline(raw, [BASE_W] * len(raw))
    return build_outline(list(sm), list(sw))


def _oracle_center(raw):
    sm, _ = smooth_centerline(raw, None)
    return sm


def _jitter(n, seed=7, amp=0.6):
    rng = random.Random(seed)
    return [(i * 0.9, (i % 9) * 2.0 + (rng.random() * 2 - 1) * amp)
            for i in range(n)]


def _reversal():
    fwd = [(i * 2.0, 0.0) for i in range(31)]
    return fwd + [(60.0 - i * 2.0, 0.0) for i in range(1, 31)]


def _cluster():
    line = [(i * 2.0, 0.0) for i in range(30)]
    blob = [(60.0 + (i % 5) * 0.1, (i % 3) * 0.1) for i in range(60)]
    tail = [(62.0 + i * 2.0, 0.0) for i in range(30)]
    return line + blob + tail


def test_history_lists_not_rebound_in_steady_state():
    # Tight clusters legitimately take the full-derivation path (which
    # rebinds _S without counting a fallback — pre-existing behavior),
    # so identity stability is asserted on clean paths only.
    for raw in (_jitter(400), _reversal()):
        lo = LiveOutline()
        for x, y in raw[:40]:
            lo.append(x, y, BASE_W)
        ids = (id(lo._S), id(lo._D), id(lo._L), id(lo._R), id(lo._T))
        for x, y in raw[40:]:
            lo.append(x, y, BASE_W)
        assert (id(lo._S), id(lo._D), id(lo._L), id(lo._R),
                id(lo._T)) == ids
        lc = LiveCenterline()
        for x, y in raw[:40]:
            lc.append(x, y)
        sid = id(lc._S)
        for x, y in raw[40:]:
            lc.append(x, y)
        assert id(lc._S) == sid


def test_exactness_at_every_prefix_including_borders():
    for raw in (_jitter(200), _reversal(), _cluster()):
        lo = LiveOutline()
        for i, (x, y) in enumerate(raw):
            lo.append(x, y, BASE_W)
            assert lo.outline() == _oracle_outline(raw[:i + 1]), i
    for raw in (_jitter(200), _reversal()):
        lc = LiveCenterline()
        for i, (x, y) in enumerate(raw):
            lc.append(x, y)
            assert lc.smoothed() == _oracle_center(raw[:i + 1]), i
    # Pre-existing baseline divergence (verified bit-identical against
    # HEAD): the widthless twin misses the oracle inside tight
    # clusters. Locked here so the refactor cannot drift silently.
    raw = _cluster()
    lc = LiveCenterline()
    first_miss = None
    for i, (x, y) in enumerate(raw):
        lc.append(x, y)
        if lc.smoothed() != _oracle_center(raw[:i + 1]) and \
                first_miss is None:
            first_miss = i
    assert first_miss == 45


def test_zero_fallbacks_on_clean_paths():
    for raw in (_jitter(300), _reversal()):
        lo, lc = LiveOutline(), LiveCenterline()
        for x, y in raw:
            lo.append(x, y, BASE_W)
            lc.append(x, y)
        assert lo.fallbacks == 0
        assert lc.fallbacks == 0


def test_outline_splice_miss_falls_back_exact():
    raw = _jitter(120)
    lo = LiveOutline()
    for x, y in raw[:100]:
        lo.append(x, y, BASE_W)
    lo._S = [(999.0, 999.0, 9.0)] * len(lo._S)  # corrupt splice state
    n = lo.fallbacks
    for i, (x, y) in enumerate(raw[100:], start=100):
        lo.append(x, y, BASE_W)
        assert lo.outline() == _oracle_outline(raw[:i + 1]), i
    assert lo.fallbacks > n
