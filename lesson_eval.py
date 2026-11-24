#!/usr/bin/env python3
"""Stdlib-only evaluator for lesson / reminder triggers.

A "candidate" is any program that reads one JSON event on stdin and prints the
lessons it would surface on stdout. This tool runs the candidate once per
labelled scenario in a fresh subprocess (cold start), extracts the lessons from
its output, and reports recall, precision, false-alarm rate and latency.
Scenario commands are data: they are never executed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
EVENTS = {"user_prompt", "pre_tool_bash", "pre_tool_edit", "pre_tool_agent",
          "session_start", "task_card"}
KINDS = {"positive", "negative", "near_negative"}
NEGATIVE_KINDS = ("negative", "near_negative")
ADAPTERS = ("canonical", "claude-hook")

# Optional alias roots: "<scheme>://<alias>/<rest>" is rewritten to "<root>/<rest>".
# Filled from --source-roots (JSON object alias -> directory).
SOURCE_ROOTS: dict = {}
# Lines starting with these prefixes are wrapper headings of a hook's output,
# not retrieval results (only skipped when the output also contains bullets).
WRAPPER_PREFIXES: tuple = ("RELATED LESSONS (",)

SOURCE_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://[^\s)\]<>\"']+?\.(?:md|txt)"
    r"|(?:[A-Za-z]:)?[\w./\\~-]+?\.(?:md|txt))"
    r"(?::\d+(?:[-–]\d+)?)?", re.IGNORECASE)
ID_RE = re.compile(r"(?<![\w])L\d+(?![\w])", re.IGNORECASE)


def folded(value):
    return " ".join(str(value).casefold().split())


def source_parts(value):
    """Return (normalized path, first line, last line); lines are None if absent."""
    value = str(value).strip().strip("`\"'()[] ").replace("\\", "/")
    line = re.search(r":(\d+)(?:[-–](\d+))?$", value)
    start = end = None
    if line:
        start = int(line[1])
        end = int(line[2] or line[1])
        value = value[:line.start()]
    scheme = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value)
    if scheme and len(scheme.group(0)) > 3:
        alias, sep, relative = value[scheme.end():].partition("/")
        roots = {k.casefold(): v for k, v in SOURCE_ROOTS.items()}
        if sep and alias.casefold() in roots:
            value = str(roots[alias.casefold()]).replace("\\", "/") + "/" + relative
    value = re.sub(r"/+", "/", value)
    while value.startswith("./"):
        value = value[2:]
    return value.casefold().rstrip("/"), start, end


def read_array(path):
    with open(path, encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"{path}: top-level JSON value must be an array")
    return data


def validate(lessons, scenarios):
    """Structural validation; returns a list of error strings (empty = valid)."""
    errors = []
    ids = set()
    if not lessons or not scenarios:
        errors.append("lesson and scenario arrays must not be empty")
    for index, lesson in enumerate(lessons):
        prefix = f"lesson[{index}]"
        if not isinstance(lesson, dict):
            errors.append(prefix + ": must be an object")
            continue
        ident = lesson.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"L\d+", ident):
            errors.append(prefix + ": id must look like L001")
        elif ident in ids:
            errors.append(prefix + ": duplicate id")
        else:
            ids.add(ident)
        for key in ("source", "lesson"):
            if not isinstance(lesson.get(key), str) or not lesson[key].strip():
                errors.append(prefix + f": {key} must be non-empty text")
        if isinstance(lesson.get("source"), str):
            _, start, end = source_parts(lesson["source"])
            if start is None or start < 1 or end < start:
                errors.append(prefix + ": source must end in a positive line number (path:line)")
        trigger = lesson.get("trigger")
        if (not isinstance(trigger, dict) or not isinstance(trigger.get("events"), list)
                or not trigger["events"] or not all(isinstance(v, str) for v in trigger["events"])
                or not set(trigger["events"]).issubset(EVENTS)
                or not isinstance(trigger.get("pattern"), str)):
            errors.append(prefix + ": invalid trigger (events: non-empty list of known events, pattern: text)")
        if type(lesson.get("deterministic")) is not bool:
            errors.append(prefix + ": deterministic must be a boolean")
        if lesson.get("status") not in {"built_in", "candidate"}:
            errors.append(prefix + ": status must be built_in or candidate")
        for key in ("match_phrases", "source_aliases"):
            values = lesson.get(key, [])
            if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
                errors.append(prefix + f": {key} must be an array of text")
        if not lesson.get("match_phrases"):
            errors.append(prefix + ": match_phrases must not be empty")
    seen = set()
    for index, scenario in enumerate(scenarios):
        prefix = f"scenario[{index}]"
        if not isinstance(scenario, dict):
            errors.append(prefix + ": must be an object")
            continue
        ident = scenario.get("id")
        if not isinstance(ident, str) or not ident or ident in seen:
            errors.append(prefix + ": id missing or duplicated")
        else:
            seen.add(ident)
        if not isinstance(scenario.get("client"), str) or not scenario.get("client"):
            errors.append(prefix + ": client must be non-empty text")
        if scenario.get("event") not in EVENTS:
            errors.append(prefix + ": unknown event")
        if scenario.get("kind") not in KINDS:
            errors.append(prefix + ": unknown kind")
        if not isinstance(scenario.get("input"), (str, dict)):
            errors.append(prefix + ": input must be text or an object")
        for key in ("expected_lessons", "acceptable"):
            values = scenario.get(key)
            if (not isinstance(values, list) or not all(isinstance(v, str) for v in values)
                    or len(values) != len(set(values)) or not set(values).issubset(ids)):
                errors.append(prefix + f": {key} must be an array of known, distinct lesson ids")
        expected = scenario.get("expected_lessons", [])
        if scenario.get("kind") == "positive" and not expected:
            errors.append(prefix + ": a positive scenario needs expected lessons")
        if scenario.get("kind") in NEGATIVE_KINDS and (expected or scenario.get("acceptable")):
            errors.append(prefix + ": a negative scenario cannot expect or accept lessons")
    return errors


def validate_contract(lessons, scenarios, minimums, require_full_matrix=False):
    """Optional dataset-size and coverage requirements, all caller-supplied."""
    errors = []
    if len(lessons) < minimums.get("lessons", 0):
        errors.append(f"dataset contract: at least {minimums['lessons']} lessons required")
    if len(scenarios) < minimums.get("scenarios", 0):
        errors.append(f"dataset contract: at least {minimums['scenarios']} scenarios required")
    for kind in sorted(KINDS):
        minimum = minimums.get(kind, 0)
        count = sum(row.get("kind") == kind for row in scenarios)
        if count < minimum:
            errors.append(f"dataset contract: {kind} needs at least {minimum} scenarios ({count})")
    if require_full_matrix:
        clients = {row.get("client") for row in scenarios}
        for client in sorted(clients):
            for event in sorted(EVENTS):
                if not any(row.get("client") == client and row.get("event") == event for row in scenarios):
                    errors.append(f"dataset contract: no scenario for client {client} / event {event}")
    return errors


def message(scenario):
    value = scenario["input"]
    if isinstance(value, str):
        return value
    return str(value.get("prompt") or value.get("content") or value.get("command") or "")


def adapt(scenario, adapter="canonical", cwd=None):
    """Return (payload, transport label); payload None means the event is unsupported."""
    client, event = scenario["client"], scenario["event"]
    cwd = str(cwd or ROOT)
    if adapter == "canonical":
        return {"client": client, "event": event, "input": scenario["input"], "cwd": cwd}, "canonical"
    if adapter == "claude-hook":
        # Minimal approximation of Claude Code hook inputs; not validated against every version.
        if event == "user_prompt":
            return {"hook_event_name": "UserPromptSubmit", "prompt": message(scenario),
                    "cwd": cwd, "session_id": "eval"}, "claude-hook"
        if event in {"pre_tool_bash", "pre_tool_agent"}:
            tool = "Bash" if event == "pre_tool_bash" else "Agent"
            entry = scenario["input"]
            tool_input = dict(entry) if isinstance(entry, dict) else {
                "command" if tool == "Bash" else "prompt": entry}
            return {"hook_event_name": "PreToolUse", "tool_name": tool,
                    "tool_input": tool_input, "cwd": cwd, "session_id": "eval"}, "claude-hook"
    return None, "unsupported"


def select_split(scenarios, split="all"):
    """Freeze a numeric-ID 70/30 split without inspecting inputs or labels."""
    if split == "all":
        return list(scenarios)
    if split not in {"development", "holdout"}:
        raise ValueError("split must be development, holdout or all")
    if any(not re.fullmatch(r"S\d+", row["id"]) for row in scenarios):
        raise ValueError("a held-out split needs scenario ids of the form S + number")
    ordered = sorted(scenarios, key=lambda row: int(row["id"][1:]))
    numbers = [int(row["id"][1:]) for row in ordered]
    if len(numbers) != len(set(numbers)):
        raise ValueError("numeric scenario ids must be unique")
    boundary = len(ordered) - math.ceil(len(ordered) * .30)
    return ordered[:boundary] if split == "development" else ordered[boundary:]


class Extractor:
    """Turns a candidate's stdout into a list of matched / ambiguous / unmatched items."""

    def __init__(self, lessons):
        self.lessons = {lesson["id"]: lesson for lesson in lessons}
        self.paths = {}
        for lesson in lessons:
            _, canonical_start, canonical_end = source_parts(lesson["source"])
            for source in [lesson["source"], *lesson.get("source_aliases", [])]:
                path, start, end = source_parts(source)
                if start is None:
                    start, end = canonical_start, canonical_end
                self.paths.setdefault(path, []).append((lesson["id"], start, end))

    def resolve_source(self, source, context):
        path, start, end = source_parts(source)
        options = self.paths.get(path, [])
        # An alias without line information retains the canonical lesson line.
        if start is not None:
            options = [(ident, a, b) for ident, a, b in options
                       if a is not None and max(a, start) <= min(b, end)]
        ids = list(dict.fromkeys(x[0] for x in options))
        if len(ids) > 1:
            matches = [ident for ident in ids if any(
                folded(phrase) in folded(context)
                for phrase in self.lessons[ident].get("match_phrases", []))]
            ids = matches if len(matches) == 1 else ids
        if len(ids) == 1:
            return {"status": "matched", "id": ids[0], "raw": source}
        if ids:
            return {"status": "ambiguous", "candidates": ids, "raw": source}
        return {"status": "unmatched", "raw": source}

    def extract(self, stdout):
        records = []
        seen = set()

        def add(record):
            key = (record["status"], record.get("id") or folded(record["raw"]))
            if key not in seen:
                seen.add(key)
                records.append(record)

        def dump(value):
            return json.dumps(value, ensure_ascii=False)

        def text_items(value):
            lines = [line.strip() for line in value.splitlines() if line.strip()]
            has_bullets = any(re.match(r"^(?:[-*•]|\d+[.)])\s", line) for line in lines)
            for line in lines:
                # A known hook wrapper heading conveys no retrieval result itself.
                if has_bullets and WRAPPER_PREFIXES and line.startswith(WRAPPER_PREFIXES):
                    continue
                ids = list(dict.fromkeys(m.group(0).upper() for m in ID_RE.finditer(line)))
                sources = [m.group(0) for m in SOURCE_RE.finditer(line)]
                hits = []
                for ident in ids:
                    hits.append({"status": "matched" if ident in self.lessons else "unmatched",
                                 **({"id": ident} if ident in self.lessons else {}), "raw": ident})
                for source in sources:
                    record = self.resolve_source(source, line)
                    if record.get("id") not in {h.get("id") for h in hits if h["status"] == "matched"}:
                        hits.append(record)
                if not hits:
                    hits = [{"status": "unmatched", "raw": line}]
                for record in hits:
                    add(record)

        def walk(value):
            if value is None or value == "" or value == [] or value == {}:
                return
            if isinstance(value, str):
                text_items(value)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                if "id" in value or "source" in value:
                    ident = value.get("id")
                    source = value.get("source")
                    if isinstance(ident, str) and ident in self.lessons:
                        add({"status": "matched", "id": ident, "raw": dump(value)})
                        if source:
                            resolved = self.resolve_source(str(source), dump(value))
                            if resolved.get("id") != ident:
                                add(resolved)
                    elif ident is not None:
                        add({"status": "unmatched", "raw": str(ident)})
                        if source:
                            add(self.resolve_source(str(source), dump(value)))
                    elif source:
                        add(self.resolve_source(str(source), dump(value)))
                    else:
                        add({"status": "unmatched", "raw": dump(value)})
                    extras = {key: item for key, item in value.items() if key not in {
                        "id", "source", "lesson", "content", "text", "title", "score"}}
                    if extras:
                        add({"status": "unmatched", "raw": dump(extras)})
                elif "hookSpecificOutput" in value:
                    hook = value["hookSpecificOutput"]
                    if isinstance(hook, dict):
                        walk(hook.get("additionalContext"))
                        extras = {k: v for k, v in hook.items() if k not in {"additionalContext", "hookEventName"}}
                        if extras:
                            add({"status": "unmatched", "raw": dump(extras)})
                    else:
                        add({"status": "unmatched", "raw": dump(value)})
                    extras = {k: v for k, v in value.items() if k != "hookSpecificOutput"}
                    if extras:
                        add({"status": "unmatched", "raw": dump(extras)})
                elif "context" in value:
                    walk(value["context"])
                    extras = {k: v for k, v in value.items() if k != "context"}
                    if extras:
                        add({"status": "unmatched", "raw": dump(extras)})
                elif any(key in value for key in ("results", "lessons", "ids", "sources")):
                    for key in value:
                        walk(value[key])
                else:
                    add({"status": "unmatched", "raw": dump(value)})
            else:
                add({"status": "unmatched", "raw": json.dumps(value)})

        stripped = stdout.strip()
        if not stripped:
            return []
        try:
            walk(json.loads(stripped))
        except json.JSONDecodeError:
            text_items(stripped)
        return records


