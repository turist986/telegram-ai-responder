import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ._opentele_compat import patch_opentele_for_new_python

_KEY_FILE_NAMES = ("key_datas", "key_data")


def _find_real_tdata_dir(extracted_dir: Path) -> Path:
    """Находит настоящую папку TData внутри распакованного архива.

    Если пользователь зазиповал саму папку `tdata` (обычный способ через
    «Отправить в архив» в проводнике), в архиве уже есть свой уровень
    `tdata/...` — а мы распаковываем его ещё в одну папку `tdata/`,
    получая двойную вложенность (`.../tdata/tdata/key_datas`). Ищем
    файл ключа на текущем уровне и на один уровень вложенности вниз,
    чтобы оба варианта архива (с обёрткой и без) сработали одинаково.
    """
    if any((extracted_dir / name).exists() for name in _KEY_FILE_NAMES):
        return extracted_dir

    for child in extracted_dir.iterdir():
        if child.is_dir() and any((child / name).exists() for name in _KEY_FILE_NAMES):
            return child

    return extracted_dir  # не нашли — вернём как есть, пусть TDesktop сам сообщит об ошибке


def tdata_to_session(tdata_dir: Path, out_session_path: Path) -> None:
    """Конвертирует папку TData (Telegram Desktop) в .session файл Telethon.

    Требует опциональный пакет `opentele` (pip install -r requirements-tdata.txt).
    API этой библиотеки может отличаться между версиями — при ошибках
    сверьтесь с её документацией (https://github.com/thedemons/opentele).
    """
    patch_opentele_for_new_python()  # см. _opentele_compat.py — совместимость с Python 3.13+

    try:
        from opentele.api import UseCurrentSession
        from opentele.td import TDesktop
    except ImportError as exc:
        raise RuntimeError(
            "Для импорта TData установите пакет 'opentele' (pip install -r requirements-tdata.txt)"
        ) from exc

    tdata_dir = _find_real_tdata_dir(Path(tdata_dir))

    async def _convert():
        # opentele сам перебирает суффиксы "s"/"1"/"0" для файла ключа
        # (например "key_data" + "s" = "key_datas", актуальный формат
        # современного Telegram Desktop) — явно указывать keyFile не нужно.
        tdesk = TDesktop(str(tdata_dir))
        if not tdesk.isLoaded():
            raise RuntimeError(
                "Не удалось прочитать TData — похоже, это не полная папка профиля "
                "Telegram Desktop (нет файла ключа) или профиль защищён локальным "
                "паролем, который здесь не передан."
            )
        client = await tdesk.ToTelethon(session=str(out_session_path), flag=UseCurrentSession)
        await client.disconnect()

    # Вызывается из async-обработчика FastAPI, где цикл событий уже запущен, а
    # asyncio.run() в нём запрещён — поэтому гоним конвертацию в отдельном потоке.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: asyncio.run(_convert())).result()
