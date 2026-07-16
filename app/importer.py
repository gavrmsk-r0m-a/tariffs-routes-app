from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable

from app.repository import BusinessRuleError, Repository, normalize_phone_status, normalize_provider_name, validate_phone_number


@dataclass
class ImportPreview:
    entity_type: str
    total_rows: int = 0
    new_rows: int = 0
    duplicate_rows: int = 0
    error_rows: int = 0
    created_rows: int = 0
    updated_rows: int = 0
    skipped_rows: int = 0
    review_required_rows: int = 0
    legacy_info_rows: int = 0
    rows: list[dict] = field(default_factory=list)


def parse_csv(text: str) -> list[dict[str, str]]:
    sample = text.strip("\ufeff\n\r ")
    if not sample:
        return []
    dialect = csv.Sniffer().sniff(sample.splitlines()[0] + "\n") if sample else csv.excel
    reader = csv.DictReader(io.StringIO(sample), dialect=dialect)
    return [{k.strip(): (v or "").strip() for k, v in row.items() if k is not None} for row in reader]


def _first(row: dict[str, str], *names: str) -> str:
    lowered = {k.lower(): v for k, v in row.items()}
    for name in names:
        if name in row:
            return row[name].strip()
        if name.lower() in lowered:
            return lowered[name.lower()].strip()
    return ""



def _has_any(row: dict[str, str], *names: str) -> bool:
    lowered = {k.lower() for k in row}
    return any(name in row or name.lower() in lowered for name in names)


def _parse_monthly_fee(value: str) -> str | None:
    text = (value or "").strip()
    if text in {"", "?", "-"} or text.casefold() == "неизвестно":
        return None
    normalized = text.replace(" ", "").replace(",", ".")
    try:
        return str(Decimal(normalized))
    except InvalidOperation as exc:
        raise BusinessRuleError(f"Некорректная АП в EUR: {value}") from exc


def _map_final_status(value: str) -> tuple[str, bool, bool]:
    text = (value or "").strip().casefold()
    if not text:
        raise BusinessRuleError("Итоговый статус обязателен для импорта номеров")
    if text == "отключен":
        return "unused", False, False
    if text == "используется":
        return "used", True, False
    if text in {"???", "не используется", "не нужен", "свободен"}:
        return "unknown", True, True
    raise BusinessRuleError(f"Неизвестный Итоговый статус: {value}")

def preview_import(conn: sqlite3.Connection, entity_type: str, csv_text: str) -> ImportPreview:
    rows = parse_csv(csv_text)
    preview = ImportPreview(entity_type=entity_type, total_rows=len(rows))
    seen_keys: dict[tuple, int] = {}
    for idx, row in enumerate(rows, start=2):
        try:
            key = _business_key(conn, entity_type, row)
            if entity_type == "phone_numbers":
                phone_values = _phone_import_values(conn, row)
                phone_values.update(_phone_reference_ids(conn, row))
            else:
                phone_values = {}
            if key in seen_keys:
                preview.duplicate_rows += 1
                if entity_type == "phone_numbers":
                    preview.error_rows += 1
                    preview.rows.append(_phone_preview_row(idx, "duplicate_in_file", "duplicate_in_file", f"Номер уже встречался в строке {seen_keys[key]} этого файла.", phone_values, key, errors=[f"Номер уже встречался в строке {seen_keys[key]} этого файла."]))
                else:
                    preview.rows.append({"line": idx, "status": "duplicate_in_file", "action": "skip", "message": str(key)})
                continue
            seen_keys[key] = idx
            if entity_type == "phone_numbers":
                if _phone_review_reasons(phone_values):
                    preview.review_required_rows += 1
                if phone_values.get("reference_legacy"):
                    preview.legacy_info_rows += 1
            exists = _exists(conn, entity_type, key)
            if exists:
                preview.duplicate_rows += 1
                if entity_type == "phone_numbers":
                    preview.rows.append(_phone_preview_row(idx, "update", "update", "Строка обновит существующий номер.", phone_values, key))
                else:
                    preview.rows.append({"line": idx, "status": "duplicate_in_db", "action": "update", "message": str(key)})
            else:
                preview.new_rows += 1
                if entity_type == "phone_numbers":
                    preview.rows.append(_phone_preview_row(idx, "create", "create", "Строка создаст новый номер.", phone_values, key))
                else:
                    preview.rows.append({"line": idx, "status": "new", "action": "create", "message": str(key)})
        except Exception as exc:  # preview should collect row errors
            preview.error_rows += 1
            if entity_type == "phone_numbers":
                preview.rows.append({"line": idx, "status": "error", "action": "error", "number": _first(row, "number", "номер"), "working_status": "", "active_provider": "", "review_required": "Нет", "review_reasons": "", "errors": str(exc), "info": "", "message": str(exc)})
            else:
                preview.rows.append({"line": idx, "status": "error", "action": "skip", "message": str(exc)})
    return preview


