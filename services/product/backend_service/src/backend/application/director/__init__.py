"""Canonical Director package (backend application logic).

Task 1.11 moved Director ownership here. Module-level decomposition is
Task 1.21; only the prompt bundle and its fixed-file loader/composer exist at
this stage. Legacy ``core.director`` remains a compatibility seam until
callers migrate.
"""

from __future__ import annotations