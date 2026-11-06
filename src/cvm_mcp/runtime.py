"""Process-wide singletons: one HTTP client and one store per server process."""

from __future__ import annotations

from .fetch import Client
from .store import Store

client = Client()
store = Store(client)
