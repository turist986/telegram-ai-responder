"""Ставит зависимости импорта TData так, чтобы это работало на «голой» машине
(например, свежий Windows VPS) БЕЗ компилятора C++.

Почему не просто `pip install opentele`: opentele тянет пакет tgcrypto, который
собирается из исходников и требует Microsoft C++ Build Tools / build-essential — на чистом
сервере установка падает, и импорт TData оказывается недоступен. Здесь opentele ставится
без tgcrypto (`--no-deps`), а нужные ему PyQt5 и telethon — отдельно; вместо tgcrypto проект
использует чистый Python-шим (см. app/services/_opentele_compat.py). Готовое колесо tgcrypto
(если оно есть для вашей версии Python) ставится необязательно — просто ускоряет шифрование.

Запуск:  python scripts/install_tdata_deps.py
(внутри активированного venv, либо: venv\\Scripts\\python.exe scripts\\install_tdata_deps.py)"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pip(*args: str) -> int:
    return subprocess.call([sys.executable, "-m", "pip", *args])


def main() -> int:
    print("[1/3] PyQt5 (нужен opentele для чтения формата TData)…")
    if pip("install", "PyQt5"):
        print("[!] Не удалось установить PyQt5.")
        return 1
    print("[2/3] opentele без tgcrypto (компилятор не нужен)…")
    if pip("install", "--no-deps", "opentele==1.15.1"):
        print("[!] Не удалось установить opentele.")
        return 1
    print("[3/3] tgcrypto — необязательное ускорение, только готовое колесо…")
    # вывод глушим: отсутствие колеса — штатная ситуация, красные «ERROR» пугали бы зря
    quiet = subprocess.run([sys.executable, "-m", "pip", "install", "--only-binary=:all:", "tgcrypto"],
                           capture_output=True, text=True)
    if quiet.returncode:
        print("    (колеса tgcrypto для этой версии Python нет — ничего страшного, работает чистый Python-шим)")

    check = (
        "import sys; sys.path.insert(0, r'%s'); "
        "from app.services.session_utils import tdata_available; ok, hint = tdata_available(); "
        "print('OK: импорт TData доступен' if ok else 'НЕ РАБОТАЕТ: ' + hint); sys.exit(0 if ok else 1)" % ROOT
    )
    return subprocess.call([sys.executable, "-c", check])


if __name__ == "__main__":
    raise SystemExit(main())
