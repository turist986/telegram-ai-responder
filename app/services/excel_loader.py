import logging
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Account
from .proxy import ProxyConfigError, normalize_proxy

logger = logging.getLogger(__name__)

# Ожидаемые столбцы в managers.xlsx (порядок не важен, регистр не важен):
#   Аккаунт / Телефон  - идентификатор аккаунта — ОБЯЗАТЕЛЬНО, должен совпадать
#                         с именем загруженного .session файла (без расширения)
#   Имя менеджера       - имя, подставляется в дефолтный шаблон дисклеймера
#   Дисклеймер / Маркер - готовый текст дисклеймера для этого аккаунта (необязательно,
#                         переопределяет шаблон)
#   Статус              - свободный текст ("работает", "в отпуске", ...) — только
#                         информативно, не управляет тумблером вкл/выкл
#   Прокси              - socks5://user:pass@host:port (также socks4:// и http://),
#                         необязательно; тот же формат, что и в панели

_EMPTY_MARKS = {"нет", "-", "—", "–", "none", "n/a", "null", "no", "не надо"}

COLUMN_MAP = {
    "аккаунт": "identifier",
    "телефон": "identifier",
    "имя менеджера": "manager_name",
    "имя": "manager_name",
    "дисклеймер": "disclaimer_marker",
    "маркер": "disclaimer_marker",
    "статус": "status_note",
    "прокси": "proxy",
    "промпт": "system_prompt",
}


def _read_rows(path: Path) -> tuple[list[dict], dict[int, str]]:
    """Читает managers.xlsx через openpyxl и возвращает список словарей с
    нормализованными ключами (identifier / manager_name / disclaimer_marker /
    status_note), без зависимости от pandas."""
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active

    rows_iter = ws.iter_rows(values_only=True)
    header = next(rows_iter, None)
    if not header:
        return [], {}

    field_by_col: dict[int, str] = {}
    for idx, cell in enumerate(header):
        key = str(cell or "").strip().lower()
        if key in COLUMN_MAP:
            field_by_col[idx] = COLUMN_MAP[key]

    rows = []
    for raw_row in rows_iter:
        row = {
            "identifier": "",
            "manager_name": "",
            "disclaimer_marker": "",
            "status_note": "",
            "proxy": "",
            "system_prompt": "",
        }
        for idx, field in field_by_col.items():
            if idx < len(raw_row) and raw_row[idx] is not None:
                value = str(raw_row[idx]).strip()
                # «нет» вместо пустой ячейки — частая привычка; иначе оно попадёт в
                # дисклеймер клиентам, а в прокси сломает подключение.
                if field != "identifier" and value.lower() in _EMPTY_MARKS:
                    value = ""
                row[field] = value
        rows.append(row)

    wb.close()
    return rows, field_by_col


def sync_accounts_from_excel(path: Path, db: Session) -> dict:
    """Обновляет таблицу Account по данным managers.xlsx.

    Тумблер enabled (вкл/выкл автоответчика), выставленный в панели, не
    затрагивается — из Excel обновляются только имя, дисклеймер и статус.
    """
    rows, field_by_col = _read_rows(path)

    if "identifier" not in field_by_col.values():
        raise ValueError(
            "В файле не найден столбец 'Аккаунт' (или 'Телефон') с идентификатором аккаунта"
        )

    created, updated = 0, 0
    for row in rows:
        identifier = row["identifier"].strip()
        if not identifier:
            continue

        account = db.query(Account).filter_by(identifier=identifier).one_or_none()
        if account is None:
            account = Account(identifier=identifier, enabled=True)
            db.add(account)
            created += 1
        else:
            updated += 1

        account.manager_name = row["manager_name"] or None
        account.disclaimer_marker = row["disclaimer_marker"] or None
        account.status_note = row["status_note"] or None
        if "proxy" in field_by_col.values():
            try:
                new_proxy = normalize_proxy(row["proxy"])
            except ProxyConfigError:
                new_proxy = row["proxy"] or None  # воркер покажет понятную ошибку
            # прокси обязателен: пустая ячейка Excel не должна «разоружить» аккаунт
            if new_proxy or not (account.proxy and settings.require_proxy):
                account.proxy = new_proxy
        if "system_prompt" in field_by_col.values():
            account.system_prompt = row["system_prompt"] or None

    db.commit()
    logger.info("Excel sync: %s new, %s updated", created, updated)
    return {"created": created, "updated": updated}