def candidate_env():
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "LESSON_EVAL": "1"})
    # Client classification must come from the payload, not from this terminal.
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env


def stop_tree(proc):
    if os.name == "nt":
        # The PID is the process this runner created, never a name/global selector.
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False, timeout=5, shell=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()


def invoke(command, payload, timeout, cwd):
    start = time.perf_counter()
    proc = None
    result = {"stdout": "", "stderr": "", "returncode": None, "error": None}
    try:
        kwargs = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, shell=False, cwd=cwd,
                                env=candidate_env(), **kwargs)
        try:
            out, err = proc.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            result["error"] = "timeout"
            stop_tree(proc)
            try:
                out, err = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                # A detached descendant may retain inherited pipes: close our ends.
                for pipe in (proc.stdout, proc.stderr):
                    if pipe:
                        pipe.close()
                out, err = b"", b""
                result["error"] = "timeout-pipe-cleanup"
        result.update(stdout=out.decode("utf-8", "replace"), stderr=err.decode("utf-8", "replace"),
                      returncode=proc.poll())
        if proc.returncode and not result["error"]:
            result["error"] = f"exit:{proc.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        if proc is not None and proc.poll() is None:
            stop_tree(proc)
            proc.wait(timeout=2)
    result["latency_ms"] = round((time.perf_counter() - start) * 1000, 3)
    return result


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def summarize(rows, k):
    expected_all = sum(len(row["expected_lessons"]) for row in rows)
    supported = [row for row in rows if row["status"] != "SKIP"]
    expected_supported = sum(len(row["expected_lessons"]) for row in supported)
    recall_hits = tp_all = tp_k = predicted_all = predicted_k = 0
    for row in supported:
        items = row["items"]
        expected = set(row["expected_lessons"])
        accepted = expected | set(row["acceptable"])
        if row["status"] == "OK":
            recall_hits += len(expected & {x.get("id") for x in items[:k] if x["status"] == "matched"})
        for subset, name in ((items, "all"), (items[:k], "k")):
            correct = sum(row["status"] == "OK" and x["status"] == "matched"
                          and x.get("id") in accepted for x in subset)
            if name == "all":
                tp_all += correct
                predicted_all += len(subset)
            else:
                tp_k += correct
                predicted_k += len(subset)
    negatives = {}
    for kind in NEGATIVE_KINDS:
        target = [row for row in rows if row["kind"] == kind]
        valid = [row for row in target if row["status"] == "OK"]
        alarms = sum(bool(row["items"]) for row in valid)
        negatives[kind] = {"target_count": len(target), "valid_count": len(valid),
                           "unsupported_count": sum(row["status"] == "SKIP" for row in target),
                           "error_count": sum(row["status"] == "ERROR" for row in target),
                           "alarm_count": alarms, "false_alarm_rate": ratio(alarms, len(valid))}
    latencies = sorted(row["latency_ms"] for row in supported)
    return {"target_count": len(rows), "supported_count": len(supported),
            "unsupported_count": len(rows) - len(supported),
            "error_count": sum(row["status"] == "ERROR" for row in supported),
            "expected_all": expected_all, "expected_supported": expected_supported,
            "recall_hits_at_k": recall_hits,
            "recall_at_k_all_targets": ratio(recall_hits, expected_all),
            "recall_at_k_supported": ratio(recall_hits, expected_supported),
            "precision_all": ratio(tp_all, predicted_all), "precision_at_k": ratio(tp_k, predicted_k),
            "correct_all": tp_all, "predicted_all": predicted_all,
            "correct_at_k": tp_k, "predicted_at_k": predicted_k,
            "negatives": negatives,
            "latency_median_ms": statistics.median(latencies) if latencies else None,
            "latency_p95_ms": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None}