def apply_import(
    conn: sqlite3.Connection,
    entity_type: str,
    csv_text: str,
    *,
    user_id: int,
    duplicate_action: str = "update",
    mode: str = "append_update",
) -> ImportPreview:
    if mode == "replace_section":
        raise BusinessRuleError("Режим замены раздела временно отключён. Используйте Дополнить / обновить.")
    preview = preview_import(conn, entity_type, csv_text)
    if entity_type == "phone_numbers" and preview.error_rows:
        raise BusinessRuleError("Импорт невозможен: в предпросмотре есть ошибки. Исправьте файл и повторите предпросмотр.")
    if mode == "replace_section":
        _clear_section(conn, entity_type)
    repo = Repository(conn)
    rows = parse_csv(csv_text)
    seen_keys: set[tuple] = set()
    for row in rows:
        try:
            key = _business_key(conn, entity_type, row)
            if key in seen_keys:
                preview.skipped_rows += 1
                continue
            seen_keys.add(key)
            exists = _exists(conn, entity_type, key)
            if mode != "replace_section" and exists and duplicate_action == "skip":
                preview.skipped_rows += 1
                continue
            if entity_type == "routes":
                _apply_route(repo, row, user_id, exists=exists)
            elif entity_type == "phone_numbers":
                _apply_phone(repo, row, user_id, exists=exists)
            elif entity_type == "calling_companies":
                _apply_company(repo, row, user_id, exists=exists)
            elif entity_type == "tariffs":
                _apply_tariff(repo, row, user_id, exists=exists)
            elif entity_type == "dictionaries":
                _apply_dictionary(repo, row)
            else:
                raise BusinessRuleError(f"Unsupported import type: {entity_type}")
            if entity_type == "phone_numbers":
                imported_for_count = _phone_import_values(conn, row)
                refs_for_count = _phone_reference_ids(conn, row)
                if bool(refs_for_count["empty_provider"]) or bool(imported_for_count["empty_project"]) or bool(imported_for_count["empty_assignment"]) or bool(imported_for_count["status_review_required"]):
                    preview.review_required_rows += 1
                if bool(refs_for_count["reference_legacy"]):
                    preview.legacy_info_rows += 1
            if exists:
                preview.updated_rows += 1
            else:
                preview.created_rows += 1
        except Exception as exc:
            preview.skipped_rows += 1
            if entity_type == "phone_numbers":
                preview.error_rows += 1
                preview.rows.append({"line": 0, "status": "error", "action": "error", "message": str(exc), "errors": str(exc)})
            continue
    conn.commit()
    return preview


def _business_key(conn: sqlite3.Connection, entity_type: str, row: dict[str, str]) -> tuple:
    if entity_type == "routes":
        country = _first(row, "country", "страна", "гео")
        name = _first(row, "name", "route", "название маршрута", "маршрут")
        if not country or not name:
            raise BusinessRuleError("country and route name are required")
        return (country, name)
    if entity_type == "phone_numbers":
        country = _first(row, "country", "страна", "гео", "GEO")
        number = _first(row, "number", "номер")
        if not country or not number:
            raise BusinessRuleError("country and number are required")
        normalized = validate_phone_number(number)
        return (normalized,)
    if entity_type == "calling_companies":
        server = _first(row, "server", "сервер")
        country = _first(row, "country", "страна", "гео")
        external_id = _first(row, "company_id_external", "company_id", "id компании", "ID компании")
        if not server or not country or not external_id:
            raise BusinessRuleError("server, country, and company_id_external are required")
        return (server, country, external_id)
    if entity_type == "tariffs":
        country = _first(row, "country", "страна", "гео")
        provider = _first(row, "provider", "провайдер")
        prefix = _first(row, "prefix", "префикс")
        if not country or not provider:
            raise BusinessRuleError("country and provider are required")
        return (country, provider, prefix)
    if entity_type == "dictionaries":
        kind = _first(row, "type", "тип")
        name = _first(row, "name", "название")
        if not kind or not name:
            raise BusinessRuleError("type and name are required")
        return (kind, name)
    raise BusinessRuleError(f"Unsupported import type: {entity_type}")


