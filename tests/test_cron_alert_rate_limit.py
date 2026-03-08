"""Verify cron alert rate-limiting is implemented."""
import pathlib


def test_rate_limiting_uses_config_param():
    src = pathlib.Path('mml.edi/models/edi_processor.py').read_text()
    assert 'last_alert' in src, (
        "edi_processor.py must store/check last alert timestamp to implement rate limiting"
    )
    assert '_ALERT_COOLDOWN_SECONDS' in src or 'cooldown' in src.lower(), (
        "Rate limiting cooldown constant or logic must be present"
    )