def gate(metrics, recall=.9, precision=.7, false_alarm=.05, p95=1500):
    checks = {}

    def check(name, value, threshold, minimum=True):
        checks[name] = {"value": value, "threshold": threshold,
                        "pass": value is not None and (value >= threshold if minimum else value <= threshold)}
    check("recall_at_k_all_targets", metrics["recall_at_k_all_targets"], recall)
    check("precision_all", metrics["precision_all"], precision)
    check("latency_p95_ms", metrics["latency_p95_ms"], p95, False)
    for kind, counts in metrics["negatives"].items():
        if counts["target_count"] == 0:
            # No scenario of this kind in the selected split: the rate is undefined, not failed.
            # SKIP/ERROR targets still fail the coverage and error checks below.
            checks[kind + "_false_alarm_rate"] = {"value": None, "threshold": false_alarm,
                                                  "pass": True, "not_applicable": True}
            continue
        check(kind + "_false_alarm_rate", counts["false_alarm_rate"], false_alarm, False)
    checks["complete_coverage"] = {"pass": metrics["target_count"] > 0 and metrics["unsupported_count"] == 0,
                                   "unsupported_count": metrics["unsupported_count"]}
    checks["no_errors"] = {"pass": metrics["error_count"] == 0, "error_count": metrics["error_count"]}
    return {"status": "PASS" if all(item["pass"] for item in checks.values()) else "FAIL", "checks": checks}


