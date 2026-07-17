# mml_edi/services/wall_format.py
"""Pure shaping helpers for the EDI Wall Display radiator.

No Odoo dependency — plain functions over primitives, so the wall's alarm-tone,
poll-tone and service-level-bar logic is unit-testable without a live database
(``tests/test_wall_format.py``). ``models/edi_wall.py`` is a thin adapter that
reads the ORM and calls these to shape the payload.
"""


def _rag_higher_better(value, target, amber_floor):
    """'green' / 'amber' / 'red' for a higher-is-better metric (None -> 'none').

    Mirrors ``models.edi_dashboard._rag_higher_better`` — duplicated here so this
    module stays free of any Odoo import (the wall bars reuse the dashboard's RAG
    thresholds but must remain pure-testable).
    """
    if value is None:
        return "none"
    if value >= target:
        return "green"
    if value >= amber_floor:
        return "amber"
    return "red"


def alarm_count_tone(count):
    """Alarm tone for a count metric: 'calm' at 0, 'amber' at 1-2, 'red' at >=3.

    Drives the wall tile's glow: a calm (green) tile has nothing outstanding; a
    single outstanding item is amber; three or more is a red radiator alarm.
    """
    if not count:
        return "calm"
    return "red" if count >= 3 else "amber"


def poll_tone(fresh_min):
    """Alarm tone for the poll-freshness metric (whole minutes, or None).

    A radiator should shout when the feed goes cold: green (calm) under 1h, amber
    to 3h, red beyond — and red when a partner has never polled (None). The crons
    poll well inside an hour, so a calm tile is the healthy steady state.
    """
    if fresh_min is None:
        return "red"
    if fresh_min <= 60:
        return "calm"
    return "amber" if fresh_min <= 180 else "red"


def service_bar(label, value, target, amber_floor):
    """One service-level bar: value / target label / clamped width / marker / RAG
    tone (higher-is-better). ``value``/``target`` are percentages (``value`` may
    be None when a KPI has no denominator, rendering '—' with a 0-width fill).

    Width + marker are clamped to the 0-100 track: a 99.2% bar sits just short of
    a 100 marker, and an over-target value (e.g. 100% against a 75 target) never
    overflows the track.
    """
    return {
        "label": label,
        "value": ("%.1f%%" % value) if value is not None else "—",
        "target": "target %d" % int(target),
        "pct": "%.1f%%" % max(0.0, min(100.0, value if value is not None else 0.0)),
        "mark": "%.1f%%" % max(0.0, min(100.0, target)),
        "tone": _rag_higher_better(value, target, amber_floor),
    }
