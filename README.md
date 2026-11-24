> 🚧 **Status: active development.** It works for the author; interfaces may change.

# lesson-trigger-eval

A small, standard-library-only Python tool that measures **how well a "lesson" or "reminder" trigger works**: given a catalog of short lessons and a set of labelled events, does your trigger surface the right lesson at the right moment, and stay quiet otherwise?

It is meant for people who wire reminders into coding-agent hooks (for example "before a force-push, remind me to ask first") and want numbers instead of impressions. Your trigger is any program that reads one JSON event on stdin and prints the lessons it would show. This tool runs it once per scenario in a fresh process and reports recall, precision, false-alarm rate and latency.

## Quick start

Requires Python 3.12 or newer. Run from the repository root:

```
python lesson_eval.py --report report.json --max-p95-ms 5000 --candidate python examples/toy_candidate.py
```

This evaluates the bundled toy candidate (a plain substring matcher) against the bundled example data (5 lessons, 15 scenarios) and writes `report.json`. The exit code is 0 when the quality gate passes and 1 when it does not. Run the tests with `python -m unittest` (or `python lesson_eval.py --self-test`).

## How it works

1. **Lessons** (`examples/lessons.json`): each has an `id` (`L001`), a `source` (`path:line`), the lesson text, a trigger description (events + pattern) and `match_phrases` used to disambiguate several lessons from one file.
2. **Scenarios** (`examples/scenarios.json`): each has a `client`, an `event` (`user_prompt`, `pre_tool_bash`, `pre_tool_edit`, `pre_tool_agent`, `session_start`, `task_card`), an `input`, a `kind` (`positive`, `negative`, `near_negative`) and the lessons expected (and merely acceptable) for it. A near-negative is on the same topic but the lesson does not apply.
3. **Candidate protocol**: stdin gets `{"client", "event", "input", "cwd"}` (never the ground truth). stdout may be JSON such as `{"results":[{"id":"L001","source":"examples/lessons.md:3"}]}` or plain text that mentions lesson ids or `path:line` sources. A non-zero exit or a timeout counts as an error, never as an empty (successful) answer.
4. **Scoring**: recall@k (default k=3), precision over everything the candidate printed (unknown or unparsable items count against it), false-alarm rate on negative and near-negative scenarios, median and p95 cold-start latency, plus a client x event matrix and a numeric-id 70/30 development/holdout split. The **gate** compares these with thresholds you set.

Useful options: `--adapter claude-hook` (sends Claude Code style hook inputs instead of the canonical payload; unsupported events are reported as skipped), `--source-roots roots.json` (map `scheme://alias/path` sources to directories), `--wrapper-prefix TEXT` (ignore a hook's heading line), `--min-recall`, `--min-precision`, `--max-false-alarm`, `--max-p95-ms`, `--min-positive` and the other `--min-*` dataset-size checks, `--split development|holdout`, `--validate-only`. See `python lesson_eval.py --help`.

## Requirements

Python 3.12+, no third-party packages. Tested on the author's Windows machine. A CI workflow for Ubuntu and Windows is included but has not run yet.

## Limits and honest caveats

- **Not a sandbox.** Scenario inputs are passed as data and never executed, but the candidate program runs with your permissions, can read files and use the network, and may leave detached child processes the runner cannot reach. Review the candidate yourself.
- Latency is **cold start per scenario** (process spawn + run), so it is dominated by interpreter start-up; it is not a measure of a warm, long-running service.
- A lesson must have a `path:line` source and the output parser looks for ids shaped like `L001` and for `.md`/`.txt` sources (no spaces in paths). Candidates that print something else will score as unmatched.
- Matching by source is exact on file and overlapping line range; two lessons in one file are told apart only by their `match_phrases` appearing in the same output line.
- The bundled data and toy candidate are deliberately tiny and prove nothing about any real trigger. The `claude-hook` payloads are minimal approximations of Claude Code hook inputs and have not been checked against every version.
- The tool measures a trigger against labels that a human wrote; label quality limits what the numbers mean.

Turkish version: [README.tr.md](README.tr.md)

## License

Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