def evaluate(lessons, scenarios, command, adapter, timeout, k, cwd):
    extractor = Extractor(lessons)
    rows = []
    for scenario in scenarios:
        payload, transport = adapt(scenario, adapter, cwd)
        row = {key: scenario[key] for key in ("id", "client", "event", "kind", "expected_lessons", "acceptable")}
        row.update(transport=transport, items=[], stdout="", stderr="")
        if payload is None:
            row.update(status="SKIP", reason="the adapter does not support this client/event")
            row["latency_ms"] = 0.0
        else:
            result = invoke(command, payload, timeout, cwd)
            row.update(result)
            row["status"] = "ERROR" if result["error"] else "OK"
            row["items"] = extractor.extract(result["stdout"])
        row["matched"] = [item for item in row["items"] if item["status"] == "matched"]
        row["unmatched"] = [item for item in row["items"] if item["status"] == "unmatched"]
        row["ambiguous"] = [item for item in row["items"] if item["status"] == "ambiguous"]
        rows.append(row)
    metrics = summarize(rows, k)
    matrix = []
    for client in sorted({row["client"] for row in rows}):
        for event in sorted({row["event"] for row in rows}):
            subset = [row for row in rows if row["client"] == client and row["event"] == event]
            if subset:
                matrix.append({"client": client, "event": event, **summarize(subset, k)})
    return {"format_version": 1, "adapter": adapter, "command": command, "k": k,
            "timing": "one fresh subprocess per scenario; spawn+stdin+execution+stdout+cleanup; cold start",
            "safety": "shell=False; scenario commands are stdin data; the candidate command is not sandboxed",
            "metrics": metrics, "client_event": matrix, "scenarios": rows}


