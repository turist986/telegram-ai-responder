"""Точечная совместимость opentele<=1.15.1 с окружениями без сборки C-модулей.

Два независимых патча, оба применяются только если реальной установленной
альтернативы нет — апстрим-фикс или обычный `pip install tgcrypto` в среде
с компилятором делают эти патчи пустой операцией:

1. opentele.utils.extend_class() сравнивает атрибуты декорируемого класса с
   атрибутами родителя и считает любое совпадение имени "конфликтом", если
   оно не в жёстко зашитом списке безопасных ("__module__", "__doc__" и
   т.п.). Python 3.13 добавил новые автоматические атрибуты класса
   (__firstlineno__, __static_attributes__), которых в этом списке нет —
   opentele падает на самом импорте (`opentele.tl.telethon`), считая их
   настоящим конфликтом. Патч удаляет эти два атрибута перед сравнением.

2. opentele на этапе импорта (`opentele/td/storage.py`) требует пакет
   `tgcrypto` — C-расширение, которое собирается из исходников и требует
   компилятор (недоступно на части машин, включая эту). Ему нужны только
   две функции — `ige256_encrypt`/`ige256_decrypt` (AES-256 в режиме IGE,
   фирменном режиме Telegram). У Telethon уже есть чистый Python-fallback
   именно для этого режима (`telethon.crypto.aes.AES`, через `pyaes`,
   используется, когда нет ускорителя `cryptg`) — подставляем поддельный
   модуль `tgcrypto`, который делегирует эти два вызова в готовую
   реализацию Telethon. Медленнее нативного расширения, но корректно и не
   требует компилятора.
"""
import importlib.util
import sys
import types
from pathlib import Path

_LEAKY_CLASS_DUNDERS = ("__firstlineno__", "__static_attributes__")

_patched = False


def _install_tgcrypto_shim() -> None:
    if "tgcrypto" in sys.modules:
        return
    if importlib.util.find_spec("tgcrypto") is not None:
        return  # настоящий пакет доступен — используем его, шим не нужен

    from telethon.crypto.aes import AES as _TelethonAES

    # opentele передаёт сюда PyQt5 QByteArray, а не обычные bytes — чистая
    # Python-реализация Telethon рассчитана на настоящие bytes (иначе
    # побайтовые XOR ломаются с TypeError), поэтому явно приводим типы.
    shim = types.ModuleType("tgcrypto")
    shim.ige256_encrypt = lambda data, key, iv: _TelethonAES.encrypt_ige(bytes(data), bytes(key), bytes(iv))
    shim.ige256_decrypt = lambda data, key, iv: _TelethonAES.decrypt_ige(bytes(data), bytes(key), bytes(iv))
    sys.modules["tgcrypto"] = shim


def patch_opentele_for_new_python() -> None:
    global _patched
    if _patched or "opentele.utils" in sys.modules:
        return

    _install_tgcrypto_shim()

    spec = importlib.util.find_spec("opentele")
    if spec is None or not spec.submodule_search_locations:
        return  # opentele не установлен — обычный ImportError сработает дальше как обычно

    pkg_dir = Path(next(iter(spec.submodule_search_locations)))
    utils_path = pkg_dir / "utils.py"
    if not utils_path.exists():
        return

    # Временная заглушка пакета — нужна только чтобы `from . import debug`
    # внутри utils.py резолвился как относительный импорт.
    had_placeholder = "opentele" not in sys.modules
    if had_placeholder:
        placeholder = types.ModuleType("opentele")
        placeholder.__path__ = [str(pkg_dir)]
        placeholder.__package__ = "opentele"
        sys.modules["opentele"] = placeholder

    try:
        utils_spec = importlib.util.spec_from_file_location("opentele.utils", utils_path)
        utils_module = importlib.util.module_from_spec(utils_spec)
        sys.modules["opentele.utils"] = utils_module
        utils_spec.loader.exec_module(utils_module)
    except Exception:
        sys.modules.pop("opentele.utils", None)
        if had_placeholder:
            sys.modules.pop("opentele", None)
        return
    finally:
        if had_placeholder:
            # Не мешаем настоящему `import opentele` инициализировать пакет
            # по-настоящему — оставляем в кэше только патченный utils.
            sys.modules.pop("opentele", None)

    original_new = utils_module.extend_class.__new__

    def patched_new(cls, decorated_cls, isOverride=False):
        for leaky_attr in _LEAKY_CLASS_DUNDERS:
            if leaky_attr in decorated_cls.__dict__:
                try:
                    delattr(decorated_cls, leaky_attr)
                except (AttributeError, TypeError):
                    pass
        return original_new(cls, decorated_cls, isOverride)

    utils_module.extend_class.__new__ = patched_new
    _patched = True
