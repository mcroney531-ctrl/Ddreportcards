"""
The combined GM Assistant prompt, checked as one prompt.

Behavioural instructions reach the model from five sources: backend rules,
the app's system text, the app's context block, follow-up guidance and the
tool descriptions. tests/fixtures/prompt_snapshots/ holds, for the baseline
and Phase A variants, the exact assembled prompt in four situations plus an
inventory of every directive by source and topic (evals/build_snapshots.py,
using the real app captured against synthetic Sleeper data).

These tests:
  * rebuild the snapshots from the committed app capture and current backend
    code, so any backend instruction change forces a reviewed snapshot diff;
  * rebuild the inventory the same way;
  * enforce source ownership per topic (the app sets style, not lookup or
    conviction policy) with a short list of reviewed exceptions;
  * check that no source reintroduces an instruction Phase A replaced.
CI re-captures the app text from gm-command and diffs it against the
committed capture, which catches app-side drift.
"""
import json
import pathlib
import unittest

from evals import build_snapshots as bs
from evals.instruction_inventory import conflicts_present, directives, ownership_violations

SNAP = pathlib.Path(__file__).parent / "fixtures" / "prompt_snapshots"


def captured(variant):
    return json.loads((SNAP / variant / "frontend_capture.json").read_text())


class SnapshotsAreCurrentTest(unittest.TestCase):
    def test_snapshots_and_inventory_match_the_code(self):
        for variant, module in bs.VARIANTS.items():
            built = bs.build(module, captured(variant))
            inv = json.loads((SNAP / variant / "inventory.json").read_text())
            for situation, b in built.items():
                with self.subTest(variant=variant, situation=situation):
                    self.assertEqual(b["system"], (SNAP / variant / f"{situation}.txt").read_text(),
                                     "Snapshot is stale: run python -m evals.build_snapshots and review the diff.")
                    self.assertEqual(directives(b["segments"]), inv[situation])

    def test_app_capture_had_no_page_errors(self):
        for variant in bs.VARIANTS:
            with self.subTest(variant):
                self.assertEqual(captured(variant)["page_errors"], [])


class CandidateConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.built = bs.build(bs.VARIANTS["candidate"], captured("candidate"))

    def test_each_topic_is_directed_only_by_its_owners(self):
        for situation, b in self.built.items():
            with self.subTest(situation):
                self.assertEqual(ownership_violations(directives(b["segments"])), [])

    def test_no_replaced_instruction_survives_in_any_source(self):
        for situation, b in self.built.items():
            with self.subTest(situation):
                self.assertEqual(conflicts_present(b["system"]), [])

    def test_the_conflict_check_sees_the_baseline_conflicts(self):
        base = bs.build(bs.VARIANTS["baseline"], captured("baseline"))
        self.assertGreaterEqual(len(conflicts_present(base["follow_up_players"]["system"])), 6)

    def test_every_situation_has_each_source_it_should(self):
        self.assertIn("context_block", self.built["first_turn_players"]["segments"])
        self.assertIn("stance", self.built["follow_up_players"]["segments"])
        self.assertNotIn("stance", self.built["first_turn"]["segments"])


if __name__ == "__main__":
    unittest.main()