def partition_metrics(rows, k):
    """Summarize already captured results; never invoke the candidate again."""
    return {name: {"ids": [row["id"] for row in selected], "metrics": summarize(selected, k)}
            for name in ("development", "holdout")
            for selected in [select_split(rows, name)]}


def report_path(value, workspace):
    workspace = Path(workspace).resolve()
    destination = Path(value)
    if not destination.is_absolute():
        destination = workspace / destination
    destination = destination.resolve()
    if not destination.is_relative_to(workspace):
        raise ValueError("the report must be inside the chosen workspace")
    if not destination.parent.is_dir():
        raise ValueError("the report's parent folder must exist")
    return destination


def run_self_test():
    import unittest
    sys.path.insert(0, str(ROOT))
    import test_lesson_eval
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_lesson_eval)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main(argv=None):
    global SOURCE_ROOTS, WRAPPER_PREFIXES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lessons", default=str(ROOT / "examples" / "lessons.json"))
    parser.add_argument("--scenarios", default=str(ROOT / "examples" / "scenarios.json"))
    parser.add_argument("--adapter", choices=ADAPTERS, default="canonical")
    parser.add_argument("--split", choices=["development", "holdout", "all"], default="all",
                        help="numeric id order: first 70%% development, last ceil(30%%) holdout")
    parser.add_argument("--command-json", help="candidate argv as a JSON array; no shell quoting applies")
    parser.add_argument("--candidate", nargs=argparse.REMAINDER,
                        help="must be the last option: executable followed by its separate argv items")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds; 0 < timeout <= 60")
    parser.add_argument("--workspace", default=os.getcwd(),
                        help="working directory of the candidate and the folder the report must stay in")
    parser.add_argument("--report", default="report.json")
    parser.add_argument("--source-roots", help="JSON object mapping alias -> directory for scheme://alias/path sources")
    parser.add_argument("--wrapper-prefix", action="append", default=[],
                        help="output lines starting with this text are wrapper headings, not results (repeatable)")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--min-recall", type=float, default=.9)
    parser.add_argument("--min-precision", type=float, default=.7)
    parser.add_argument("--max-false-alarm", type=float, default=.05)
    parser.add_argument("--max-p95-ms", type=float, default=1500)
    parser.add_argument("--min-lessons", type=int, default=1)
    parser.add_argument("--min-scenarios", type=int, default=1)
    parser.add_argument("--min-positive", type=int, default=0)
    parser.add_argument("--min-negative", type=int, default=0)
    parser.add_argument("--min-near-negative", type=int, default=0)
    parser.add_argument("--require-full-matrix", action="store_true",
                        help="require a scenario for every client x event combination present")
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_test()
    if args.k < 1 or not 0 < args.timeout <= 60:
        parser.error("k >= 1 and 0 < timeout <= 60 are required")
    if any(not 0 <= v <= 1 for v in (args.min_recall, args.min_precision, args.max_false_alarm)) or args.max_p95_ms <= 0:
        parser.error("ratio thresholds must be within 0..1 and the p95 threshold above 0")
    try:
        if args.source_roots:
            with open(args.source_roots, encoding="utf-8-sig") as handle:
                roots = json.load(handle)
            if not isinstance(roots, dict):
                raise ValueError("--source-roots must be a JSON object")
            SOURCE_ROOTS = roots
        if args.wrapper_prefix:
            WRAPPER_PREFIXES = tuple(args.wrapper_prefix)
        lessons, scenarios = read_array(args.lessons), read_array(args.scenarios)
        errors = validate(lessons, scenarios)
        if not errors:
            errors.extend(validate_contract(
                lessons, scenarios,
                {"lessons": args.min_lessons, "scenarios": args.min_scenarios,
                 "positive": args.min_positive, "negative": args.min_negative,
                 "near_negative": args.min_near_negative}, args.require_full_matrix))
        if errors:
            print(json.dumps({"validation": "FAIL", "errors": errors}, ensure_ascii=False, indent=2))
            return 2
        dataset_count = len(scenarios)
        scenarios = select_split(scenarios, args.split)
        if args.validate_only:
            print(json.dumps({"validation": "PASS", "lessons": len(lessons), "scenarios": len(scenarios),
                              "dataset_scenarios": dataset_count, "split": args.split,
                              "selected_ids": [row["id"] for row in scenarios]}, ensure_ascii=False))
            return 0
        if args.command_json and args.candidate is not None:
            parser.error("--command-json and --candidate cannot be combined")
        command = json.loads(args.command_json) if args.command_json else args.candidate
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            parser.error("a candidate command is required: a non-empty argv array of text")
        workspace = Path(args.workspace).resolve()
        if not workspace.is_dir():
            raise ValueError("the workspace must be an existing folder")
        destination = report_path(args.report, workspace)
        if destination in {Path(args.lessons).resolve(), Path(args.scenarios).resolve(), Path(__file__).resolve(),
                           ROOT / "test_lesson_eval.py", ROOT / "README.md"}:
            raise ValueError("the report must not overwrite an input, the code or the documentation")
        report = evaluate(lessons, scenarios, command, args.adapter, args.timeout, args.k, str(workspace))
        report["split"] = {"name": args.split, "dataset_count": dataset_count,
                           "selected_count": len(scenarios), "selected_ids": [row["id"] for row in scenarios],
                           "method": "numeric scenario id ascending; last ceil(n * .30) held out"}
        if args.split == "all" and all(re.fullmatch(r"S\d+", row["id"]) for row in scenarios):
            report["partitions"] = partition_metrics(report["scenarios"], args.k)
        report["gate"] = gate(report["metrics"], args.min_recall, args.min_precision, args.max_false_alarm, args.max_p95_ms)
        with open(destination, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(json.dumps({"report": str(destination), "gate": report["gate"], "metrics": report["metrics"]},
                         ensure_ascii=False, indent=2))
        return 0 if report["gate"]["status"] == "PASS" else 1
    except (OSError, ValueError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
