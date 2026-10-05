"""运行器只调用公开接口，校验快照并传播取消。"""
from __future__ import annotations

import asyncio
import copy
import unittest

from agent_core.evaluation.contracts import (
    EvalCase, EvaluationError, RunConfig, RunManifest,
)
from agent_core.evaluation.runner import EvaluationRunner, run_suite


def make_case(case_id="A", **changes):
    fields = dict(case_id=case_id, kind="answer", query={"filters": []},
                  context={}, expected={"status": "ok", "rows": [{"secret_gold": 123}]},
                  data_snapshot_id="snapshot-v1")
    return EvalCase(**(fields | changes))


def make_manifest(**changes):
    return RunManifest(**(dict(run_id="run-1", suite_version="suite-v1",
                               data_snapshot_id="snapshot-v1", provider_origin="fake",
                               execution_origin="mock") | changes))


async def verified(manifest):
    return {"verified": True, "snapshot_id": manifest.data_snapshot_id}


class EvaluationRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_existing_contract_without_gold_and_isolates_mutation(self):
        calls = []

        class Target:
            async def query(self, query, context):
                calls.append(copy.deepcopy((query, context)))
                query["filters"].append("mutated")
                return {"status": "ok", "rows": []}

        case = make_case()
        run = await run_suite(Target(), [case], make_manifest(), snapshot_verifier=verified)
        self.assertEqual(run.status, "complete")
        self.assertTrue(run.preconditions["verified"])
        self.assertEqual(calls[0][0], {"filters": []})
        self.assertNotIn("expected", calls[0][1])
        self.assertNotIn("secret_gold", str(calls))
        self.assertEqual(case.query, {"filters": []})
        self.assertEqual(run.cases[0].query, {"filters": []})
        self.assertEqual(calls[0][1]["request_id"], "run-1:A:1")

    async def test_missing_or_failed_precondition_never_calls_target(self):
        class Target:
            async def query(self, *args):
                raise AssertionError("未核实快照不能运行")

        for callback in (None, lambda manifest: asyncio.sleep(0, result={"verified": False})):
            run = await run_suite(Target(), [make_case()], make_manifest(), snapshot_verifier=callback)
            self.assertEqual(run.status, "invalid")
            self.assertEqual(run.observations, [])
            self.assertEqual(run.error_code, "SNAPSHOT_NOT_VERIFIED")

    async def test_changed_final_snapshot_invalidates_run(self):
        count = 0

        async def verifier(manifest):
            nonlocal count
            count += 1
            return {"verified": count == 1}

        class Target:
            async def query(self, *args):
                return {"status": "ok"}

        run = await run_suite(Target(), [make_case()], make_manifest(), snapshot_verifier=verifier)
        self.assertEqual(count, 2)
        self.assertEqual(run.status, "invalid")
        self.assertEqual(run.error_code, "SNAPSHOT_CHANGED")
        self.assertEqual(len(run.observations), 1)

    async def test_observed_schema_change_invalidates_even_if_final_is_restored(self):
        async def verifier(manifest):
            return {"verified": True, "schema_fingerprint_sha256": "original"}

        class Target:
            async def query(self, *args):
                return {"status": "ok", "metadata": {"schema_fingerprint_sha256": "changed"}}

        run = await run_suite(Target(), [make_case()], make_manifest(), snapshot_verifier=verifier)
        self.assertEqual(run.status, "invalid")
        self.assertTrue(run.preconditions["schema_changed_during_run"])
        self.assertFalse(run.preconditions["verified"])

    async def test_timeout_records_failure_after_target_cleanup(self):
        closed = asyncio.Event()

        class Target:
            async def query(self, *args):
                try:
                    await asyncio.sleep(10)
                finally:
                    closed.set()

        run = await run_suite(Target(), [make_case()], make_manifest(), RunConfig(timeout_ms=20),
                              snapshot_verifier=verified)
        self.assertTrue(closed.is_set())
        self.assertEqual(run.observations[0].response["error_code"], "EVALUATION_TIMEOUT")
        self.assertIsNone(run.observations[0].response["execution_attempted"])
        self.assertGreaterEqual(run.observations[0].elapsed_ms, 15)

    async def test_external_cancel_propagates_and_keeps_partial_run(self):
        started, closed = asyncio.Event(), asyncio.Event()

        class Target:
            async def query(self, query, context):
                if context["task_id"] == "A":
                    return {"status": "ok"}
                started.set()
                try:
                    await asyncio.sleep(10)
                finally:
                    closed.set()

        runner = EvaluationRunner()
        task = asyncio.create_task(runner.run_suite(Target(), [make_case(), make_case("B")],
                                                    make_manifest(), snapshot_verifier=verified))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        self.assertEqual(runner.last_run.status, "interrupted")
        self.assertEqual(len(runner.last_run.observations), 1)
        self.assertFalse(runner.last_run.preconditions["verified"])

    async def test_exception_is_sanitized_and_counted(self):
        class Target:
            async def query(self, *args):
                raise RuntimeError("password=secret")

        run = await run_suite(Target(), [make_case()], make_manifest(), snapshot_verifier=verified)
        self.assertEqual(run.observations[0].response["error_code"], "TARGET_ERROR")
        self.assertNotIn("secret", str(run.observations[0].response))

    async def test_repeats_split_and_roadmap_are_explicit(self):
        calls = []

        class Target:
            async def query(self, *args):
                calls.append(args)
                return {"status": "ok"}

        cases = [make_case(), make_case("B", split="test"),
                 make_case("R", kind="roadmap", scope="roadmap")]
        run = await run_suite(Target(), cases, make_manifest(), RunConfig(repeats=3),
                              snapshot_verifier=verified)
        self.assertEqual(run.planned_attempts, 3)
        self.assertEqual(len(run.cases), 2)
        self.assertEqual([o.attempt_id for o in run.observations], [1, 2, 3])
        self.assertEqual(len(calls), 3)

    async def test_duplicate_and_snapshot_mismatch_fail_before_run(self):
        class Target:
            async def query(self, *args):
                raise AssertionError

        for cases in ([make_case(), make_case()], [make_case(data_snapshot_id="different")]):
            with self.assertRaises(EvaluationError):
                await run_suite(Target(), cases, make_manifest(), snapshot_verifier=verified)

    def test_invalid_config_and_real_model_without_id_are_rejected(self):
        for changes in ({"repeats": True}, {"repeats": 0}, {"timeout_ms": -1}, {"split": "unknown"}):
            with self.assertRaises(EvaluationError):
                RunConfig(**changes)
        with self.assertRaises(EvaluationError):
            make_manifest(provider_origin="real")


if __name__ == "__main__":
    unittest.main()
