"""Распаковка архивов с TData (.zip / .rar) для пакетного импорта.

Что делаем и зачем:
  * формат определяем по содержимому (сигнатуре), а не по расширению — переименованный
    архив или «.zip», внутри которого на самом деле rar, не должны ломать импорт;
  * распаковываем сами, поэлементно, а не extractall(): проверяем каждый путь (нет
    абсолютных путей, «..», дисков — иначе архив мог бы записать файл за пределы папки),
    считаем реальный размер (а не заявленный в заголовке — от zip-бомб) и пропускаем то,
    что для импорта не нужно;
  * пропускаем кеш профиля Desktop (user_data, emoji, словари…): он бывает на сотни
    мегабайт, а для конвертации нужны только несколько маленьких файлов ключей и карты.

RAR: пакет rarfile сам читает заголовки, а сжатое содержимое распаковывает внешняя
программа (unrar / unar / 7-Zip / bsdtar). В Windows 10+ есть встроенный tar.exe
(это bsdtar), и мы подключаем его сами, если других программ нет."""
import re
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable


class ArchiveError(RuntimeError):
    """Ошибка с текстом, понятным пользователю."""


# Каталоги профиля Telegram Desktop, не нужные для конвертации (только раздувают архив).
SKIP_DIRS = {"user_data", "emoji", "dictionaries", "temp", "tdummy", "cache", "media_cache", "webview"}
MAX_TOTAL_BYTES = 1_500_000_000   # суммарно распакованного (после пропуска кеша)
MAX_FILE_BYTES = 64_000_000       # один файл крупнее — это кеш/медиа, для импорта не нужен
MAX_FILES = 20_000


def detect_kind(path: Path) -> str | None:
    with Path(path).open("rb") as f:
        head = f.read(8)
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    return None


def _safe_relative(name: str) -> PurePosixPath | None:
    """None — запись-каталог (пропускаем). Опасный путь — ошибка целиком: такой архив
    не нужно распаковывать вообще, а не «вычищать» по кускам."""
    norm = name.replace("\\", "/")
    if norm.endswith("/"):
        return None
    p = PurePosixPath(norm)
    if p.is_absolute() or re.match(r"^[A-Za-z]:", norm) or ".." in p.parts:
        raise ArchiveError(f"Небезопасный путь внутри архива: «{name}» — архив отклонён")
    return p


def _copy_limited(src: BinaryIO, dst: BinaryIO, limit: int, name: str) -> int:
    written = 0
    while True:
        chunk = src.read(1 << 16)
        if not chunk:
            return written
        written += len(chunk)
        if written > limit:
            raise ArchiveError(f"Файл «{name}» в архиве оказался больше заявленного — архив отклонён")
        dst.write(chunk)


def _extract(members: Iterable[tuple[str, int]], opener: Callable[[str], BinaryIO], dest: Path) -> int:
    total = files = 0
    for name, declared in members:
        rel = _safe_relative(name)
        if rel is None:
            continue
        if any(part.lower() in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if declared > MAX_FILE_BYTES:
            continue
        files += 1
        total += declared
        if files > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise ArchiveError("Архив слишком большой даже без кеша профиля — загрузите его по частям")
        target = dest.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with opener(name) as src, target.open("wb") as out:
            _copy_limited(src, out, MAX_FILE_BYTES, name)
    return files


def _extract_zip(src: Path, dest: Path, password: str | None) -> int:
    try:
        with zipfile.ZipFile(src) as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))
            members = [(i.filename, i.file_size) for i in zf.infolist() if not i.is_dir()]
            return _extract(members, lambda n: zf.open(n), dest)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"Архив повреждён или это не .zip: {exc}") from exc
    except NotImplementedError as exc:
        raise ArchiveError("Этот zip зашифрован методом AES — такое не поддерживается. "
                           "Запакуйте без пароля или в .rar") from exc
    except RuntimeError as exc:
        if "password" in str(exc).lower():
            raise ArchiveError("Архив защищён паролем — укажите пароль архива (или он неверный)") from exc
        raise ArchiveError(f"Не удалось распаковать zip: {exc}") from exc


def _configure_rar_tools(rarfile) -> None:
    """Подключаем встроенный tar.exe (bsdtar) Windows, если нет привычного имени bsdtar."""
    if shutil.which("bsdtar"):
        return
    tar = shutil.which("tar")
    if not tar:
        return
    try:
        out = subprocess.run([tar, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return
    if "bsdtar" in out.lower() or "libarchive" in out.lower():
        rarfile.BSDTAR_TOOL = tar


_RAR_TOOL_HINT = (
    "Для сжатых .rar нужна программа распаковки: 7-Zip (Windows: winget install 7zip.7zip), UnRAR или bsdtar. "
    "Либо запакуйте профили в .zip — он работает без дополнительных программ"
)


def _extract_rar(src: Path, dest: Path, password: str | None) -> int:
    try:
        import rarfile
    except ImportError as exc:
        raise ArchiveError("Для .rar не установлен пакет rarfile: pip install -r requirements.txt") from exc
    _configure_rar_tools(rarfile)
    try:
        with rarfile.RarFile(str(src)) as rf:
            if password:
                rf.setpassword(password)
            members = [(i.filename, i.file_size) for i in rf.infolist() if not i.is_dir()]
            return _extract(members, lambda n: rf.open(n), dest)
    except rarfile.RarCannotExec as exc:
        raise ArchiveError(_RAR_TOOL_HINT) from exc
    except (rarfile.PasswordRequired, rarfile.RarWrongPassword) as exc:
        raise ArchiveError("Архив защищён паролем — укажите пароль архива (или он неверный)") from exc
    except rarfile.NeedFirstVolume as exc:
        raise ArchiveError("Это не первая часть многотомного архива — загрузите первую часть") from exc
    except rarfile.Error as exc:
        detail = str(exc)
        if "tool" in detail.lower() or "exec" in detail.lower():
            raise ArchiveError(_RAR_TOOL_HINT) from exc
        raise ArchiveError(f"Не удалось распаковать rar: {detail}") from exc


def extract_tdata_archive(src: Path, dest: Path, password: str | None = None) -> int:
    """Распаковывает .zip/.rar в dest (только нужные для TData файлы). Возвращает число
    распакованных файлов. Бросает ArchiveError."""
    kind = detect_kind(src)
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        return _extract_zip(src, dest, password)
    if kind == "rar":
        return _extract_rar(src, dest, password)
    raise ArchiveError("Файл не похож на .zip или .rar (7z и прочие форматы не поддерживаются)")
