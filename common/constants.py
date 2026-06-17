"""Shared, dependency-light constants used across services.

This module is stdlib-only and reaches into neither ``etl`` nor any
third-party package, so it is safe to import from both the ETL writer image
and the read-only dashboard image (each of which ships ``common/`` but not the
other service's package).

``_MIN_HISTORY_OBS`` is the IV-rank/percentile accrual threshold: the minimum
number of stored ``atm_iv`` snapshots before ``etl/sources/iv.py`` computes a
non-NULL ``iv_rank`` / ``iv_percentile``, and equivalently the denominator of
Panel D's cold-start ``N/20`` accruing label. Both the ETL writer and the
Panel D reader consume this single definition so they stay in lockstep.
"""

_MIN_HISTORY_OBS = 20
