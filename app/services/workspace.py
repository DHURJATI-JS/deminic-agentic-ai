"""Per-user code workspace on disk (sandboxed path resolution)."""
from __future__ import annotations

from pathlib import Path

from app.config import settings


class PathError(ValueError):
    pass


def user_root(user_id: int) -> Path:
    root = settings.workspaces_dir / f"u{int(user_id)}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve(user_id: int, rel: str) -> Path:
    """Resolve a workspace-relative path; refuses traversal / absolute paths."""
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    if not rel or rel in (".", ".."):
        return user_root(user_id)
    if any(part in ("..",) for part in rel.split("/")) or ":" in rel:
        raise PathError("path escapes the workspace")
    root = user_root(user_id).resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise PathError("path escapes the workspace")
    return target


def rel_of(user_id: int, path: Path) -> str:
    return str(path.resolve().relative_to(user_root(user_id).resolve())).replace("\\", "/")
