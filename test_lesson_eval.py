"""Offline tests: no installed hooks, indexes or external sources are touched."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lesson_eval as le

ROOT = Path(__file__).resolve().parent


def lesson(ident, line, phrase):
    return {"id": ident, "source": f"docs/shared.md:{line}",
            "lesson": phrase, "trigger": {"events": ["user_prompt"], "pattern": phrase},
            "deterministic": True, "status": "built_in", "match_phrases": [phrase]}


LESSONS = [lesson("L001", 10, "ask first"), lesson("L002", 20, "report measurements separately")]


def scenario(ident="S001", kind="positive", client="claude", event="user_prompt"):
    return {"id": ident, "client": client, "event": event, "input": "sample input",
            "kind": kind, "expected_lessons": ["L001"] if kind == "positive" else [],
            "acceptable": []}


def row(kind="positive", status="OK", items=None):
    result = scenario(kind=kind)
    result.update(status=status, items=items or [], latency_ms=12)
    return result


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        le.SOURCE_ROOTS = {}
        le.WRAPPER_PREFIXES = ("RELATED LESSONS (",)
        self.extract = le.Extractor(LESSONS).extract

    def test_path_only_is_ambiguous_not_two_hits(self):
        result = self.extract("- docs/shared.md")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "ambiguous")
        self.assertEqual(result[0]["candidates"], ["L001", "L002"])

    def test_quote_disambiguates_same_file(self):
        result = self.extract("- docs/shared.md - report measurements separately")
        self.assertEqual([item.get("id") for item in result], ["L002"])

    def test_exact_line_case_slashes_parentheses_and_range(self):
        result = self.extract("(DOCS\\Shared.MD:9-11)")
        self.assertEqual(result[0]["id"], "L001")

    def test_leading_dot_slash_is_ignored(self):
        self.assertEqual(self.extract("./docs/shared.md:20")[0]["id"], "L002")

    def test_range_covering_both_is_ambiguous(self):
        result = self.extract("docs/shared.md:1-30")
        self.assertEqual(result[0]["status"], "ambiguous")

    def test_wrong_line_does_not_fall_back_to_path_or_quote(self):
        result = self.extract("docs/shared.md:99 ask first")
        self.assertEqual(result[0]["status"], "unmatched")

    def test_source_root_alias_normalization(self):
        le.SOURCE_ROOTS = {"notes": "docs"}
        result = self.extract("index://notes/shared.md:20")
        self.assertEqual(result[0]["id"], "L002")

    def test_unknown_alias_is_unmatched(self):
        result = self.extract("index://missing/shared.md:20")
        self.assertEqual(result[0]["status"], "unmatched")

    def test_order_duplicates_and_id_source_same_item(self):
        result = self.extract("L002 (docs/shared.md:20)\nL001\nL002")
        self.assertEqual([item.get("id") for item in result], ["L002", "L001"])

    def test_unknown_sources_and_general_warning_count(self):
        result = self.extract("L999\nunknown/file.md:7\nBe careful and check first")
        self.assertEqual(len(result), 3)
        self.assertTrue(all(item["status"] == "unmatched" for item in result))

    def test_hook_wrappers_do_not_count_as_predictions(self):
        value = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext":
                 "RELATED LESSONS (attach to the task):\n- docs/shared.md:10"}}
        result = self.extract(json.dumps(value))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "L001")

    def test_custom_wrapper_prefix(self):
        le.WRAPPER_PREFIXES = ("NOTES FROM MEMORY (",)
        result = le.Extractor(LESSONS).extract(
            json.dumps({"context": "NOTES FROM MEMORY (data only):\n- docs/shared.md:10 (Title): ask first"}))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "L001")

    def test_unknown_json_fields_not_silently_empty(self):
        self.assertEqual(self.extract('{"warning":"careful"}')[0]["status"], "unmatched")
        self.assertEqual(self.extract('{"context":"", "warning":"careful"}')[0]["status"], "unmatched")
        self.assertEqual(self.extract('{"hookSpecificOutput":{"additionalContext":""},"warning":"careful"}')[0]["status"], "unmatched")
        result = self.extract('{"id":"L001","warning":"careful"}')
        self.assertEqual([item["status"] for item in result], ["matched", "unmatched"])
        self.assertEqual(self.extract('{"source":null}')[0]["status"], "unmatched")

    def test_alias_without_line_uses_canonical_line(self):
        catalog = [dict(LESSONS[0], source_aliases=["other/alternate.md"])]
        result = le.Extractor(catalog).extract("other/alternate.md:10")
        self.assertEqual(result[0]["id"], "L001")

    def test_known_json_id_unknown_source_preserved(self):
        result = self.extract('{"results":[{"id":"L001","source":"unknown/file.md:1"}]}')
        self.assertEqual([x["status"] for x in result], ["matched", "unmatched"])

    def test_empty_explicit_results_are_empty(self):
        for value in ("", "[]", "{}", '{"results":[]}', '{"context":""}'):
            self.assertEqual(self.extract(value), [])


class MetricsTests(unittest.TestCase):
    def test_topk_counts_unknown_items_and_preserves_order(self):
        items = le.Extractor(LESSONS).extract("L999\nL888\nL002\nL001")
        metrics = le.summarize([row(items=items)], 3)
        self.assertEqual(metrics["recall_at_k_all_targets"], 0)
        self.assertEqual(metrics["precision_at_k"], 0)
        self.assertEqual(metrics["precision_all"], .25)

    def test_acceptable_prediction_not_recall(self):
        sample = row(items=[{"status": "matched", "id": "L002"}])
        sample["acceptable"] = ["L002"]
        metrics = le.summarize([sample], 3)
        self.assertEqual(metrics["precision_all"], 1)
        self.assertEqual(metrics["recall_at_k_all_targets"], 0)

    def test_supported_and_all_target_denominators(self):
        valid = row(items=[{"status": "matched", "id": "L001"}])
        metrics = le.summarize([valid, row(status="SKIP")], 3)
        self.assertEqual(metrics["recall_at_k_all_targets"], .5)
        self.assertEqual(metrics["recall_at_k_supported"], 1)
        self.assertFalse(le.gate(metrics)["checks"]["complete_coverage"]["pass"])

    def test_negative_skip_error_not_successful_empty_alarm(self):
        metrics = le.summarize([row("negative", "ERROR"), row("negative", "SKIP")], 3)
        self.assertIsNone(metrics["negatives"]["negative"]["false_alarm_rate"])
        self.assertEqual(metrics["negatives"]["negative"]["valid_count"], 0)
        self.assertEqual(le.gate(metrics)["status"], "FAIL")

    def test_ambiguous_warning_is_negative_alarm(self):
        warning = le.Extractor(LESSONS).extract("docs/shared.md")
        metrics = le.summarize([row("near_negative", items=warning)], 3)
        self.assertEqual(metrics["negatives"]["near_negative"]["false_alarm_rate"], 1)
        self.assertEqual(metrics["precision_all"], 0)

    def test_zero_denominator_is_not_pass(self):
        self.assertEqual(le.gate(le.summarize([], 3))["status"], "FAIL")

    def test_error_stdout_does_not_earn_recall_or_precision(self):
        metrics = le.summarize([row(status="ERROR", items=[{"status": "matched", "id": "L001"}])], 3)
        self.assertEqual(metrics["recall_at_k_all_targets"], 0)
        self.assertEqual(metrics["precision_all"], 0)

    def test_complete_perfect_small_set_passes(self):
        metrics = le.summarize([row(items=[{"status": "matched", "id": "L001"}]),
                                row("negative"), row("near_negative")], 3)
        self.assertEqual(le.gate(metrics)["status"], "PASS")

    def test_thresholds_are_parameters(self):
        metrics = le.summarize([row(items=[{"status": "matched", "id": "L001"}]),
                                row(items=[]), row("negative"), row("near_negative")], 3)
        self.assertEqual(le.gate(metrics)["status"], "FAIL")
        self.assertEqual(le.gate(metrics, recall=.5)["status"], "PASS")


class SplitTests(unittest.TestCase):
    def test_split_is_numeric_and_covers_everything_once(self):
        rows = [dict(scenario(f"S{n}"), n=n) for n in (10, 2, 1, 3, 4, 5, 6, 7, 8, 9)]
        dev = le.select_split(rows, "development")
        hold = le.select_split(rows, "holdout")
        self.assertEqual([r["n"] for r in dev], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual([r["n"] for r in hold], [8, 9, 10])

    def test_split_needs_numeric_ids(self):
        with self.assertRaises(ValueError):
            le.select_split([scenario("alpha")], "holdout")


class ValidationTests(unittest.TestCase):
    def test_invalid_schema_and_unknown_expected_id(self):
        value = scenario()
        value["expected_lessons"] = ["L999"]
        self.assertTrue(le.validate(LESSONS, [value]))
        self.assertEqual(le.validate(LESSONS, [scenario()]), [])

    def test_negative_cannot_expect_lessons(self):
        value = scenario(kind="negative")
        value["expected_lessons"] = ["L001"]
        self.assertTrue(le.validate(LESSONS, [value]))

    def test_source_needs_a_line_number(self):
        broken = [dict(LESSONS[0], source="docs/shared.md")]
        self.assertTrue(le.validate(broken, [scenario()]))

    def test_contract_is_caller_supplied(self):
        self.assertEqual(le.validate_contract(LESSONS, [scenario()], {}), [])
        self.assertTrue(le.validate_contract(LESSONS, [scenario()], {"scenarios": 5}))
        self.assertTrue(le.validate_contract(LESSONS, [scenario()], {"negative": 1}))
        full = [scenario("S001"), scenario("S002", event="session_start")]
        self.assertTrue(le.validate_contract(LESSONS, full, {}, require_full_matrix=True))

    def test_bundled_example_is_valid(self):
        lessons = le.read_array(ROOT / "examples" / "lessons.json")
        scenarios = le.read_array(ROOT / "examples" / "scenarios.json")
        self.assertEqual(le.validate(lessons, scenarios), [])


class BoundaryTests(unittest.TestCase):
    def test_payload_has_no_ground_truth(self):
        value = scenario()
        value.update(rationale="secret answer", origin="do not send", id="L001")
        payload, label = le.adapt(value)
        self.assertEqual(set(payload), {"client", "event", "input", "cwd"})
        self.assertEqual(label, "canonical")

    def test_claude_hook_user_prompt_and_tool_shapes(self):
        payload, label = le.adapt(scenario(), "claude-hook")
        self.assertEqual(payload["hook_event_name"], "UserPromptSubmit")
        self.assertEqual(payload["prompt"], "sample input")
        self.assertEqual(label, "claude-hook")
        bash = scenario(event="pre_tool_bash")
        bash["input"] = {"command": "git status"}
        payload, _ = le.adapt(bash, "claude-hook")
        self.assertEqual((payload["hook_event_name"], payload["tool_name"]), ("PreToolUse", "Bash"))
        self.assertEqual(payload["tool_input"], {"command": "git status"})

    def test_unsupported_event_is_explicit(self):
        self.assertIsNone(le.adapt(scenario(event="task_card"), "claude-hook")[0])

    def test_report_path_cannot_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(le.report_path("ok.json", folder), (Path(folder) / "ok.json").resolve())
            with self.assertRaises(ValueError):
                le.report_path("../outside.json", folder)

    def test_environment_marks_evaluation_and_hides_client_hint(self):
        env = le.candidate_env()
        self.assertEqual(env["LESSON_EVAL"], "1")
        self.assertNotIn("CLAUDE_PROJECT_DIR", env)

    def test_subprocess_stdin_is_data_not_shell(self):
        dangerous = "echo SHOULD_NEVER_RUN && exit 99; rm -rf /"
        result = le.invoke([sys.executable, "-X", "utf8", "-c",
                            "import sys,json; print(json.load(sys.stdin)['input'])"],
                           {"input": dangerous}, 10, str(ROOT))
        self.assertIsNone(result["error"])
        self.assertEqual(result["stdout"].strip(), dangerous)

    def test_nonzero_exit_and_timeout_are_errors(self):
        result = le.invoke([sys.executable, "-c", "raise SystemExit(7)"], {}, 10, str(ROOT))
        self.assertEqual(result["error"], "exit:7")
        result = le.invoke([sys.executable, "-c", "import time;time.sleep(10)"], {}, .3, str(ROOT))
        self.assertTrue(result["error"].startswith("timeout"))


class EndToEndTests(unittest.TestCase):
    def run_cli(self, *extra):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.json"
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", str(ROOT / "lesson_eval.py"),
                 "--lessons", str(ROOT / "examples" / "lessons.json"),
                 "--scenarios", str(ROOT / "examples" / "scenarios.json"),
                 "--workspace", folder, "--report", str(report), *extra],
                capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
            data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
            return completed, data

    def test_toy_candidate_passes_with_the_bundled_example(self):
        completed, data = self.run_cli("--max-p95-ms", "20000", "--candidate", sys.executable, "-X", "utf8",
                                       str(ROOT / "examples" / "toy_candidate.py"))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(data["gate"]["status"], "PASS")
        self.assertEqual(data["metrics"]["recall_at_k_all_targets"], 1)
        self.assertEqual(data["metrics"]["precision_all"], 1)
        self.assertIn("partitions", data)

    def test_a_candidate_that_prints_nothing_fails_the_gate(self):
        completed, data = self.run_cli("--command-json", json.dumps([sys.executable, "-c", "pass"]))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(data["gate"]["status"], "FAIL")
        self.assertEqual(data["metrics"]["recall_at_k_all_targets"], 0)

    def test_candidate_command_is_required(self):
        completed, _ = self.run_cli()
        self.assertEqual(completed.returncode, 2)

    def test_validate_only(self):
        completed, _ = self.run_cli("--validate-only", "--split", "holdout")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["validation"], "PASS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