def _parse_bool(value: str, *, default: bool = True) -> bool:
    if value == "":
        return default
    return value.strip().lower() not in {"0", "no", "false", "нет", "неактивна", "inactive"}


def _resolve_reference(conn: sqlite3.Connection, table: str, value: str, label: str, *, code_column: str = "name", normalized_provider: bool = False) -> tuple[int | str, bool]:
    text = value.strip()
    if not text:
        raise BusinessRuleError(f"{label} обязателен")
    repo = Repository(conn)
    if normalized_provider:
        row = repo.get_provider_by_normalized_name(normalize_provider_name(text))
    elif table == "countries" and code_column == "name":
        row = repo.get_country_by_name(text)
    elif table == "currencies" and code_column == "code":
        row = repo.get_currency_by_code(text)
    elif table == "projects" and code_column == "name":
        row = repo.get_project_by_name(text)
    elif table == "phone_number_types" and code_column == "name":
        row = repo.get_phone_number_type_by_name(text)
    else:
        raise BusinessRuleError(f"Unsupported reference lookup: {table}.{code_column}")
    if row is None:
        raise BusinessRuleError(f"Значение ‘{text}’ не найдено в справочнике {label}. Исправьте файл или добавьте значение в справочник вручную.")
    return row["id"], not bool(row["is_active"])


def _resolve_assignment_code(conn: sqlite3.Connection, value: str) -> tuple[str | None, bool]:
    value = value.strip()
    if not value:
        return None, False
    row = Repository(conn).get_phone_assignment_type_by_code_or_name(value)
    if row is None:
        raise BusinessRuleError(f"Значение ‘{value}’ не найдено в справочнике Назначение. Исправьте файл или добавьте значение в справочник вручную.")
    return str(row["code"]), not bool(row["is_active"])


def _phone_import_values(conn: sqlite3.Connection, row: dict[str, str]) -> dict[str, str | None | bool]:
    project = _first(row, "project", "project_label", "проект") or None
    project_legacy = False
    if project:
        _, project_legacy = _resolve_reference(conn, "projects", project, "Проект")
    phone_type = _first(row, "phone_type", "тип номера") or None
    phone_type_legacy = False
    if phone_type:
        _, phone_type_legacy = _resolve_reference(conn, "phone_number_types", phone_type, "Тип номера")
    assignment, assignment_legacy = _resolve_assignment_code(conn, _first(row, "assignment_type", "назначение"))
    has_final_status = _has_any(row, "Итоговый статус", "final_status")
    if has_final_status:
        status, is_active, status_review_required = _map_final_status(_first(row, "Итоговый статус", "final_status"))
    else:
        status = normalize_phone_status(_first(row, "status", "статус"))
        is_active = _parse_bool(_first(row, "is_active", "активен"), default=True)
        status_review_required = False
    monthly_fee = _parse_monthly_fee(_first(row, "АП в EUR", "monthly_fee"))
    return {
        "project_label": project,
        "assignment_type": assignment,
        "reference_legacy": project_legacy or phone_type_legacy or assignment_legacy,
        "empty_project": project is None,
        "empty_assignment": assignment is None,
        "status": status,
        "is_active": is_active,
        "status_review_required": status_review_required,
        "connection_cost": _first(row, "connection_fee", "connection_cost", "стоимость подключения") or None,
        "monthly_fee": monthly_fee,
        "phone_type": phone_type,
        "tariff_label": _first(row, "tariff_label", "тариф") or None,
        "comment": _first(row, "comment", "комментарий") or None,
        "created_at": _first(row, "created_at", "дата создания") or None,
        "outgoing_rate": _first(row, "outgoing_rate") or None,
        "incoming_rate": _first(row, "incoming_rate") or None,
        "has_imported_created_by": _has_any(row, "Создал", "imported_created_by", "source_created_by", "legacy_created_by"),
        "imported_created_by": _first(row, "Создал", "imported_created_by", "source_created_by", "legacy_created_by") or None,
    }




def _phone_review_reasons(values: dict) -> list[str]:
    reasons = []
    if values.get("empty_provider"):
        reasons.append("пустой провайдер")
    if values.get("empty_project"):
        reasons.append("пустой проект")
    if values.get("empty_assignment"):
        reasons.append("пустое назначение")
    if values.get("status_review_required"):
        reasons.append("статус требует проверки")
    return reasons


