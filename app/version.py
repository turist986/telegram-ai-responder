"""Версия кода = короткий хеш git-коммита. Читается из .git напрямую (без вызова git), один раз
при старте процесса — поэтому в панели видно, какой именно код РАБОТАЕТ, а не какой лежит на диске:
если после обновления цифры старые, сайт не перезапущен."""
from functools import lru_cache
from pathlib import Path

from .config import BASE_DIR


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _resolve(git_dir: Path) -> str:
    head = _read(git_dir / "HEAD")
    if not head.startswith("ref:"):
        return head                                   # detached HEAD: сам хеш
    ref = head.split(":", 1)[1].strip()
    ref_file = git_dir / ref
    if ref_file.is_file():
        return _read(ref_file)
    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in _read(packed).splitlines():
            if line.endswith(" " + ref) and not line.startswith(("#", "^")):
                return line.split()[0]
    return ""


@lru_cache(maxsize=1)
def get_version() -> str:
    try:
        git_dir = BASE_DIR / ".git"
        if git_dir.is_file():                         # worktree: «gitdir: <путь>»
            git_dir = Path(_read(git_dir).split(":", 1)[1].strip())
        sha = _resolve(git_dir)
        return sha[:7] if sha else "н/д"
    except Exception:
        return "н/д"
