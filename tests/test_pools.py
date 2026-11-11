"""Lane admission contracts, independent of run grouping and native providers."""

import fcntl
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hypothesis import example, given, settings, strategies as st

import test_xcheck as support  # repository import path and isolated fixture setup
from xcheck import files, pools
from xcheck.types import Lane


class Permits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        limits = patch.object(pools, "LIMITS", {"grok": 2}, create=True)
        limits.start()
        self.addCleanup(limits.stop)

    def take(self, lease, pool="grok"):
        held = pools.acquire(self.root, pool, lease, time.monotonic() + 0.03)
        if held:
            self.addCleanup(held.release)
        return held

    def test_two_runs_share_a_pool_but_a_third_lane_waits(self):
        first, second = self.take("run-a/grok"), self.take("run-b/grok")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(self.take("run-c/grok"))
        first.release()
        self.assertIsNotNone(self.take("run-c/grok"))

    def test_same_run_does_not_bypass_the_limit(self):
        self.assertIsNotNone(self.take("run-a/first"))
        self.assertIsNotNone(self.take("run-a/second"))
        self.assertIsNone(self.take("run-a/third"))

    def test_join_retains_exactly_one_permit_after_runner_death(self):
        first = self.take("run-a/grok")
        child = pools.join(self.root, "grok", "run-a/grok")
        self.assertIsNotNone(child)
        self.addCleanup(child.release)
        first.release()
        self.assertIsNotNone(self.take("run-b/grok"))
        self.assertIsNone(self.take("run-c/grok"))
        child.release()
        self.assertIsNotNone(self.take("run-c/grok"))
        self.assertIsNone(pools.join(self.root, "grok", "run-a/grok"))

    def test_waiter_does_not_retain_aggregate_custody(self):
        first, second = self.take("a"), self.take("b")
        self.assertIsNone(self.take("c"))
        first.release()
        second.release()
        with (self.root / ".pools/grok/busy").open("a+") as busy:
            fcntl.flock(busy, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_account_maintenance_excludes_new_admission(self):
        d = self.root / ".pools/grok"
        d.mkdir(parents=True)
        with (d / "gate").open("a+") as gate, (d / "busy").open("a+") as busy:
            fcntl.flock(gate, fcntl.LOCK_EX)
            fcntl.flock(busy, fcntl.LOCK_EX)
            self.assertIsNone(self.take("a"))
        self.assertIsNotNone(self.take("a"))

    def test_maintenance_waits_for_the_last_spender_not_the_first_completion(self):
        a, b = self.take("a"), self.take("b")
        a.release()
        with (self.root / ".pools/grok/busy").open("a+") as busy:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(busy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            b.release()
            fcntl.flock(busy, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_lower_limit_counts_holders_of_previously_available_slots(self):
        a, b = self.take("a"), self.take("b")
        pools.LIMITS["grok"] = 1
        a.release()  # slot zero is free, but a live higher slot still counts
        self.assertIsNone(self.take("c"))
        b.release()
        self.assertIsNotNone(self.take("c"))

    def test_a_duplicate_claim_cannot_create_two_join_targets(self):
        self.assertIsNotNone(self.take("a"))
        with self.assertRaisesRegex(ValueError, "already owns"):
            self.take("a")
        self.assertIsNotNone(self.take("b"))

    def test_disjoint_pools_do_not_share_their_capacity(self):
        self.assertIsNotNone(self.take("a"))
        self.assertIsNotNone(self.take("b"))
        self.assertIsNotNone(self.take("c", pool="claude"))

    def test_recorded_workspace_survives_roundtrip_and_old_records_keep_their_layout(self):
        import json
        base = self.root / "run"
        base.mkdir()
        lane = Lane("grok", "grok-4.7", "xhigh")
        with patch.object(files, "ROOT", self.root):
            run = files.Run(base, {lane.name: lane}, support.CONTRACT, False, 0, None,
                            "headless", "other", "now", {}, {})
            run.save()
            self.assertEqual(files.Run.load(base).workspace(lane.name), run.workspace(lane.name))
            record = json.loads((base / "run.json").read_text())
            del record["workspaces"]
            (base / "run.json").write_text(json.dumps(record))
            self.assertEqual(files.Run.load(base).workspace(lane.name),
                             self.root / ".lanes" / lane.pool / lane.name)

    def test_old_runtime_must_drain_before_unrepresented_spend_is_admitted(self):
        d = self.root / ".pools/grok"
        d.mkdir(parents=True)
        (d / "owner").write_text("old-run")
        with (d / "busy").open("a+") as busy:
            fcntl.flock(busy, fcntl.LOCK_SH)
            self.assertIsNone(self.take("new-run/grok"))
        self.assertIsNotNone(self.take("new-run/grok"))

    @settings(max_examples=30, deadline=None)
    @given(limit=st.integers(1, 4),
           operations=st.lists(st.tuples(st.sampled_from(["take", "release", "join"]), st.integers(0, 5)),
                               min_size=0, max_size=30))
    @example(limit=4, operations=[("take", i) for i in range(6)])
    @example(limit=1, operations=[("take", 0), ("join", 0), ("release", 0), ("take", 1),
                                 ("release", 0), ("take", 1), ("join", 0)])
    def test_permit_count_follows_live_leases_not_handle_count(self, limit, operations):
        """Oracle: set of live identities with handle reference counts and capacity.
        Trigger: full pools, child-only custody, stale joins and slot reuse.
        A run label or another handle for one lease cannot create extra capacity.
        """
        with tempfile.TemporaryDirectory() as root, patch.dict(pools.LIMITS, {"grok": limit}):
            handles = {i: [] for i in range(6)}
            try:
                for operation, identity in operations:
                    live = sum(bool(group) for group in handles.values())
                    if operation == "release":
                        if handles[identity]:
                            handles[identity].pop().release()
                    elif operation == "join":
                        child = pools.join(Path(root), "grok", str(identity))
                        self.assertEqual(child is not None, bool(handles[identity]))
                        if child:
                            handles[identity].append(child)
                    elif not handles[identity]:
                        held = pools.acquire(Path(root), "grok", str(identity), time.monotonic() + 0.03)
                        self.assertEqual(held is not None, live < limit)
                        if held:
                            handles[identity].append(held)
            finally:
                for group in handles.values():
                    for held in group:
                        held.release()

    @settings(max_examples=25, deadline=None)
    @given(st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\0"),
                   min_size=1, max_size=100))
    @example("run\r\nwith\rreturns\nand 'quotes'\\雪")
    def test_join_preserves_lease_identity_exactly(self, identity):
        """Oracle: a child may join its live identity, never a different identity.
        Trigger: CR/CRLF normalization, whitespace, quotes, Unicode and escapes.
        """
        with tempfile.TemporaryDirectory() as root:
            held = pools.acquire(Path(root), "grok", identity, time.monotonic() + 1)
            self.assertIsNotNone(held)
            try:
                child = pools.join(Path(root), "grok", identity)
                self.assertIsNotNone(child)
                child.release()
                self.assertIsNone(pools.join(Path(root), "grok", identity + "-other"))
            finally:
                held.release()

    def test_workspaces_are_run_scoped_without_embedding_arbitrary_base_names(self):
        lane = Lane("grok", "grok-4.7", "xhigh")
        with patch.object(files, "ROOT", self.root):
            a = files.workspace(lane, self.root / "project a's review")
            b = files.workspace(lane, self.root / "project b")
            self.assertNotEqual(a, b)
            self.assertEqual(a, files.workspace(lane, self.root / "project a's review" / "."))
            self.assertTrue(a.is_relative_to(self.root / ".lanes"))
            self.assertNotIn("'", str(a))
