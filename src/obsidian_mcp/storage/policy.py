"""Canonical vault paths and the central filesystem authorization policy.

The MCP tools intentionally deal in vault-relative strings.  This module is
the only place where those strings become filesystem paths.  Keeping that
conversion here prevents a tool from validating one path and subsequently
constructing a different (or symlinked) path for the actual operation.
"""

from __future__ import annotations

import os
import posixpath
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


class VaultPathError(ValueError):
    """The supplied path is not a safe vault-relative path."""


class ReadPermissionError(PermissionError):
    """A path is outside the readable portion of the vault."""


class WritePermissionError(PermissionError):
    """A path is outside the writable portion of the vault."""


class ProtectedPathError(WritePermissionError):
    """A security-sensitive path cannot be modified by MCP."""


class PermanentDeleteDisabledError(WritePermissionError):
    """Permanent deletion is disabled by configuration."""


class InvalidFileTypeError(ValueError):
    """A tool-specific writer was given the wrong file extension."""


class PolicyMergeError(ValueError):
    """An identity override would widen a vault's policy instead of narrowing it."""


@dataclass(frozen=True)
class VaultPath:
    """An authorized, immutable vault-relative path and its absolute target."""

    relative: str
    absolute: Path
    stat_result: os.stat_result | None = None


def matches_path_rule(path: str, rule: str, *, casefold: bool = False) -> bool:
    """Match one canonical path against an exact or recursive rooted rule."""
    recursive = rule.endswith("/")
    candidate = path.casefold() if casefold else path
    scope = rule.rstrip("/")
    scope = scope.casefold() if casefold else scope
    return candidate == scope or (
        recursive and candidate.startswith(scope + "/")
    )


