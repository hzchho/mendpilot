"""Current repair workflow; no historical prompt substitutions."""

REPAIR_PROMPT = """
## Efficient repair and evidence
All work must fit within {{step_limit}} model calls. Batch independent reads/checks in
one bash call. The container has no network: use grep/find if rg is unavailable,
and do not install or download dependencies. Keep reproduction scripts in /tmp.

Optional local tools (python /tmp/mendpilot_tools.py):
  view PATH --start N --lines 100; search 'literal' [PATH]; tests [--path PATH]
  edit: stdin JSON {"path":"source.py", "old":"unique text", "new":"replacement"}
The editor checks unique matches and new syntax errors. Test recommendations are heuristic.
For argument details use python /tmp/mendpilot_tools.py check --help (or another
subcommand's --help). Start with the observed
repository layout and installed commands below; locate unknown paths with git ls-files
or find before opening them. Each inspection should resolve a concrete question;
once a plausible cause is identified, use a small executable check to test it.

1. Inspect the issue and relevant source. Reproduce the user-visible symptom through
   the actual public API/CLI, preserving input types, relative paths, cwd and settings.
   Do not replace the stated behavior with a private-helper hypothesis. Check every
   explicit requirement, including API arguments and any required output wording.
   For path/import bugs, preserve the original relative inputs and search paths;
   adding the fixture directory to sys.path can hide the actual failure. Separate
   explicit requirements from inferred implementation preferences: assertions should
   check observable behavior rather than mandate an undocumented internal layout.
2. Write /tmp/repro.py with meaningful assertions and run it directly when useful. Registration is optional; the runner records a standalone test command for automatic final replay.
3. Make a minimal fix preserving existing behavior, then run the reproduction again.
   Error handling should preserve valid partial results and the library's existing
   diagnostic conventions, rather than discard useful work just to avoid a crash.
4. Run a distinct, focused visible regression check when feasible. Registration is optional.
   Prefer relevant existing tests. If they require unavailable infrastructure, use
   /tmp/regression.py with boundary assertions and state its coverage limitation.
   Checks can be batched in one bash command. After code/script changes, rerun only
   the required affected checks. Do not repeat successful checks on unchanged code
   merely for reassurance. Generated version modules are preserved in clean checkouts.
   When changing default behavior, also check that documented explicit opt-in usage
   still works. Trace visible caller code if the original caller is unavailable,
   and state the approximation instead of assuming a different invocation is equivalent.
5. Briefly review the diff against the issue before submitting. If an issue proposes
   an API spelling, preserve it unless visible repository evidence requires otherwise.
   For new source files use git add -N PATH so the patch includes them. Do not commit.

## Submission
When the fix is ready, use this command alone:
  python /tmp/mendpilot_tools.py submit
The runner automatically checks diff whitespace and replays the latest standalone test before submitting. If no test command was recorded, it still checks the nonempty patch and submits. A failed automatic test is returned for correction. No patch.txt, separate patch-generation call, or second submission
is required. A concrete failure is returned for correction within the same budget.
Self-tests are evidence, not proof of complete correctness. Hidden evaluation is unavailable.
"""

def repair_prompt():
    return REPAIR_PROMPT
