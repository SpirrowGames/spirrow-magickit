"""Adapters for external services."""

from magickit.adapters.base import BaseAdapter
from magickit.adapters.cognilens import CognilensAdapter
from magickit.adapters.lexora import LexoraAdapter
from magickit.adapters.prismind import PrismindAdapter

__all__ = [
    "BaseAdapter",
    "CognilensAdapter",
    "LexoraAdapter",
    "PrismindAdapter",
]