def _phone_preview_row(line: int, status: str, action: str, base_message: str, values: dict, key: tuple, *, errors: list[str] | None = None) -> dict:
    reasons = _phone_review_reasons(values)
    info = []
    if values.get("reference_legacy"):
        info.append("Историческое значение / legacy")
    if values.get("imported_created_by"):
        info.append(f"Создал в Excel: {values['imported_created_by']}")
    message = _phone_preview_message(values, key)
    if base_message:
        message = f"{base_message} {message}"
    return {
        "line": line,
        "status": status,
        "action": action,
        "number": key[0] if key else "",
        "working_status": str(values.get("status") or ""),
        "active_provider": "Да" if values.get("is_active") else "Нет",
        "review_required": "Да" if reasons else "Нет",
        "review_reasons": ", ".join(reasons),
        "errors": "; ".join(errors or []),
        "info": "; ".join(info),
        "message": message,
    }

def _phone_preview_message(values: dict, key: tuple) -> str:
    notes = []
    reasons = []
    if values.get("reference_legacy"):
        notes.append("историческое справочное значение")
    if values.get("empty_provider"):
        reasons.append("пустой провайдер")
    if values.get("empty_project"):
        reasons.append("пустой проект")
    if values.get("empty_assignment"):
        reasons.append("пустое назначение")
    if values.get("status_review_required"):
        reasons.append("статус требует проверки")
    if reasons:
        notes.append("Требует проверки: " + ", ".join(reasons))
    if values.get("imported_created_by"):
        notes.append(f"Создал в Excel: {values['imported_created_by']}")
    return f"{key}" + ("; " + "; ".join(notes) if notes else "")


def _phone_reference_ids(conn: sqlite3.Connection, row: dict[str, str]) -> dict[str, int | None]:
    country_name = _first(row, "country", "страна", "гео", "GEO")
    country_id, country_legacy = _resolve_reference(conn, "countries", country_name, "ГЕО")
    provider_name = _first(row, "provider", "провайдер")
    provider_id = None
    provider_legacy = False
    if provider_name:
        provider_id, provider_legacy = _resolve_reference(conn, "providers", provider_name, "Провайдер", normalized_provider=True)
    currency_code = _first(row, "currency", "валюта") or "EUR"
    currency_id, currency_legacy = _resolve_reference(conn, "currencies", currency_code, "Валюта", code_column="code")
    values = _phone_import_values(conn, row)
    return {
        "country_id": int(country_id),
        "provider_id": int(provider_id) if provider_id is not None else None,
        "currency_id": int(currency_id),
        "reference_legacy": country_legacy or provider_legacy or currency_legacy or bool(values["reference_legacy"]),
        "empty_provider": not bool(provider_name),
    }

def _exists(conn: sqlite3.Connection, entity_type: str, key: tuple) -> bool:
    repo = Repository(conn)
    if entity_type == "routes":
        country, name = key
        return repo.route_exists_by_country_name_and_name(country, name)
    if entity_type == "phone_numbers":
        (normalized_number,) = key
        return repo.phone_number_exists_by_normalized_number(normalized_number)
    if entity_type == "calling_companies":
        server, country, external_id = key
        return repo.calling_company_exists_by_server_country_external_id(server, country, external_id)
    if entity_type == "tariffs":
        country, provider, prefix = key
        return repo.current_tariff_exists_by_country_provider_prefix(country, provider, prefix or None)
    return False


def _apply_route(repo: Repository, row: dict[str, str], user_id: int, *, exists: bool) -> None:
    country_id = repo.get_or_create_country(_first(row, "country", "страна", "гео"))
    provider_id = repo.get_or_create_provider(_first(row, "provider", "провайдер") or "Unknown")
    prefix_id = repo.get_or_create_prefix(provider_id, _first(row, "prefix", "префикс") or None)
    name = _first(row, "name", "route", "название маршрута", "маршрут")
    values = {
        "provider_id": provider_id,
        "provider_prefix_id": prefix_id,
        "project_label": _first(row, "project_label", "проект") or None,
        "cli_source_type": _first(row, "cli_source_type", "тип аон") or "other",
        "cli_source_label": _first(row, "cli_source_label", "аон", "источник аон") or "OTHER",
        "comment": _first(row, "comment", "комментарий") or None,
    }
    if exists:
        repo.update_route_import_fields(
            country_id=country_id,
            name=name,
            provider_id=values["provider_id"],
            provider_prefix_id=values["provider_prefix_id"],
            project_label=values["project_label"],
            cli_source_type=values["cli_source_type"],
            cli_source_label=values["cli_source_label"],
            comment=values["comment"],
            updated_by=user_id,
        )
    else:
        repo.create_route(country_id=country_id, provider_id=provider_id, provider_prefix_id=prefix_id, name=name, created_by=user_id, **{k: v for k, v in values.items() if k not in {"provider_id", "provider_prefix_id"}})


