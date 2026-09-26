import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ._opentele_compat import patch_opentele_for_new_python

_KEY_FILE_NAMES = ("key_datas", "key_data")


def find_tdata_dirs(root: Path) -> list[Path]:
    """Все папки TData внутри root (папка с файлом ключа key_datas), в порядке имён."""
    dirs = {p.parent for name in _KEY_FILE_NAMES for p in Path(root).rglob(name) if p.is_file()}
    return sorted(dirs, key=lambda p: str(p))


def tdata_available() -> tuple[bool, str]:
    """(готов ли импорт TData, подсказка что делать, если нет). Проверяет, что opentele
    импортируется (с учётом совместимости с новыми версиями Python)."""
    patch_opentele_for_new_python()
    try:
        import opentele.api  # noqa: F401
        import opentele.td  # noqa: F401
    except ImportError as exc:
        return False, (f"не установлен пакет opentele или его зависимость ({exc}). "
                       "Выполните: python scripts/install_tdata_deps.py")
    except Exception as exc:  # noqa: BLE001 — opentele на незнакомой версии Python может падать при импорте
        return False, f"opentele не импортируется на этой версии Python ({type(exc).__name__}: {exc})"
    return True, ""


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


# Понятные тексты для исключений opentele/Telethon (сопоставляем по имени класса —
# не импортируем их заранее, пакет опционален и на разных версиях называется по-разному).
_FRIENDLY = {
    "TDataWrongPasscode": "неверный локальный код-пароль Telegram Desktop",
    "TDataReadMapDataIncorrectPasscode": "неверный локальный код-пароль Telegram Desktop",
    "TDataBadDecryptKey": ("не удалось расшифровать TData — обычно профиль защищён локальным кодом-паролем "
                           "Desktop (укажите его) либо файлы повреждены/неполные"),
    "NoPasswordProvided": "у аккаунта включён облачный пароль (2FA) — укажите его",
    "PasswordIncorrect": "неверный облачный пароль (2FA)",
    "TDesktopUnauthorized": ("сеанс в этой TData уже недействителен (из аккаунта вышли или сеанс завершён) — "
                             "нужна свежая TData"),
    "TDesktopHasNoAccount": "в этой TData нет ни одного аккаунта",
    "TDataAuthKeyNotFound": "в TData нет ключа авторизации — профиль не был залогинен",
    "AccountAuthKeyNotFound": "в TData нет ключа авторизации — профиль не был залогинен",
    "TFileNotFound": "в папке нет нужных файлов TData (нужны key_datas и папка аккаунта)",
    "TDataInvalidMagic": "файлы TData повреждены (неверная сигнатура)",
    "TDataInvalidCheckSum": "файлы TData повреждены (не сходится контрольная сумма)",
    "TDataBadConfigData": "файлы TData повреждены (конфигурация)",
    "AuthKeyDuplicatedError": ("ключ этой TData прямо сейчас используется с другого IP (открыт Telegram Desktop?) — "
                               "закройте Desktop и повторите; Telegram мог аннулировать ключ"),
    "AuthKeyUnregisteredError": "сеанс в этой TData уже недействителен — нужна свежая TData",
    "SessionRevokedError": "сеанс в этой TData завершён владельцем — нужна свежая TData",
    "UserDeactivatedBanError": "аккаунт заблокирован Telegram",
    "UserDeactivatedError": "аккаунт деактивирован",
}


def describe_error(exc: BaseException) -> str:
    """Текст ошибки конвертации для пользователя."""
    name = type(exc).__name__
    if name in _FRIENDLY:
        return _FRIENDLY[name]
    if type(exc) is BaseException and "connect" in str(exc).lower():
        # opentele.QRLoginToNewClient делает raise BaseException("Cannot connect")
        return "не удалось подключиться к Telegram через прокси — проверьте прокси (доступность и что он пропускает Telegram)"
    if name == "FloodWaitError":
        return f"Telegram просит подождать {getattr(exc, 'seconds', '?')} с — повторите позже"
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError, TimeoutError)) or name in (
        "ProxyError", "ProxyConnectionError", "GeneralProxyError", "SOCKS5Error",
    ):
        return f"не удалось подключиться к Telegram через прокси ({name}: {exc}) — проверьте прокси"
    return f"{name}: {exc}"


def tdata_to_session(tdata_dir: Path, out_session_path: Path, proxy: tuple | None = None, *,
                     passcode: str | None = None, cloud_password: str | None = None) -> None:
    """Конвертирует папку TData (Telegram Desktop) в .session файл Telethon.

    proxy — кортеж PySocks (см. proxy.parse_proxy). CreateNewSession делает настоящие
    сетевые подключения к Telegram (один раз старым ключом из TData и один раз новым),
    поэтому БЕЗ прокси они пойдут с реального IP машины, на которой запущена панель —
    у человека за личным VPN это его собственный «реальный» адрес, а именно его и
    видит потом Telegram. При REQUIRE_PROXY=true (по умолчанию) вызов без прокси
    отклоняется.

    passcode — локальный код-пароль Telegram Desktop (если профиль им защищён);
    cloud_password — облачный пароль (2FA) аккаунта, нужен для входа в новый сеанс.

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

    Требует опциональный пакет `opentele` (python scripts/install_tdata_deps.py).
    API этой библиотеки может отличаться между версиями — при ошибках
    сверьтесь с её документацией (https://github.com/thedemons/opentele).
    Любая ошибка бросается как RuntimeError с понятным текстом.
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
            "Для импорта TData не установлен пакет opentele. Выполните: python scripts/install_tdata_deps.py"
        ) from exc

    tdata_dir = _find_real_tdata_dir(Path(tdata_dir))

    async def _convert():
        # opentele сам перебирает суффиксы "s"/"1"/"0" для файла ключа
        # (например "key_data" + "s" = "key_datas", актуальный формат
        # современного Telegram Desktop) — явно указывать keyFile не нужно.
        tdesk = TDesktop(str(tdata_dir), passcode=passcode) if passcode else TDesktop(str(tdata_dir))
        if not tdesk.isLoaded():
            raise RuntimeError(
                "Не удалось прочитать TData — похоже, это не полная папка профиля "
                "Telegram Desktop (нет файла ключа) или профиль защищён локальным "
                "паролем, который не указан."
            )
        # CreateNewSession делает реальный сетевой запрос к Telegram (QR-логин поверх
        # старых данных) — в отличие от UseCurrentSession, где сеанс просто копируется
        # локально. Поэтому конвертация теперь занимает пару секунд, а не мгновенна.
        kwargs = {"proxy": proxy} if proxy else {}
        if cloud_password:
            kwargs["password"] = cloud_password
        client = await tdesk.ToTelethon(session=str(out_session_path), flag=CreateNewSession, **kwargs)
        await client.disconnect()

    # Вызывается из async-обработчика FastAPI, где цикл событий уже запущен, а
    # asyncio.run() в нём запрещён — поэтому гоним конвертацию в отдельном потоке.
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pool.submit(lambda: asyncio.run(_convert())).result()
        except (RuntimeError, KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as exc:  # noqa: BLE001
            # opentele/Telethon внутри могут бросить что угодно — в т.ч. голый
            # BaseException("Cannot connect") (так делает сам opentele), — а вызывающий
            # код ловит только RuntimeError и показывает его текст пользователю; без
            # этой обёртки любая другая ошибка превращалась бы в неинформативный
            # HTTP 500 без единого слова о причине. Ctrl+C/выход/отмена не глотаем.
            raise RuntimeError(describe_error(exc)) from exc
