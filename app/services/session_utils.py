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


def tdata_to_session(tdata_dir: Path, out_session_path: Path, proxy: tuple | None = None) -> None:
    """Конвертирует папку TData (Telegram Desktop) в .session файл Telethon.

    proxy — кортеж PySocks (см. proxy.parse_proxy). CreateNewSession делает настоящие
    сетевые подключения к Telegram (один раз старым ключом из TData и один раз новым),
    поэтому БЕЗ прокси они пойдут с реального IP машины, на которой запущена панель —
    у человека за личным VPN это его собственный «реальный» адрес, а именно его и
    видит потом Telegram. При REQUIRE_PROXY=true (по умолчанию) вызов без прокси
    отклоняется.

    Использует opentele CreateNewSession, а НЕ UseCurrentSession: результат — новый,
    отдельный сеанс (свой auth_key), который появляется как собственная запись в
    списке активных сеансов Telegram, не деля сеанс с самим Telegram Desktop.
    UseCurrentSession в буквальном смысле копирует auth_key живого Desktop-клиента —
    тогда любая активность самого Desktop (в т.ч. с другого IP/VPN, если владелец
    зашёл туда напрямую) отражается на ТОЙ ЖЕ записи сеанса, которую использует наш
    воркер, и наоборот: воркер через прокси и Desktop через VPN выглядят как одно и
    то же место, которое постоянно телепортируется — именно так Telegram и
    обнаруживает подозрительную активность. CreateNewSession — это настоящий (хотя и
    автоматический) вход поверх данных из TData, создающий отдельную запись
    устройства, никак не связанную с уже открытыми сеансами Desktop.

    Требует опциональный пакет `opentele` (pip install -r requirements-tdata.txt).
    API этой библиотеки может отличаться между версиями — при ошибках
    сверьтесь с её документацией (https://github.com/thedemons/opentele).
    """
    from ..config import settings

    if proxy is None and settings.require_proxy:
        raise RuntimeError(
            "Для импорта TData нужен прокси аккаунта: подключение к Telegram при конвертации иначе "
            "пошло бы с реального IP этой машины. Задайте прокси (или отключите REQUIRE_PROXY — не рекомендуется)."
        )

    patch_opentele_for_new_python()  # см. _opentele_compat.py — совместимость с Python 3.13+

    try:
        from opentele.api import CreateNewSession
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
        # CreateNewSession делает реальный сетевой запрос к Telegram (QR-логин поверх
        # старых данных) — в отличие от UseCurrentSession, где сеанс просто копируется
        # локально. Поэтому конвертация теперь занимает пару секунд, а не мгновенна.
        kwargs = {"proxy": proxy} if proxy else {}
        client = await tdesk.ToTelethon(session=str(out_session_path), flag=CreateNewSession, **kwargs)
        await client.disconnect()

    # Вызывается из async-обработчика FastAPI, где цикл событий уже запущен, а
    # asyncio.run() в нём запрещён — поэтому гоним конвертацию в отдельном потоке.
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pool.submit(lambda: asyncio.run(_convert())).result()
        except RuntimeError:
            raise
        except Exception as exc:
            # opentele/Telethon внутри могут бросить что угодно (PyQt5-, Qt- и
            # protobuf-специфичные исключения, не только RuntimeError) — вызывающий
            # код (accounts.py) ловит только RuntimeError и показывает текст
            # пользователю; без этой обёртки любая другая ошибка превращалась бы в
            # неинформативный HTTP 500 без единого слова о причине.
            raise RuntimeError(f"Не удалось сконвертировать TData ({type(exc).__name__}): {exc}") from exc