def _apply_phone(repo: Repository, row: dict[str, str], user_id: int, *, exists: bool) -> None:
    number = _first(row, "number", "номер")
    validate_phone_number(number)
    country_name = _first(row, "country", "страна", "гео", "GEO")
    if not country_name:
        raise BusinessRuleError("ГЕО обязателен для импорта номеров")
    refs = _phone_reference_ids(repo.conn, row)
    country_id = int(refs["country_id"])
    provider_id = refs["provider_id"]
    imported = _phone_import_values(repo.conn, row)
    review_required = bool(refs["empty_provider"]) or bool(imported["empty_project"]) or bool(imported["empty_assignment"]) or bool(imported["status_review_required"])
    currency_id = int(refs["currency_id"])
    is_active = bool(imported["is_active"])
    data = {
        "country_id": country_id,
        "provider_id": provider_id,
        "project_label": imported["project_label"],
        "assignment_type": imported["assignment_type"],
        "status": imported["status"],
        "is_active": 1 if is_active else 0,
        "connection_cost": imported["connection_cost"],
        "monthly_fee": imported["monthly_fee"],
        "phone_type": imported["phone_type"],
        "tariff_label": imported["tariff_label"],
        "comment": imported["comment"],
        "currency_id": currency_id,
        "created_at": imported["created_at"],
        "outgoing_rate": imported["outgoing_rate"],
        "incoming_rate": imported["incoming_rate"],
        "review_required": review_required,
        "has_imported_created_by": imported["has_imported_created_by"],
        "imported_created_by": imported["imported_created_by"],
    }
    if exists:
        existing = repo.get_phone_number_import_identity_by_normalized_number(validate_phone_number(number))
        imported_created_by = existing["imported_created_by"] if existing else None
        should_update_imported_created_by = bool(data["has_imported_created_by"] and data["imported_created_by"])
        if should_update_imported_created_by:
            imported_created_by = data["imported_created_by"]
        review_required = bool(data["review_required"] or (existing and existing["review_required"]))
        if is_active:
            deactivated_at = None
        else:
            deactivated_at = existing["deactivated_at"] if existing else None
            if deactivated_at is None:
                deactivated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        details = "Номер импортирован/обновлён"
        if should_update_imported_created_by:
            old_label = existing["imported_created_by"] if existing and existing["imported_created_by"] else "—"
            if old_label != imported_created_by:
                details += f". Создал в Excel: было {old_label}, стало {imported_created_by}"
            else:
                details += f". Создал в Excel: {imported_created_by}"
        repo.update_phone_number_import_fields_with_history(
            normalized_number=validate_phone_number(number), phone_number_id=existing["id"],
            country_id=data["country_id"], provider_id=data["provider_id"],
            project_label=data["project_label"], assignment_type=data["assignment_type"],
            status=data["status"], is_active=is_active, connection_cost=data["connection_cost"],
            monthly_fee=data["monthly_fee"], outgoing_rate=data["outgoing_rate"],
            incoming_rate=data["incoming_rate"], currency_id=data["currency_id"],
            phone_type=data["phone_type"], tariff_label=data["tariff_label"], comment=data["comment"],
            review_required=review_required, imported_created_by=imported_created_by,
            deactivated_at=deactivated_at, updated_by=user_id, history_changed_by=user_id,
            history_new_value=details, history_comment=details,
        )
    else:
        create_data = {k: v for k, v in data.items() if k not in {"has_imported_created_by"}}
        repo.create_phone_number(number=number, created_by=user_id, deactivated_at=(data["created_at"] if not is_active else None), **create_data)