def _normalise_relative(value: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise VaultPathError("Vault path must be a string")
    if "\x00" in value:
        raise VaultPathError("Vault path contains a NUL byte")
    # MCP clients on Windows commonly send backslash paths even when the
    # server runs on Linux.  Treat both separators as path separators.
    value = value.replace("\\", "/")
    if value.startswith("/") or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
        raise VaultPathError("Absolute vault paths are not allowed")
    normalised = posixpath.normpath(value)
    if normalised == ".":
        normalised = ""
    if not allow_empty and not normalised:
        raise VaultPathError("A vault file or child directory path is required")
    if normalised == ".." or normalised.startswith("../"):
        raise VaultPathError(f"Path escapes vault root: {value!r}")
    return normalised


def normalise_path_rules(values: Iterable[str], *, name: str) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise VaultPathError(f"{name} entries must be strings")
        recursive = raw.endswith(("/", "\\"))
        item = _normalise_relative(raw.rstrip("/\\"), allow_empty=False)
        if recursive:
            item += "/"
        if item not in result:
            result.append(item)
    return tuple(result)


def rule_covers_rule(allow_rule: str, candidate: str) -> bool:
    """Is every path reachable through ``candidate`` also inside ``allow_rule``?

    Component-aware, reusing the same rooted-rule matcher the runtime checks
    use: a recursive candidate (``a/b/``) needs a recursive covering rule,
    while an exact-file candidate only needs to match it.
    """
    path = candidate.rstrip("/")
    if candidate.endswith("/") and not allow_rule.endswith("/"):
        return False
    return matches_path_rule(path, allow_rule)


def merge_allowlist(
    vault_rules: tuple[str, ...],
    override_rules: tuple[str, ...] | None,
    *,
    name: str = "paths",
) -> tuple[str, ...]:
    """Intersect an allowlist with an identity override (``None`` inherits).

    An empty vault allowlist means "the whole vault", so the override simply
    becomes the effective scope.  Otherwise every override rule has to resolve
    inside the vault's own scope; anything else would widen access and is
    rejected rather than silently narrowed.
    """
    if override_rules is None:
        return tuple(vault_rules)
    override = tuple(override_rules)
    if not override:
        return tuple(vault_rules)
    if not vault_rules:
        return override
    for rule in override:
        if not any(rule_covers_rule(allowed, rule) for allowed in vault_rules):
            raise PolicyMergeError(
                f"{name} entry {rule!r} is not inside the vault's own "
                f"{name} ({', '.join(vault_rules)})"
            )
    return override


def merge_denylist(
    vault_rules: tuple[str, ...],
    override_rules: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Union a denylist with an identity override — a deny can only be added."""
    result = list(vault_rules)
    for rule in override_rules or ():
        if rule not in result:
            result.append(rule)
    return tuple(result)


def merge_readonly(vault_value: bool, override_value: bool | None) -> bool:
    """OR toward ``True`` — an identity may force read-only, never undo it."""
    return bool(vault_value) or bool(override_value)


@dataclass(frozen=True)
class EffectiveAccessPolicy:
    """The five path-policy fields after merging one identity override in."""

    read_only: bool
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    deny_read_paths: tuple[str, ...]
    deny_write_paths: tuple[str, ...]

    def merged_with(self, override) -> EffectiveAccessPolicy:
        """Apply one ``config.PolicyOverride``, restricting only."""
        if override is None:
            return self
        return EffectiveAccessPolicy(
            read_only=merge_readonly(self.read_only, override.read_only),
            read_paths=merge_allowlist(
                self.read_paths, override.read_paths, name="read_paths"
            ),
            write_paths=merge_allowlist(
                self.write_paths, override.write_paths, name="write_paths"
            ),
            deny_read_paths=merge_denylist(
                self.deny_read_paths, override.deny_read_paths
            ),
            deny_write_paths=merge_denylist(
                self.deny_write_paths, override.deny_write_paths
            ),
        )


class _Inherit:
    """Sentinel: take the identity from the current request context."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<inherit identity>"


INHERIT_IDENTITY = _Inherit()


class VaultAccessPolicy:
    """Resolve and authorize all MCP paths against one vault root."""

    def __init__(
        self,
        vault_root: str | Path,
        *,
        read_only: bool = False,
        read_paths: Iterable[str] = (),
        write_paths: Iterable[str] = (),
        deny_read_paths: Iterable[str] = (),
        deny_write_paths: Iterable[str] = (),
        allow_permanent_delete: bool = False,
        vault_name: str | None = None,
        apply_identity_overrides: bool = True,
    ) -> None:
        root = Path(vault_root)
        if not root.exists() or not root.is_dir():
            raise VaultPathError(f"Vault root does not exist or is not a directory: {root}")
        self.root = root.resolve()
        self.allow_permanent_delete = bool(allow_permanent_delete)
        self.vault_name = vault_name
        # The vault-wide index and watcher deliberately opt out: they are
        # shared by every identity and must stay identity-agnostic.
        self.apply_identity_overrides = bool(apply_identity_overrides)
        self._base = EffectiveAccessPolicy(
            read_only=bool(read_only),
            read_paths=normalise_path_rules(read_paths, name="READ_PATHS"),
            write_paths=normalise_path_rules(write_paths, name="WRITE_PATHS"),
            deny_read_paths=normalise_path_rules(deny_read_paths, name="DENY_READ_PATHS"),
            deny_write_paths=normalise_path_rules(deny_write_paths, name="DENY_WRITE_PATHS"),
        )
        self._merged: dict[object, EffectiveAccessPolicy] = {}

    @classmethod
    def from_config(cls, config, *, apply_identity_overrides: bool = True) -> VaultAccessPolicy:
        try:
            vault_name = config.resolve_vault_name()
        except AttributeError:  # pragma: no cover - duck-typed test configs
            vault_name = None
        return cls(
            config.vault_path,
            read_only=config.read_only,
            read_paths=getattr(config, "read_paths", ()),
            write_paths=config.write_paths,
            deny_read_paths=config.deny_read_paths,
            deny_write_paths=config.deny_write_paths,
            allow_permanent_delete=config.allow_permanent_delete,
            vault_name=vault_name,
            apply_identity_overrides=apply_identity_overrides,
        )

    # ── Effective (identity-merged) policy ──────────────────────────────

    def _override_for(self, identity: object) -> object | None:
        if identity is INHERIT_IDENTITY:
            if not self.apply_identity_overrides or self.vault_name is None:
                return None
            from ..config import current_identity

            identity = current_identity()
        if identity is None or self.vault_name is None:
            return None
        lookup = getattr(identity, "override_for", None)
        return lookup(self.vault_name) if lookup is not None else None

    def effective(self, identity: object = INHERIT_IDENTITY) -> EffectiveAccessPolicy:
        """Merge the vault policy with one identity override, per call.

        Nothing is precomputed per (identity, vault) pair: the five merges are
        cheap and a re-read of ``vaults.json`` therefore needs no cache to be
        restructured.
        """
        override = self._override_for(identity)
        if override is None:
            return self._base
        cached = self._merged.get(override)
        if cached is None:
            cached = self._base.merged_with(override)
            self._merged[override] = cached
        return cached

    @property
    def read_only(self) -> bool:
        return self.effective().read_only

    @property
    def read_paths(self) -> tuple[str, ...]:
        return self.effective().read_paths

    @property
    def write_paths(self) -> tuple[str, ...]:
        return self.effective().write_paths

    @property
    def deny_read_paths(self) -> tuple[str, ...]:
        return self.effective().deny_read_paths

    @property
    def deny_write_paths(self) -> tuple[str, ...]:
        return self.effective().deny_write_paths

    def canonicalize(self, path: str, *, allow_empty: bool = True) -> VaultPath:
        relative = _normalise_relative(path, allow_empty=allow_empty)
        lexical = self.root / relative if relative else self.root

        # Do not follow a symlink supplied by an MCP caller.  This checks every
        # existing component, including a parent of a not-yet-created file.
        current = self.root
        for component in Path(relative).parts:
            current = current / component
            if current.is_symlink():
                raise VaultPathError(f"Symlink path components are not allowed: {path!r}")

        try:
            absolute = lexical.resolve(strict=False)
        except OSError as exc:
            raise VaultPathError(f"Unable to resolve vault path: {path!r}") from exc
        if not absolute.is_relative_to(self.root):
            raise VaultPathError(f"Path escapes vault root: {path!r}")
        # ``relative`` is derived from the normalized lexical path, not from a
        # potentially symlink-resolved target, so callers get a stable key.
        return VaultPath(relative=relative, absolute=absolute)

    @staticmethod
    def rule_path(rule: str) -> str:
        """Return the canonical path portion of a configured rule."""
        return rule.rstrip("/")

    @staticmethod
    def _matches(path: str, rule: str, *, casefold: bool = False) -> bool:
        """Match exact file rules and slash-suffixed recursive directory rules."""
        return matches_path_rule(path, rule, casefold=casefold)

    def authorize_discovered_read(
        self,
        path: str,
        info: os.stat_result,
        *,
        identity: object = INHERIT_IDENTITY,
    ) -> VaultPath:
        """Authorize a no-follow descriptor discovery without rewalking it.

        Callers must obtain ``path`` and ``info`` from the descriptor-relative
        scanner. Unlike ``resolve_read``, this method deliberately performs no
        second path lookup that could race or duplicate O(depth) syscalls.
        """
        relative = _normalise_relative(path, allow_empty=False)
        if relative != path:
            raise VaultPathError(f"Non-canonical discovered path: {path!r}")
        policy = self.effective(identity)
        if policy.read_paths and not self._read_scope_allows(
            relative, policy.read_paths, allow_ancestor=stat.S_ISDIR(info.st_mode)
        ):
            raise ReadPermissionError(f"Read access denied for path {relative!r}")
        if self._denied(relative, policy.deny_read_paths):
            raise ReadPermissionError(f"Read access denied for path {relative!r}")
        return VaultPath(
            relative=relative,
            absolute=self.root / relative,
            stat_result=info,
        )

    def _denied(self, path: str, rules: tuple[str, ...]) -> str | None:
        # The longest matching rule gives a useful deterministic reason in logs.
        # Case-fold deny rules even on a case-sensitive host. This can only
        # deny additional paths and prevents case aliases bypassing policy on
        # the common case-insensitive macOS/Windows filesystems.
        matches = [rule for rule in rules if self._matches(path, rule, casefold=True)]
        return max(matches, key=len) if matches else None

    def _read_scope_allows(
        self,
        path: str,
        read_paths: tuple[str, ...],
        *,
        allow_ancestor: bool = False,
    ) -> bool:
        for rule in read_paths:
            if self._matches(path, rule):
                return True
            scope = self.rule_path(rule)
            if allow_ancestor and (not path or scope.startswith(path + "/")):
                return True
        return False

    def permits_read(
        self,
        relative: str,
        *,
        allow_ancestor: bool = False,
        identity: object = INHERIT_IDENTITY,
    ) -> bool:
        """Rule-only read check for an already-canonical vault-relative path.

        Touches no filesystem, so it is the cheap gate for filtering the many
        candidate paths an index-backed tool is about to return.
        """
        path = relative.replace("\\", "/").strip("/")
        policy = self.effective(identity)
        if policy.read_paths and not self._read_scope_allows(
            path, policy.read_paths, allow_ancestor=allow_ancestor
        ):
            return False
        return not self._denied(path, policy.deny_read_paths)

    def resolve_read(
        self,
        path: str,
        *,
        allow_empty: bool = False,
        identity: object = INHERIT_IDENTITY,
    ) -> VaultPath:
        result = self.canonicalize(path, allow_empty=allow_empty)
        policy = self.effective(identity)
        if policy.read_paths and not self._read_scope_allows(
            result.relative, policy.read_paths, allow_ancestor=allow_empty
        ):
            raise ReadPermissionError(f"Read access denied for path {result.relative!r}")
        if self._denied(result.relative, policy.deny_read_paths):
            raise ReadPermissionError(f"Read access denied for path {result.relative!r}")
        return result

    def resolve_write(
        self,
        path: str,
        *,
        allow_empty: bool = False,
        identity: object = INHERIT_IDENTITY,
    ) -> VaultPath:
        result = self.canonicalize(path, allow_empty=allow_empty)
        policy = self.effective(identity)
        if policy.read_only:
            raise WritePermissionError("Server is in read-only mode")
        if policy.write_paths and not any(
            self._matches(result.relative, rule) for rule in policy.write_paths
        ):
            raise WritePermissionError(f"Write access denied for path {result.relative!r}")
        if self._denied(result.relative, policy.deny_write_paths):
            raise ProtectedPathError(f"Write access denied for protected path {result.relative!r}")
        return result

    def resolve_delete(
        self,
        path: str,
        *,
        permanent: bool = False,
        identity: object = INHERIT_IDENTITY,
    ) -> VaultPath:
        result = self.resolve_write(path, allow_empty=True, identity=identity)
        if result.relative == "":
            raise ProtectedPathError("The vault root cannot be deleted")
        if permanent and not self.allow_permanent_delete:
            raise PermanentDeleteDisabledError("Permanent deletion is disabled")
        return result

    def can_read(self, path: str, *, identity: object = INHERIT_IDENTITY) -> bool:
        try:
            self.resolve_read(path, identity=identity)
            return True
        except (VaultPathError, ReadPermissionError):
            return False

    def can_write(self, path: str, *, identity: object = INHERIT_IDENTITY) -> bool:
        try:
            self.resolve_write(path, identity=identity)
            return True
        except (VaultPathError, WritePermissionError):
            return False


def path_rules_from_env(raw: str, *, name: str) -> list[str]:
    """Parse a comma-separated path setting and validate it immediately."""
    values = [part.strip() for part in raw.split(",") if part.strip()]
    return list(normalise_path_rules(values, name=name))
