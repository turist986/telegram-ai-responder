"""Генерирует bcrypt-хэш для ADMIN_PASSWORD_HASH в .env.

Запуск:
    python scripts/hash_password.py
"""
import getpass

import bcrypt

if __name__ == "__main__":
    password = getpass.getpass("Пароль администратора: ")
    print(bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"))
