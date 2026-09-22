"""Создаёт data/managers.xlsx с примером строк и нужными столбцами.

Запуск:
    python scripts/create_managers_template.py
"""
from pathlib import Path

import openpyxl

OUT = Path(__file__).resolve().parent.parent / "data" / "managers.xlsx"

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Менеджеры"
ws.append(["Аккаунт", "Имя менеджера", "Дисклеймер", "Статус", "Прокси"])
ws.append(["79991234567", "Анна Иванова", "", "работает", "socks5://user:pass@ru-proxy.example.com:1080"])
ws.append([
    "79997654321",
    "Сергей Петров",
    "Ответ подготовлен ИИ-ассистентом Сергея. По всем вопросам — отдел продаж.",
    "в отпуске",
    "socks5://user:pass@de-proxy.example.com:1080",
])
ws.append(["79995551122", "", "", "новый аккаунт", ""])

OUT.parent.mkdir(parents=True, exist_ok=True)
wb.save(OUT)
print(f"Создан шаблон: {OUT}")
