# mml_edi/tests/test_dashboard_js_retry_ack.py
"""Guard test: the dashboard's 'Retry ACK' RPC must report its own failures.

onTriageAction is an async t-on-click handler, so Owl does not await it: a
rejected orm.call escapes through the global unhandledrejection path as a raw
crash dialog, the board is never refreshed and the operator is told nothing.
Every other RPC on this screen, and the identical action in
edi_mobile_triage.js, wraps the call in try/catch and notifies at type
'danger'.

There is no JS test harness in this module, so this reads the source: it holds
the shape in place without pulling in a browser runner.
"""
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DASHBOARD_JS = os.path.join(_ROOT, "static", "src", "js", "edi_dashboard.js")


def _retry_ack_branch():
    """The body of onTriageAction's 'retry_ack' case, up to its break."""
    with open(_DASHBOARD_JS, encoding="utf-8") as handle:
        source = handle.read()
    match = re.search(
        r'case "retry_ack":(.*?)\n\s*case "log":', source, re.DOTALL)
    assert match, "onTriageAction no longer has a retry_ack case"
    return match.group(1)


class TestRetryAckErrorHandling:

    def test_the_rpc_is_wrapped(self):
        branch = _retry_ack_branch()
        assert "try {" in branch
        assert "} catch (" in branch

    def test_a_failure_is_notified_as_danger(self):
        branch = _retry_ack_branch()
        assert 'type: "danger"' in branch

    def test_the_failure_message_carries_the_server_reason(self):
        branch = _retry_ack_branch()
        assert "e.data ? e.data.message : e.message" in branch

    def test_the_success_toast_is_still_there(self):
        branch = _retry_ack_branch()
        assert "Pending ACKs re-queued" in branch
        assert 'type: "success"' in branch

    def test_the_board_is_still_refreshed(self):
        assert "this._load()" in _retry_ack_branch()
