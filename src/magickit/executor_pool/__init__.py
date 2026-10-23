"""Executor pool, Magickit side (ADR-2026-10-09-23 unit 3).

Dynamic executor reports, turn requests with thread occupancy, and
executor reservations. Nothing here is imported by the app unless
``executor_pool_enabled`` is true (INV-0); see ``api.py``.
"""
