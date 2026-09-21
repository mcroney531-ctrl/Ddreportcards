"""
Direct unit tests for agents/usage.py's accounting invariants, kept separate
from tests/test_usage_callbacks_async.py (which covers the event-loop
regression specifically). Making the callbacks async in that commit must not
change any of these -- only when reserve()/settle() run, never what they
decide or how failures are handled.
"""
import types
import unittest
from unittest import mock

import agents.usage as usage_mod
import budget


class InvocationReservationIsolationMixin:
    def setUp(self):
        super().setUp()
        self._reservations_snapshot = dict(usage_mod._reservations)

    def tearDown(self):
        usage_mod._reservations.clear()
        usage_mod._reservations.update(self._reservations_snapshot)
        super().tearDown()


class ReservationHappensBeforeAnyModelWorkTest(InvocationReservationIsolationMixin, unittest.IsolatedAsyncioTestCase):
    async def test_check_budget_before_model_reserves_and_stores_before_returning(self):
        callback_context = types.SimpleNamespace(invocation_id="inv-reserve-1")
        llm_request = types.SimpleNamespace(model="anthropic/claude-sonnet-4-6")
        fake_reservation = budget.Reservation(key="day-1", model="claude-sonnet-4-6", micros=1000)

        with mock.patch.object(budget, "reserve", return_value=fake_reservation) as mock_reserve:
            result = await usage_mod.check_budget_before_model(callback_context, llm_request)

        # None lets ADK proceed to call the model -- a non-None return would
        # short-circuit the call, which is not what a successful reservation
        # means.
        self.assertIsNone(result)
        mock_reserve.assert_called_once()
        self.assertIs(usage_mod._reservations["inv-reserve-1"], fake_reservation)


class SuccessfulResponseSettlesToActualUsageTest(InvocationReservationIsolationMixin, unittest.IsolatedAsyncioTestCase):
    async def test_record_model_usage_settles_with_the_reported_token_counts(self):
        callback_context = types.SimpleNamespace(invocation_id="inv-settle-1")
        reservation = budget.Reservation(key="day-1", model="claude-sonnet-4-6", micros=5000)
        usage_mod._reservations["inv-settle-1"] = reservation
        llm_response = types.SimpleNamespace(
            usage_metadata=types.SimpleNamespace(prompt_token_count=123, candidates_token_count=45)
        )

        with mock.patch.object(budget, "settle", return_value=999) as mock_settle:
            result = await usage_mod.record_model_usage(callback_context, llm_response)

        self.assertIsNone(result)
        mock_settle.assert_called_once_with(reservation, 123, 45)
        self.assertNotIn("inv-settle-1", usage_mod._reservations)

    async def test_no_usage_reported_leaves_the_reservation_charged(self):
        callback_context = types.SimpleNamespace(invocation_id="inv-settle-2")
        reservation = budget.Reservation(key="day-1", model="claude-sonnet-4-6", micros=3000)
        usage_mod._reservations["inv-settle-2"] = reservation
        llm_response = types.SimpleNamespace(usage_metadata=None)

        with mock.patch.object(budget, "settle") as mock_settle:
            result = await usage_mod.record_model_usage(callback_context, llm_response)

        self.assertIsNone(result)
        mock_settle.assert_not_called()
        # The association is already popped (can't be settled twice), but the
        # money itself was never released -- there is no budget.release() call
        # anywhere on this path.
        self.assertNotIn("inv-settle-2", usage_mod._reservations)


class ProviderFailureLeavesReservationChargedTest(unittest.TestCase):
    def setUp(self):
        self._reservations_snapshot = dict(usage_mod._reservations)

    def tearDown(self):
        usage_mod._reservations.clear()
        usage_mod._reservations.update(self._reservations_snapshot)

    def test_clear_invocation_reservation_drops_association_without_releasing_money(self):
        callback_context = types.SimpleNamespace(invocation_id="inv-error-1")
        reservation = budget.Reservation(key="day-1", model="claude-sonnet-4-6", micros=7000)
        usage_mod._reservations["inv-error-1"] = reservation

        with mock.patch.object(budget, "release") as mock_release:
            # ADK invokes this with all three as keyword arguments -- see the
            # function's own docstring for why that's load-bearing.
            result = usage_mod.clear_invocation_reservation(
                callback_context=callback_context,
                llm_request=types.SimpleNamespace(),
                error=RuntimeError("simulated provider failure"),
            )

        # None -> ADK re-raises the original error rather than treating this
        # as a handled response.
        self.assertIsNone(result)
        self.assertNotIn("inv-error-1", usage_mod._reservations)
        mock_release.assert_not_called()


class SettlementFailureAbortsTheRunTest(InvocationReservationIsolationMixin, unittest.IsolatedAsyncioTestCase):
    async def test_record_model_usage_reraises_when_settle_fails(self):
        callback_context = types.SimpleNamespace(invocation_id="inv-settle-fail-1")
        reservation = budget.Reservation(key="day-1", model="claude-sonnet-4-6", micros=2000)
        usage_mod._reservations["inv-settle-fail-1"] = reservation
        llm_response = types.SimpleNamespace(
            usage_metadata=types.SimpleNamespace(prompt_token_count=10, candidates_token_count=5)
        )

        with mock.patch.object(budget, "settle", side_effect=budget.BudgetUnavailable("store down")):
            with self.assertRaises(budget.BudgetUnavailable):
                await usage_mod.record_model_usage(callback_context, llm_response)

        # A write that fails here must abort the run rather than continue
        # silently -- logging and carrying on would leave this round missing
        # from the ledger while the pipeline kept spending against a total it
        # no longer matches. The association is already popped either way,
        # so a retry can't double-settle it.
        self.assertNotIn("inv-settle-fail-1", usage_mod._reservations)


class NoMissingAssociationRegressionTest(InvocationReservationIsolationMixin, unittest.IsolatedAsyncioTestCase):
    async def test_record_model_usage_handles_an_absent_reservation_without_crashing(self):
        callback_context = types.SimpleNamespace(invocation_id="inv-never-reserved")
        llm_response = types.SimpleNamespace(
            usage_metadata=types.SimpleNamespace(prompt_token_count=10, candidates_token_count=5)
        )

        with mock.patch.object(budget, "settle") as mock_settle:
            result = await usage_mod.record_model_usage(callback_context, llm_response)

        self.assertIsNone(result)
        mock_settle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
