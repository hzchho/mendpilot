"""Optional repair tools, automatic test replay and submission control."""
import base64
import json
from pathlib import Path
import shlex
from minisweagent.exceptions import Submitted

class VerificationEnvironment:
    tool_path = "/tmp/mendpilot_tools.py"
    tool_command = "python /tmp/mendpilot_tools.py"
    state_path = "/tmp/mendpilot-state"
    python_command = "python"

    def __init__(self, wrapped, base_commit, root="/testbed", budget_finalize_calls=4,
                 tool_path="/tmp/mendpilot_tools.py", state_path="/tmp/mendpilot-state",
                 python_executable="python"):
        self.wrapped = wrapped
        self.events = []
        self.evidence = {}
        self.root = root
        self.tool_path = tool_path
        self.state_path = state_path
        self.python_command = shlex.quote(python_executable)
        self.tool_command = self.python_command + " " + shlex.quote(tool_path)
        self.last_test_command = None
        self.budget_finalize_calls = budget_finalize_calls
        self.remaining_calls = lambda: None
        encoded = base64.b64encode((Path(__file__).resolve().parents[1] / "tools" / "repair_tools.py").read_bytes()).decode()
        install = (self.python_command + " -c " + shlex.quote(
            "import base64; open(" + repr(tool_path) + ",'wb').write(base64.b64decode('" + encoded + "'))")
                   + " && " + self.tool_command + " init --root " + shlex.quote(root) + " --base " + shlex.quote(base_commit))
        result = self.wrapped.execute({"command": install})
        if result["returncode"]:
            raise RuntimeError("Could not initialize MendPilot tools: " + result["output"][-1000:])
        self.initial_observation = result["output"].strip()

    def collect(self):
        result = self.wrapped.execute({"command": "cat " + shlex.quote(str(Path(self.state_path) / "state.json"))})
        if result["returncode"] == 0:
            self.evidence = json.loads(result["output"])
        return self.evidence

    def get_template_vars(self, **kwargs):
        return self.wrapped.get_template_vars(**kwargs)

    def serialize(self):
        return self.wrapped.serialize()

    def cleanup(self):
        return self.wrapped.cleanup()

    def execute(self, action, **kwargs):
        result = dict(self._execute(action, **kwargs))
        # Every branch, including local budget feedback, follows the environment
        # output contract expected by StrictUndefined observation templates.
        result.setdefault("exception_info", "")
        result.setdefault("returncode", 0)
        result.setdefault("output", "")
        command = action.get("command", "").strip()
        previous = getattr(self, "previous_command", None)
        self.command_repetitions = getattr(self, "command_repetitions", 0) + 1 if previous == command else 1
        self.previous_command = command
        if self.command_repetitions >= 2:
            result["output"] += ("\nREPEATED COMMAND: This exact command was repeated. "
                                 "If its result is unchanged, read a different focused section, "
                                 "check tool --help, or change the repair approach.\n")
            self.events.append({"event": "repeated_command_feedback", "count": self.command_repetitions})
        return result

    def _auto_test_candidate(self, command):
        """Return a recent visible test command that can be replayed safely enough.

        The command still runs inside the task container. ``pipefail`` makes a
        piped test command fail when the test process fails instead of trusting
        the final ``tail`` process.
        """
        if not command or self.tool_path in command:
            return None
        if any(op in command for op in ["git diff", "git status", "git log"]):
            return None
        if not any(token in command for token in ["pytest", "unittest", "runtests.py", "bin/test",
                "/tmp/repro", "/tmp/test", str(Path(self.tool_path).parent / "repro"),
                str(Path(self.tool_path).parent / "test")]):
            return None
        return command

    def _automatic_finalize(self, patch, **kwargs):
        if not patch.strip():
            return {"returncode": 1, "output": "Empty patch; implement the source fix first"}
        diff_check = self.wrapped.execute({"command": "git diff --check"}, **kwargs)
        self.events.append({"event": "automatic_diff_check", **diff_check})
        if diff_check["returncode"] != 0:
            return dict(diff_check, output="AUTOMATIC VERIFICATION FAILED (diff check): " + diff_check["output"])
        if getattr(self, "last_test_command", None):
            command = "set -o pipefail; " + self.last_test_command
            test = self.wrapped.execute({"command": command}, **kwargs)
            self.events.append({"event": "automatic_post_test", "command": self.last_test_command, **test})
            if test["returncode"] != 0:
                return dict(test, output="AUTOMATIC VERIFICATION FAILED (latest test): " + test["output"][-4500:])
            return {"returncode": 0, "output": "automatic diff check and latest test passed"}
        return {"returncode": 0, "output": "automatic diff check passed; no standalone test was recorded"}

    def _execute(self, action, **kwargs):
        command = action.get("command", "").strip()
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = []
        if tokens[:3] == ['cd', self.root, '&&']:
            tokens = tokens[3:]
        automatic = tokens in [shlex.split(self.tool_command) + ['submit'], shlex.split(self.tool_command) + ['submit', '--force']]
        if automatic:
            kwargs.setdefault("timeout", 100)
        remaining = self.remaining_calls()
        if (self.budget_finalize_calls and remaining is not None and remaining <= self.budget_finalize_calls
                and not automatic and self._auto_test_candidate(command) is None
                and not any(token in command for token in ["submit", "git diff", "pytest", "unittest", "runtests.py", "bin/test", "/tmp/repro", "/tmp/test"])):
            return {"returncode": 1,
                    "output": (f"BUDGET FINALIZATION: {remaining} model calls remain. "
                               "Stop exploration. Run one focused test if needed, then use "
                               f"{self.tool_command} submit.")}
        candidate = self._auto_test_candidate(command)
        try:
            result = self.wrapped.execute(action, **kwargs)
        except Submitted as submitted:
            # Retain validation if the model falls back to mini's old submit marker.
            patch = submitted.messages[0].get("extra", {}).get("submission", "")
            automatic = self._automatic_finalize(patch, **kwargs)
            self.events.append({"event": "marker_submission_verification", **automatic})
            if automatic["returncode"] != 0:
                return automatic
            raise
        if candidate and result["returncode"] == 0:
            self.last_test_command = candidate
            self.events.append({"event": "candidate_test_recorded", "command": candidate})
        if automatic:
            self.events.append({"event": "automatic_submission", "returncode": result["returncode"]})
            if result["returncode"] == 0:
                payload = json.loads(result["output"])
                if payload.get("submission_ready") and payload.get("submission", "").strip():
                    verified = self._automatic_finalize(payload["submission"], **kwargs)
                    self.events.append({"event": "automatic_post_verification", **verified})
                    if verified["returncode"] != 0:
                        return verified
                    raise Submitted({"role": "exit", "content": "Submitted", "extra": {
                        "exit_status": "Submitted", "submission": payload["submission"]}})
        elif self.tool_path in command:
            self.events.append({"event": "tool_call", "command": command, "returncode": result["returncode"]})
        remaining = self.remaining_calls()
        if remaining is not None and remaining <= 6:
            checks = "any chosen checks"
            result = dict(result, output=result["output"] + (
                "\nBUDGET: %s model calls remain. Finish the current repair and %s, "
                "then use the automatic submit command. Avoid optional exploration.\n" % (remaining, checks)))
        return result
