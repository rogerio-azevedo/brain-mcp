"""One per-call read filter for everything built on the vault-wide index.

The index and watcher stay vault-wide: a per-identity ``read_paths`` /
``deny_read_paths`` override is applied when a tool formats its response, by
filtering the candidate paths it is about to return.  Every index-backed tool
goes through :class:`ReadScope` rather than growing its own filter, so the
traversal and the result of e.g. a backlink or graph query are narrowed by the
exact same rules a direct ``read_note_tool`` call would be checked against.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ..config import get_config
from ..storage.policy import VaultAccessPolicy


class ReadScope:
    """Which vault paths the calling identity may see in a tool's output."""

    def __init__(self, predicate: Callable[[str], bool] | None) -> None:
        self._predicate = predicate

    @classmethod
    def current(cls, config=None, policy: VaultAccessPolicy | None = None) -> ReadScope:
        """Build the scope for the current request (identity + vault)."""
        if policy is None:
            policy = VaultAccessPolicy.from_config(config or get_config())
        effective = policy.effective()
        if not effective.read_paths and not effective.deny_read_paths:
            # Nothing to filter — keep the vault-wide fast path allocation-free.
            return cls(None)
        return cls(policy.permits_read)

    @property
    def unrestricted(self) -> bool:
        return self._predicate is None

    def allows(self, path: str) -> bool:
        return True if self._predicate is None else self._predicate(path)

    def filter(self, paths: Iterable[str]) -> list[str]:
        if self._predicate is None:
            return list(paths)
        return [path for path in paths if self._predicate(path)]