def _apply_company(repo: Repository, row: dict[str, str], user_id: int, *, exists: bool) -> None:
    server_name = _first(row, "server", "сервер")
    server = repo.get_server_by_name(server_name)
    server_id = repo.create_server(server_name) if server is None else int(server["id"])
    country_id = repo.get_or_create_country(_first(row, "country", "страна", "гео"))
    external_id = _first(row, "company_id_external", "company_id", "id компании", "ID компании")
    has_auto = (_first(row, "has_autorotation", "авторотация") or "").lower() in {"1", "yes", "true", "да"}
    is_active = (_first(row, "is_active", "активна") or "да").lower() not in {"0", "no", "false", "нет"}
    if exists:
        repo.update_calling_company_import_fields(
            server_id=server_id,
            country_id=country_id,
            company_id_external=external_id,
            company_name=_first(row, "company_name", "название компании"),
            has_autorotation=has_auto,
            comment=_first(row, "comment", "комментарий") or None,
            is_active=is_active,
            updated_by=user_id,
        )
    else:
        repo.create_calling_company(server_id=server_id, country_id=country_id, company_name=_first(row, "company_name", "название компании"), company_id_external=external_id, has_autorotation=has_auto, created_by=user_id, comment=_first(row, "comment", "комментарий") or None, is_active=is_active)


def _apply_tariff(repo: Repository, row: dict[str, str], user_id: int, *, exists: bool) -> None:
    country_id = repo.get_or_create_country(_first(row, "country", "страна", "гео"))
    currency_id = repo.get_or_create_currency(_first(row, "currency", "валюта") or "EUR")
    provider_id = repo.get_or_create_provider(_first(row, "provider", "провайдер"), currency_id)
    prefix_id = repo.get_or_create_prefix(provider_id, _first(row, "prefix", "префикс") or None)
    price = _first(row, "price", "цена", "price_in_provider_currency") or "0"
    rate = _first(row, "rate", "курс", "conversion_rate_to_eur") or "1"
    rate_date = _first(row, "rate_date", "дата курса") or "1970-01-01"
    if exists:
        repo.conn.execute(
            "UPDATE tariffs SET is_current = 0, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE country_id = ? AND provider_id = ? AND COALESCE(provider_prefix_id, 0) = COALESCE(?, 0) AND is_current = 1",
            (user_id, country_id, provider_id, prefix_id),
        )
    repo.create_tariff(country_id=country_id, provider_id=provider_id, provider_prefix_id=prefix_id, provider_currency_id=currency_id, price_in_provider_currency=price, conversion_rate_to_eur=rate, conversion_rate_date=rate_date, created_by=user_id, comment=_first(row, "comment", "комментарий") or None)


def _apply_dictionary(repo: Repository, row: dict[str, str]) -> None:
    kind = _first(row, "type", "тип").lower()
    name = _first(row, "name", "название")
    if kind == "country":
        repo.get_or_create_country(name)
    elif kind == "provider":
        repo.get_or_create_provider(name)
    elif kind == "currency":
        repo.get_or_create_currency(name)
    elif kind == "server":
        if repo.get_server_by_name(name) is None:
            repo.create_server(name)
    elif kind == "project":
        repo.ensure_project_exists(name)
    elif kind == "phone_type":
        repo.ensure_phone_number_type_exists(name)
    elif kind == "phone_assignment":
        code = _first(row, "code", "код") or name
        repo.ensure_phone_assignment_type_exists(code, name)
    else:
        raise BusinessRuleError(f"Unsupported dictionary type: {kind}")


def _clear_section(conn: sqlite3.Connection, entity_type: str) -> None:
    # Replacement is scoped to the selected section only, per MVP requirements.
    # Business logs/change_log are intentionally not cleared here.
    if entity_type == "routes":
        conn.execute("DELETE FROM route_phone_numbers")
        conn.execute("DELETE FROM route_phone_number_history")
        conn.execute("DELETE FROM route_history")
        conn.execute("DELETE FROM server_route_priorities")
        conn.execute("DELETE FROM routes")
    elif entity_type == "tariffs":
        conn.execute("DELETE FROM tariffs")
    elif entity_type == "phone_numbers":
        conn.execute("DELETE FROM route_phone_numbers")
        conn.execute("DELETE FROM route_phone_number_history")
        conn.execute("DELETE FROM phone_number_history")
        conn.execute("DELETE FROM phone_numbers")
    elif entity_type == "calling_companies":
        conn.execute("DELETE FROM calling_companies")
    elif entity_type == "dictionaries":
        # Keep this conservative; dictionary replacement can be expanded per dictionary type.
        return
    else:
        raise BusinessRuleError(f"Unsupported import type: {entity_type}")
