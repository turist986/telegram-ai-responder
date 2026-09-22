import threading
from pathlib import Path

_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}


def _read_cached(path: Path) -> str:
    path = Path(path)
    if not path.exists():
        return ""
    mtime = path.stat().st_mtime
    with _lock:
        cached = _cache.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]
    text = path.read_text(encoding="utf-8")
    with _lock:
        _cache[str(path)] = (mtime, text)
    return text


def load_knowledge_base(path: Path) -> str:
    return _read_cached(path)


def load_prompt_template(path: Path) -> str:
    return _read_cached(path)


def save_text_file(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
