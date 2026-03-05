try:
    from . import models
    from . import parsers
    from . import wizards
    from . import hooks
except ImportError:
    # Standalone execution (e.g. pytest without Odoo runtime).
    # Relative imports require a parent package; outside Odoo they are a no-op.
    pass
