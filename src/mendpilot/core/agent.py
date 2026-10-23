from minisweagent.agents.default import DefaultAgent

class RepairAgent(DefaultAgent):
    """Format feedback and budget finalization around the upstream loop."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.control_events = []
        self.finalize_attempted = False
        self.finalization_notice = False
        self.format_notices = set()

    def query(self):
        submit_command = getattr(self.env, 'tool_command', 'python /tmp/mendpilot_tools.py') + ' submit'
        if self.n_consecutive_format_errors >= 2 and self.n_calls not in self.format_notices:
            self.format_notices.add(self.n_calls)
            self.add_messages(self.model.format_message(role="user", content=(
                'Format correction: call bash with {"command":"..."}. Use one complete command; '
                'do not reply with prose alone. Read tool --help if arguments failed.')))
            self.control_events.append({"event": "format_correction", "at_call": self.n_calls})
        limit = self.config.step_limit
        if limit > 0 and self.n_calls >= max(0, limit - 5) and not self.finalization_notice:
            self.finalization_notice = True
            self.add_messages(self.model.format_message(role="user", content=(
                "Finalization phase: stop broad exploration. Finish the current source edit, "
                f"run one focused check if feasible, then {submit_command}. "
                "Do not commit. Two calls are reserved for final corrections.")))
            self.control_events.append({"event": "finalization_phase", "at_call": self.n_calls})
        if 0 < limit - self.n_calls <= 2 and not self.finalize_attempted:
            self.finalize_attempted = True
            self.control_events.append({"event": "automatic_budget_submit", "at_call": self.n_calls})
            result = self.env.execute({"command": submit_command})
            self.add_messages(self.model.format_message(role="user", content=(
                "Automatic final submission needs correction:\n" + result.get("output", "")[-4000:])))
        return super().query()
