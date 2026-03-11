"""Verify cron alert emails escape HTML in the body."""
import pathlib


def test_alert_body_uses_html_escape():
    src = (pathlib.Path(__file__).parent.parent / 'models/edi_processor.py').read_text()
    assert 'html.escape' in src or 'markupsafe' in src.lower(), (
        "edi_processor.py must escape body content before embedding in body_html. "
        "Use html.escape(body) or markupsafe.escape(body)."
    )
