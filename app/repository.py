from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.db_adapter import (
    build_in_clause,
    extract_inserted_id,
    insert_ignore_statement,
    normalize_backend_name,
    placeholder,
    prepare_insert_returning_id,
    row_to_dict,
    rows_to_dicts,
    to_db_bool,
)

PHONE_RE = re.compile(r"^[1-9][0-9]{6,20}$")
VALID_PHONE_STATUSES = {"used", "unused", "free", "problem", "unknown"}


def generate_password_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2:sha256:200000${salt.hex()}${digest.hex()}"


def check_password_hash(password_hash: str, password: str) -> bool:
    try:
        method, salt_hex, digest_hex = password_hash.split("$", 2)
        _prefix, alg, iterations_text = method.split(":", 2)
        if alg != "sha256":
            return False
        digest = hashlib.pbkdf2_hmac(alg, password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations_text))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    # Keep the legacy two-value return for older tests/callers, but store new
    # passwords as a self-contained Werkzeug-style hash in password_hash.
    if salt is not None:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return digest.hex(), salt.hex()
    return generate_password_hash(password), ""


def verify_password(password: str, password_hash: str | None, password_salt: str | None = None) -> bool:
    if not password_hash:
        return False
    if password_salt:
        try:
            expected, _ = hash_password(password, bytes.fromhex(password_salt))
        except ValueError:
            return False
        return hmac.compare_digest(expected, password_hash)
    try:
        return check_password_hash(password_hash, password)
    except ValueError:
        return False


def normalize_search_text(value: object) -> str | None:
    """Return a trimmed, Unicode-casefolded search string or None for empty input."""
    if value is None:
        return None
    text = str(value).strip()
    return text.casefold() if text else None


def search_text_matches(value: object, search: object) -> int:
    """SQLite helper for Unicode-aware, case-insensitive partial text search."""
    needle = normalize_search_text(search)
    if needle is None:
        return 1
    haystack = "" if value is None else str(value)
    return 1 if needle in haystack.casefold() else 0
OLD_PHONE_STATUS_MAP = {"reserved": "free", "blocked": "problem", "disabled": "problem"}


def normalize_phone_status(status: str | None) -> str:
    normalized = (status or "").strip().lower()
    if normalized in VALID_PHONE_STATUSES:
        return normalized
    return OLD_PHONE_STATUS_MAP.get(normalized, "unknown")

ROUTING_SCOPE_LABELS = {
    "none": "Не меняли настройки в нашей системе",
    "server_priority": "Серверный приоритет",
    "campaign_setting": "Настройка кампании",
}

COMPANY_CHANGE_LABELS = {
    "enable_autorotation": "Включили авторотацию",
    "disable_autorotation": "Выключили авторотацию",
    "set_campaign_route": "Прописали ручной маршрут",
    "remove_campaign_route": "Убрали ручной маршрут",
}


def _bool_label(value: object) -> str:
    return "Да" if str(value) in {"1", "true", "True", "yes", "Да"} else "Нет"


def _empty_label(value: object) -> str:
    return "—" if value is None or str(value).strip() == "" else str(value)


def _normalize_optional_text(value: object) -> str:
    return "" if value is None or str(value).strip() == "" else str(value).strip()


def _normalize_decimal_value(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return Decimal(str(value).strip()).normalize()


def _values_equal(old: object, new: object, kind: str = "text") -> bool:
    if kind == "money":
        return _normalize_decimal_value(old) == _normalize_decimal_value(new)
    if kind == "bool":
        return (1 if str(old) in {"1", "true", "True", "yes", "Да"} else 0) == (1 if str(new) in {"1", "true", "True", "yes", "Да"} else 0)
    if kind == "optional_text":
        return _normalize_optional_text(old) == _normalize_optional_text(new)
    if old in (None, "") and new in (None, ""):
        return True
    return old == new


def _truncate_history_text(value: object, limit: int = 100) -> str:
    text = _empty_label(value).strip()
    if text == "—":
        return text
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _clean_number_label(value: object) -> str:
    if value is None or str(value).strip() == "":
        return "—"
    dec = _normalize_decimal_value(value)
    if dec is None:
        return "—"
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"

def _company_history_value(value: object, kind: str = "text") -> str:
    if kind == "bool":
        return _bool_label(value)
    if kind == "number":
        return _clean_number_label(value)
    return _empty_label(value)

def _company_history_change(label: str, old: object, new: object, kind: str = "text") -> str | None:
    compare_kind = "money" if kind == "number" else ("bool" if kind == "bool" else "optional_text")
    if _values_equal(old, new, compare_kind):
        return None
    old_label = _truncate_history_text(_company_history_value(old, kind), 120)
    new_label = _truncate_history_text(_company_history_value(new, kind), 120)
    return f"{label}: {old_label} → {new_label}"


NO_PREFIX_LABELS = {"без префикса", "no prefix", "—", "-"}


def is_no_prefix_text(value: object) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or text.lower() in NO_PREFIX_LABELS


def normalize_real_prefix(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if is_no_prefix_text(text):
        raise BusinessRuleError("Префикс должен быть реальным кодом. Для отсутствия префикса используйте вариант Без префикса в формах маршрутов/тарифов")
    if not text.isdigit():
        raise BusinessRuleError("Префикс должен состоять только из цифр")
    return text

class BusinessRuleError(ValueError):
    """Raised when a confirmed MVP business rule is violated."""


class ConcurrencyConflict(BusinessRuleError):
    """Raised when an optimistic concurrency token no longer matches."""


@dataclass(frozen=True)
class PhoneLinkResult:
    route_phone_number_id: int
    phone_number_id: int


def normalize_provider_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def validate_phone_number(number: str) -> str:
    if not PHONE_RE.fullmatch(number):
        raise BusinessRuleError(
            "Phone number must be in international format without +, 00, spaces, brackets, or other symbols"
        )
    return number


def validate_tariff_price(price: object) -> Decimal:
    text = "" if price is None else str(price).strip()
    if text == "":
        raise BusinessRuleError("Цена обязательна")
    normalized = text.replace(",", ".")
    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise BusinessRuleError("Цена должна быть числом") from exc
    if not value.is_finite():
        raise BusinessRuleError("Цена должна быть числом")
    if value <= 0:
        raise BusinessRuleError("Цена должна быть больше 0")
    return value




def validate_currency_rate(rate_to_eur: object) -> Decimal:
    text = "" if rate_to_eur is None else str(rate_to_eur).strip()
    if text == "":
        raise BusinessRuleError("Курс обязателен")
    normalized = text.replace(",", ".")
    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise BusinessRuleError("Курс должен быть числом") from exc
    if not value.is_finite():
        raise BusinessRuleError("Курс должен быть числом")
    if value <= 0:
        raise BusinessRuleError("Курс должен быть больше 0")
    return value

def eur_price(price: str | Decimal, rate: str | Decimal) -> Decimal:
    value = Decimal(str(price)) * Decimal(str(rate))
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def query_filters(
    filters: dict | None,
    mapping: dict[str, str],
    *,
    backend: str = "sqlite",
) -> tuple[str, list]:
    if not filters:
        return "", []
    clauses: list[str] = []
    params: list = []
    for key, column in mapping.items():
        value = filters.get(key)
        if value in (None, "", "all"):
            continue
        if key.endswith("_like"):
            search = str(value).strip()
            if not search:
                continue
            if normalize_backend_name(backend) == "postgres":
                p = placeholder(backend)
                clauses.append(
                    f"POSITION(LOWER(CAST({p} AS TEXT)) "
                    f"IN LOWER(COALESCE(CAST({column} AS TEXT), ''))) > 0"
                )
                params.append(search)
            else:
                clauses.append(f"search_text_matches({column}, ?) = 1")
                params.append(normalize_search_text(search))
        else:
            clauses.append(f"{column} = {placeholder(backend)}")
            params.append(value)
    return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", [])


class Repository:
    def __init__(self, conn: sqlite3.Connection, backend: str = "sqlite"):
        self.conn = conn
        self.backend = normalize_backend_name(backend)
        if self.backend == "sqlite":
            self.conn.create_function("search_text_matches", 2, search_text_matches)

    @contextmanager
    def transaction(self):
        try:
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def _user_columns(self) -> set[str]:
        if self.backend == "sqlite":
            return {row["name"] for row in self.conn.execute("PRAGMA table_info(users)")}
        p = placeholder(self.backend)
        rows = self.conn.execute(
            f"""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = {p}
            ORDER BY ordinal_position
            """,
            ("users",),
        )
        return {row["column_name"] for row in rows}

    def _role_key(self, role: str) -> str:
        normalized = (role or "operator").strip().lower()
        if normalized in {"admin", "operator", "guest"}:
            return normalized
        if normalized == "user":
            return "operator"
        return normalized or "operator"


    def _phone_snapshot_labels(self, country_id: int, provider_id: int | None, assignment_type: str | None, currency_id: int | None) -> dict[str, str | None]:
        def one(sql: str, params: tuple[object, ...]) -> str | None:
            row = self.conn.execute(sql, params).fetchone()
            return str(row[0]) if row and row[0] is not None else None
        p = placeholder(self.backend)
        return {
            "country_label": one(f"SELECT name FROM countries WHERE id = {p}", (country_id,)),
            "provider_label": one(f"SELECT name FROM providers WHERE id = {p}", (provider_id,)) if provider_id else None,
            "assignment_label": one(f"SELECT name FROM phone_assignment_types WHERE code = {p}", (assignment_type,)) if assignment_type else None,
            "currency_label": one(f"SELECT code FROM currencies WHERE id = {p}", (currency_id,)) if currency_id else None,
        }

    def dictionary_rename_preview(self, kind: str, entity_id: int) -> dict[str, int]:
        p = placeholder(self.backend)

        def count(table: str, column: str, value: object) -> int:
            row = row_to_dict(
                self.conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE {column} = {p}",
                    (value,),
                ).fetchone()
            )
            return int(row["count"]) if row else 0

        if kind == "countries":
            return {"Купленные номера": count("phone_numbers", "country_id", entity_id), "Маршруты": count("routes", "country_id", entity_id), "Тарифы": count("tariffs", "country_id", entity_id)}
        if kind == "providers":
            return {"Купленные номера": count("phone_numbers", "provider_id", entity_id), "Маршруты": count("routes", "provider_id", entity_id), "Тарифы": count("tariffs", "provider_id", entity_id)}
        if kind == "currencies":
            return {"Купленные номера": count("phone_numbers", "currency_id", entity_id), "Тарифы": count("tariffs", "provider_currency_id", entity_id)}
        if kind == "phone-types":
            row = row_to_dict(self.conn.execute(f"SELECT name FROM phone_number_types WHERE id = {p}", (entity_id,)).fetchone())
            return {"Купленные номера": count("phone_numbers", "phone_type", row["name"] if row else None)}
        if kind == "projects":
            row = row_to_dict(self.conn.execute(f"SELECT name FROM projects WHERE id = {p}", (entity_id,)).fetchone())
            label = row["name"] if row else None
            return {"Купленные номера": count("phone_numbers", "project_label", label), "Маршруты": count("routes", "project_label", label)}
        if kind == "phone-assignments":
            row = row_to_dict(self.conn.execute(f"SELECT code FROM phone_assignment_types WHERE id = {p}", (entity_id,)).fetchone())
            return {"Купленные номера": count("phone_numbers", "assignment_type", row["code"] if row else None)}
        return {}

    def update_dictionary_snapshots(self, kind: str, entity_id: int, old_label: str | None, new_label: str | None) -> dict[str, int]:
        counts = self.dictionary_rename_preview(kind, entity_id)
        p = placeholder(self.backend)
        if kind == "countries":
            self.conn.execute(f"UPDATE phone_numbers SET country_label = {p} WHERE country_id = {p}", (new_label, entity_id))
        elif kind == "providers":
            self.conn.execute(f"UPDATE phone_numbers SET provider_label = {p} WHERE provider_id = {p}", (new_label, entity_id))
        elif kind == "currencies":
            code = self.conn.execute(f"SELECT code FROM currencies WHERE id = {p}", (entity_id,)).fetchone()
            self.conn.execute(f"UPDATE phone_numbers SET currency_label = {p} WHERE currency_id = {p}", (code["code"] if code else new_label, entity_id))
        elif kind == "phone-types" and old_label != new_label:
            self.conn.execute(f"UPDATE phone_numbers SET phone_type = {p} WHERE phone_type = {p}", (new_label, old_label))
        elif kind == "projects" and old_label != new_label:
            self.conn.execute(f"UPDATE phone_numbers SET project_label = {p} WHERE project_label = {p}", (new_label, old_label))
            self.conn.execute(f"UPDATE routes SET project_label = {p} WHERE project_label = {p}", (new_label, old_label))
        elif kind == "phone-assignments":
            row = self.conn.execute(f"SELECT code FROM phone_assignment_types WHERE id = {p}", (entity_id,)).fetchone()
            if row:
                self.conn.execute(f"UPDATE phone_numbers SET assignment_label = {p} WHERE assignment_type = {p}", (new_label, row["code"]))
        return counts

    def create_user(self, username: str, role: str = "admin", display_name: str | None = None, password: str | None = None, email: str | None = None, must_change_password: bool = False, *, commit: bool = True) -> int:
        username = username.strip()
        p = placeholder(self.backend)
        existing = self.conn.execute(f"SELECT id FROM users WHERE username = {p}", (username,)).fetchone()
        if existing:
            return int(existing["id"])
        columns = self._user_columns()
        role_key = self._role_key(role)
        insert_columns = ["username", "display_name", "is_active"]
        values: list[object] = [username, display_name or username, to_db_bool(True, self.backend)]
        if "role_key" in columns:
            insert_columns.append("role_key"); values.append(role_key)
        if "role" in columns:
            insert_columns.append("role"); values.append(role_key)
        if "email" in columns:
            insert_columns.append("email"); values.append((email or "").strip() or None)
        if "must_change_password" in columns:
            insert_columns.append("must_change_password"); values.append(to_db_bool(must_change_password, self.backend))
        if "password_hash" in columns and "password_salt" in columns:
            password_hash, password_salt = hash_password(password) if password is not None else (None, None)
            insert_columns.extend(["password_hash", "password_salt"]); values.extend([password_hash, password_salt])
        elif "password_hash" in columns:
            insert_columns.append("password_hash"); values.append(hash_password(password)[0] if password is not None else None)
        if "auth_provider" in columns:
            insert_columns.append("auth_provider"); values.append("local")
        sql = prepare_insert_returning_id(
            f"INSERT INTO users({', '.join(insert_columns)}) VALUES ({', '.join(p for _ in insert_columns)})", self.backend)
        try:
            user_id = extract_inserted_id(self.conn.execute(sql, tuple(values)), self.backend)
            if commit:
                self.conn.commit()
            return user_id
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def list_users(self, active_only: bool = False) -> list[sqlite3.Row]:
        p = placeholder(self.backend)
        where = f"WHERE is_active = {p}" if active_only else ""
        params = (to_db_bool(True, self.backend),) if active_only else ()
        role_expr = "role_key" if "role_key" in self._user_columns() else "LOWER(role)"
        ordering = (
            "is_active DESC, display_name COLLATE NOCASE, username COLLATE NOCASE"
            if self.backend == "sqlite"
            else "is_active DESC, LOWER(COALESCE(NULLIF(display_name, ''), username)), LOWER(username), id"
        )
        return list(self.conn.execute(
            f"""
            SELECT id, username, COALESCE(NULLIF(display_name, ''), username) AS display_name,
                   {role_expr} AS role_key, email, must_change_password, is_active, created_at, updated_at
            FROM users
            {where}
            ORDER BY {ordering}
            """,
            params,
        ))

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        p = placeholder(self.backend)
        role_expr = "role_key" if "role_key" in self._user_columns() else "LOWER(role)"
        return self.conn.execute(
            f"""
            SELECT id, username, COALESCE(NULLIF(display_name, ''), username) AS display_name,
                   {role_expr} AS role_key, email, must_change_password, is_active, created_at, updated_at
            FROM users
            WHERE id = {p}
            """,
            (user_id,),
        ).fetchone()

    def get_user_by_username(self, username: str) -> sqlite3.Row | None:
        p = placeholder(self.backend)
        columns = self._user_columns()
        role_expr = "role_key" if "role_key" in columns else "LOWER(role)"
        password_cols = ", password_hash, password_salt" if {"password_hash", "password_salt"}.issubset(columns) else ""
        return self.conn.execute(
            f"""
            SELECT id, username, COALESCE(NULLIF(display_name, ''), username) AS display_name,
                   {role_expr} AS role_key, email, must_change_password, is_active, created_at, updated_at{password_cols}
            FROM users
            WHERE username = {p}
            """,
            (username.strip(),),
        ).fetchone()

    def authenticate_user(self, username: str, password: str) -> sqlite3.Row | None:
        user = self.get_user_by_username(username)
        if not user or not user["is_active"]:
            return None
        try:
            ok = verify_password(password, user["password_hash"], user["password_salt"])
        except (KeyError, IndexError):
            ok = False
        return user if ok else None



    def get_app_setting_value(self, key: str) -> str | None:
        p = placeholder(self.backend)
        row = self.conn.execute(f"SELECT value FROM app_settings WHERE key = {p}", (key,)).fetchone()
        if row is None or row["value"] in (None, ""):
            return None
        return str(row["value"])

    def set_app_setting_value(self, key: str, value: str | None, updated_by: int | None = None, *, commit: bool = True) -> None:
        p = placeholder(self.backend)
        try:
            self.conn.execute(
                f"""
                INSERT INTO app_settings(key, value, updated_at, updated_by)
                VALUES ({p}, {p}, CURRENT_TIMESTAMP, {p})
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (key, value, updated_by),
            )
            if commit:
                self.conn.commit()
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def delete_app_setting_value(self, key: str, *, commit: bool = True) -> None:
        p = placeholder(self.backend)
        try:
            self.conn.execute(f"DELETE FROM app_settings WHERE key = {p}", (key,))
            if commit:
                self.conn.commit()
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def get_hlr_daily_usage(self, usage_date: str) -> dict[str, object]:
        p = placeholder(self.backend)
        row = self.conn.execute(f"SELECT * FROM hlr_daily_usage WHERE usage_date = {p}", (usage_date,)).fetchone()
        if row is None:
            return {
                "date": usage_date,
                "usage_date": usage_date,
                "usage_source": "database",
                "checked_today": 0,
                "credits_spent_today": None,
                "last_check_count": 0,
                "last_check_credits": None,
                "updated_at": None,
            }
        return {
            "date": row["usage_date"],
            "usage_date": row["usage_date"],
            "usage_source": "database",
            "checked_today": int(row["checked_count"] or 0),
            "credits_spent_today": row["credits_spent"],
            "last_check_count": int(row["last_check_count"] or 0),
            "last_check_credits": row["last_check_credits"],
            "updated_at": row["updated_at"],
        }

    def upsert_hlr_daily_usage(self, usage_date: str, checked_count_delta: int, credits_delta: object | None = None, last_check_at: str | None = None, *, commit: bool = True) -> dict[str, object]:
        p = placeholder(self.backend)
        try:
            existing = self.conn.execute(
                f"SELECT checked_count, credits_spent FROM hlr_daily_usage WHERE usage_date = {p}",
                (usage_date,),
            ).fetchone()
            previous_checked = int(existing["checked_count"] or 0) if existing else 0
            previous_credits = existing["credits_spent"] if existing else None
            if credits_delta is None:
                next_credits = previous_credits
            else:
                next_credits = float(previous_credits or 0) + float(credits_delta)
                if float(next_credits).is_integer():
                    next_credits = int(next_credits)
            self.conn.execute(
                f"""
                INSERT INTO hlr_daily_usage(usage_date, checked_count, credits_spent, last_check_count, last_check_credits, updated_at)
                VALUES ({p}, {p}, {p}, {p}, {p}, {p})
                ON CONFLICT(usage_date) DO UPDATE SET
                    checked_count = excluded.checked_count,
                    credits_spent = excluded.credits_spent,
                    last_check_count = excluded.last_check_count,
                    last_check_credits = excluded.last_check_credits,
                    updated_at = excluded.updated_at
                """,
                (usage_date, previous_checked + checked_count_delta, next_credits, checked_count_delta, credits_delta, last_check_at or datetime.now().strftime("%Y-%m-%d %H:%M")),
            )
            if commit:
                self.conn.commit()
        except Exception:
            if commit:
                self.conn.rollback()
            raise
        return self.get_hlr_daily_usage(usage_date)

    def get_hlr_limit_override(self) -> str | None:
        return self.get_app_setting_value("hlr_daily_limit_override")

    def set_hlr_limit_override(self, value: object | None, updated_by: int | None = None, *, commit: bool = True) -> None:
        """Set the HLR override, optionally leaving transaction ownership to caller.

        ``commit=False`` is the narrow transaction-foundation pattern used by the
        rollback-only PostgreSQL harness.  Existing application callers retain
        their historical commit-by-default behavior.
        """
        if self.backend == "sqlite":
            if value in (None, ""):
                self.delete_app_setting_value("hlr_daily_limit_override", commit=commit)
                return
            self.set_app_setting_value("hlr_daily_limit_override", str(value), updated_by, commit=commit)
            return

        p = placeholder(self.backend)
        try:
            if value in (None, ""):
                self.conn.execute(
                    f"DELETE FROM app_settings WHERE key = {p}",
                    ("hlr_daily_limit_override",),
                )
            else:
                self.conn.execute(
                    f"""
                    INSERT INTO app_settings(key, value, updated_at, updated_by)
                    VALUES ({p}, {p}, CURRENT_TIMESTAMP, {p})
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by
                    """,
                    ("hlr_daily_limit_override", str(value), updated_by),
                )
            if commit:
                self.conn.commit()
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def get_user_section_permission(self, user_id: int, section_key: str) -> sqlite3.Row | None:
        p = placeholder(self.backend)
        return self.conn.execute(
            """
            SELECT can_read, can_write, can_export
            FROM user_permissions
            WHERE user_id = {p} AND section_key = {p}
            """.format(p=p),
            (user_id, section_key),
        ).fetchone()

    def get_user_permissions(self, user_id: int) -> dict[str, sqlite3.Row]:
        p = placeholder(self.backend)
        return {
            row["section_key"]: row
            for row in self.conn.execute(
                """
                SELECT section_key, can_read, can_write, can_export
                FROM user_permissions
                WHERE user_id = {p}
                """.format(p=p),
                (user_id,),
            )
        }

    def set_user_permissions(self, user_id: int, permissions: dict[str, dict[str, object]], *, commit: bool = True) -> None:
        p = placeholder(self.backend)
        try:
            for section_key, values in permissions.items():
                self.conn.execute(
                    f"""
                    INSERT INTO user_permissions(user_id, section_key, can_read, can_write, can_export)
                    VALUES ({p}, {p}, {p}, {p}, {p})
                    ON CONFLICT(user_id, section_key) DO UPDATE SET
                        can_read = excluded.can_read, can_write = excluded.can_write, can_export = excluded.can_export
                    """,
                    (user_id, section_key, to_db_bool(bool(values.get("can_read")), self.backend), to_db_bool(bool(values.get("can_write")), self.backend), to_db_bool(bool(values.get("can_export")), self.backend)),
                )
            if commit:
                self.conn.commit()
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def update_user_password(self, user_id: int, password: str, *, must_change_password: bool = False, commit: bool = True) -> None:
        columns = self._user_columns()
        if not {"password_hash", "password_salt"}.issubset(columns):
            return
        p = placeholder(self.backend)
        password_hash, password_salt = hash_password(password)
        must_clause = f", must_change_password = {p}" if "must_change_password" in columns else ""
        params: tuple[object, ...] = (password_hash, password_salt, to_db_bool(must_change_password, self.backend), user_id) if must_clause else (password_hash, password_salt, user_id)
        try:
            self.conn.execute(f"UPDATE users SET password_hash = {p}, password_salt = {p}, updated_at = CURRENT_TIMESTAMP{must_clause} WHERE id = {p}", params)
            if commit:
                self.conn.commit()
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def update_user(self, user_id: int, *, display_name: str, role_key: str, is_active: bool, username: str | None = None, email: str | None = None, commit: bool = True) -> None:
        columns = self._user_columns(); p = placeholder(self.backend)
        assignments = [f"display_name = {p}", f"is_active = {p}", "updated_at = CURRENT_TIMESTAMP"]
        values: list[object] = [display_name.strip(), to_db_bool(is_active, self.backend)]
        if username is not None:
            assignments.append(f"username = {p}"); values.append(username.strip())
        if "email" in columns:
            assignments.append(f"email = {p}"); values.append((email or "").strip() or None)
        normalized_role = self._role_key(role_key)
        if "role_key" in columns:
            assignments.append(f"role_key = {p}"); values.append(normalized_role)
        if "role" in columns:
            assignments.append(f"role = {p}"); values.append(normalized_role)
        values.append(user_id)
        try:
            self.conn.execute(f"UPDATE users SET {', '.join(assignments)} WHERE id = {p}", tuple(values))
            if commit:
                self.conn.commit()
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def create_country(self, name: str, code: str | None = None, *, commit: bool = True) -> int:
        p = placeholder(self.backend)
        sql = prepare_insert_returning_id(
            f"INSERT INTO countries(name, code, is_active) VALUES ({p}, {p}, {p})",
            self.backend,
        )
        try:
            cur = self.conn.execute(sql, (name, code, to_db_bool(True, self.backend)))
            country_id = extract_inserted_id(cur, self.backend)
            if commit:
                self.conn.commit()
            return country_id
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def create_currency(self, code: str, name: str, symbol: str | None = None, *, commit: bool = True) -> int:
        p = placeholder(self.backend)
        sql = prepare_insert_returning_id(
            f"INSERT INTO currencies(code, name, symbol, is_active) VALUES ({p}, {p}, {p}, {p})",
            self.backend,
        )
        try:
            cur = self.conn.execute(sql, (code, name, symbol, to_db_bool(True, self.backend)))
            currency_id = extract_inserted_id(cur, self.backend)
            if commit:
                self.conn.commit()
            return currency_id
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def create_provider(
        self,
        name: str,
        provider_type: str = "unknown",
        default_currency_id: int | None = None,
        comment: str | None = None,
        *,
        commit: bool = True,
    ) -> int:
        p = placeholder(self.backend)
        sql = prepare_insert_returning_id(
            f"""
            INSERT INTO providers(name, normalized_name, provider_type, default_currency_id, is_active, comment)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p})
            """,
            self.backend,
        )
        try:
            cur = self.conn.execute(
                sql,
                (name, normalize_provider_name(name), provider_type, default_currency_id, to_db_bool(True, self.backend), comment),
            )
            provider_id = extract_inserted_id(cur, self.backend)
            if commit:
                self.conn.commit()
            return provider_id
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def create_prefix(self, provider_id: int, prefix: str | None, name: str | None = None, *, commit: bool = True) -> int:
        prefix = normalize_real_prefix(prefix)
        p = placeholder(self.backend)
        sql = prepare_insert_returning_id(
            f"""
            INSERT INTO provider_prefixes(provider_id, prefix, name, is_active)
            VALUES ({p}, {p}, {p}, {p})
            """,
            self.backend,
        )
        try:
            cur = self.conn.execute(sql, (provider_id, prefix or None, name, to_db_bool(True, self.backend)))
            prefix_id = extract_inserted_id(cur, self.backend)
            if commit:
                self.conn.commit()
            return prefix_id
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def create_route(
        self,
        *,
        country_id: int,
        provider_id: int,
        name: str,
        cli_source_type: str,
        cli_source_label: str,
        created_by: int,
        provider_prefix_id: int | None = None,
        project_label: str | None = None,
        comment: str | None = None,
        is_actual: bool = True,
        priority_status: str = "unknown",
        inbound_line_available: bool = False,
        aon_pool: str | None = None,
        rnd_type: str | None = None,
        rnd_pool_owner: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO routes(
                country_id, provider_id, provider_prefix_id, name, project_label,
                cli_source_type, cli_source_label, aon_pool, rnd_type, rnd_pool_owner, comment, is_actual, priority_status,
                inbound_line_available, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                country_id,
                provider_id,
                provider_prefix_id,
                name,
                project_label,
                cli_source_type,
                cli_source_label,
                aon_pool,
                rnd_type,
                rnd_pool_owner,
                comment,
                1 if is_actual else 0,
                priority_status,
                1 if inbound_line_available else 0,
                created_by,
            ),
        )
        route_id = int(cur.lastrowid)
        self.conn.execute(
            """
            INSERT INTO route_history(route_id, action, changed_by, field_name, new_value, comment)
            VALUES (?, 'created', ?, 'route', ?, ?)
            """,
            (route_id, created_by, name, comment),
        )
        self._change_log("route", route_id, "route.created", created_by, new_values={"name": name})
        self.conn.commit()
        return route_id

    def create_phone_number(
        self,
        *,
        country_id: int,
        number: str,
        assignment_type: str,
        status: str,
        created_by: int,
        phone_type: str | None = None,
        tariff_label: str | None = None,
        provider_id: int | None = None,
        project_label: str | None = None,
        connection_cost: str | None = None,
        monthly_fee: str | None = None,
        outgoing_rate: str | None = None,
        incoming_rate: str | None = None,
        currency_id: int | None = None,
        comment: str | None = None,
        is_active: bool = True,
        review_required: bool = False,
        created_at: str | None = None,
        deactivated_at: str | None = None,
        imported_created_by: str | None = None,
        commit: bool = True,
    ) -> int:
        normalized = validate_phone_number(number)
        if not is_active and deactivated_at is None:
            deactivated_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        labels = self._phone_snapshot_labels(country_id, provider_id, assignment_type, currency_id)
        p = placeholder(self.backend)
        phone_insert_sql = prepare_insert_returning_id(
            f"""
            INSERT INTO phone_numbers(
                country_id, provider_id, country_label, provider_label, number, normalized_number, project_label,
                assignment_type, assignment_label, phone_type, tariff_label, status, connection_cost, monthly_fee, outgoing_rate,
                incoming_rate, currency_id, currency_label, comment, is_active, review_required, imported_created_by, created_by, created_at, deactivated_at
            )
            VALUES ({', '.join([p] * 23)}, COALESCE({p}, CURRENT_TIMESTAMP), {p})
            """,
            self.backend,
        )
        cur = self.conn.execute(
            phone_insert_sql,
            (
                country_id,
                provider_id,
                labels["country_label"],
                labels["provider_label"],
                number,
                normalized,
                project_label,
                assignment_type,
                labels["assignment_label"],
                phone_type,
                tariff_label,
                normalize_phone_status(status),
                connection_cost,
                monthly_fee,
                outgoing_rate,
                incoming_rate,
                currency_id,
                labels["currency_label"],
                comment,
                to_db_bool(is_active, self.backend),
                to_db_bool(review_required, self.backend),
                imported_created_by,
                created_by,
                created_at,
                deactivated_at,
            ),
        )
        phone_id = extract_inserted_id(cur, self.backend)
        self.conn.execute(
            f"""
            INSERT INTO phone_number_history(phone_number_id, action, changed_by, field_name, new_value, comment)
            VALUES ({p}, 'created', {p}, 'number', {p}, {p})
            """,
            (phone_id, created_by, number, (f"{comment}. " if comment else "") + (f"Создал в Excel: {imported_created_by}" if imported_created_by else "")),
        )
        self.conn.execute(
            f"""
            INSERT INTO change_log(entity_type, entity_id, change_type, changed_by, old_values, new_values, summary, source)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
            """,
            (
                "phone_number", phone_id, "phone_number.created", created_by, None,
                json.dumps({"number": number, "imported_created_by": imported_created_by}, ensure_ascii=False),
                None, "ui",
            ),
        )
        if commit:
            self.conn.commit()
        return phone_id

    def add_phone_to_route(
        self,
        *,
        route_id: int,
        phone_number_id: int,
        usage_type: str,
        added_by: int,
        comment: str | None = None,
    ) -> PhoneLinkResult:
        phone = self.conn.execute(
            "SELECT id, number, is_active, status FROM phone_numbers WHERE id = ?",
            (phone_number_id,),
        ).fetchone()
        if phone is None:
            raise BusinessRuleError("Phone number not found")
        if int(phone["is_active"]) != 1:
            raise BusinessRuleError("Нельзя добавить номер в маршрут: номер не активен у провайдера")
        if phone["status"] != "used":
            raise BusinessRuleError("Нельзя добавить номер в маршрут: рабочий статус номера должен быть ‘Используется’")

        cur = self.conn.execute(
            """
            INSERT INTO route_phone_numbers(route_id, phone_number_id, usage_type, is_active, added_by, comment)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (route_id, phone_number_id, usage_type, added_by, comment),
        )
        link_id = int(cur.lastrowid)
        self.conn.execute(
            """
            INSERT INTO route_phone_number_history(
                route_id, phone_number_id, action, changed_by, new_values, comment
            )
            VALUES (?, ?, 'added', ?, ?, ?)
            """,
            (route_id, phone_number_id, added_by, f'{{"usage_type": "{usage_type}"}}', comment),
        )
        self._change_log(
            "route_phone_number",
            link_id,
            "route_phone_number.added",
            added_by,
            new_values={"route_id": route_id, "phone_number_id": phone_number_id, "usage_type": usage_type},
        )
        self.conn.commit()
        return PhoneLinkResult(route_phone_number_id=link_id, phone_number_id=phone_number_id)

    def create_calling_company(
        self,
        *,
        server_id: int,
        country_id: int,
        company_name: str,
        company_id_external: str,
        has_autorotation: bool,
        created_by: int,
        comment: str | None = None,
        is_active: bool = True,
        line_count: int = 0,
        dial_set_count: int = 0,
        retry_interval_seconds: int = 0,
    ) -> int:
        normalized_external_id = company_id_external.strip()
        if not normalized_external_id:
            raise BusinessRuleError("Company external ID is required")
        duplicate = self.conn.execute(
            """
            SELECT cc.company_name, cc.company_id_external, s.name AS server_name
            FROM calling_companies cc
            JOIN servers s ON s.id = cc.server_id
            WHERE cc.company_id_external = ?
            LIMIT 1
            """,
            (normalized_external_id,),
        ).fetchone()
        if duplicate is not None:
            raise BusinessRuleError(f"Кампания с ID {normalized_external_id} уже существует: {duplicate['company_name']} / {duplicate['server_name']}")
        cur = self.conn.execute(
            """
            INSERT INTO calling_companies(
                server_id, country_id, company_name, company_id_external,
                has_autorotation, line_count, dial_set_count, retry_interval_seconds,
                comment, is_active, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                server_id,
                country_id,
                company_name,
                normalized_external_id,
                1 if has_autorotation else 0,
                int(line_count),
                int(dial_set_count),
                int(retry_interval_seconds),
                comment,
                1 if is_active else 0,
                created_by,
            ),
        )
        company_id = int(cur.lastrowid)
        if has_autorotation:
            self.create_company_routing_setting(
                calling_company_id=company_id,
                country_id=country_id,
                server_id=server_id,
                route_id=None,
                routing_mode="autorotation",
                has_autorotation=True,
                comment="Начальная авторотация при создании кампании",
                created_by=created_by,
            )
        details = [
            f"Название: {_company_history_value(company_name)}",
            f"ID кампании: {_company_history_value(normalized_external_id)}",
            f"Активна: {_company_history_value(is_active, 'bool')}",
            f"Количество линий: {_company_history_value(line_count, 'number')}",
            f"Количество наборов: {_company_history_value(dial_set_count, 'number')}",
            f"Интервал, сек.: {_company_history_value(retry_interval_seconds, 'number')}",
            f"Авторотация: {_company_history_value(has_autorotation, 'bool')}",
            f"Комментарий: {_truncate_history_text(_company_history_value(comment), 120)}",
        ]
        self._change_log(
            "calling_company",
            company_id,
            "calling_company.created",
            created_by,
            new_values={
                "event": "Компания создана",
                "description": "Компания создана",
                "details": "; ".join(details),
                "search_text": "; ".join([_company_history_value(comment), company_name, normalized_external_id]),
            },
            summary="Компания создана",
        )
        self.conn.commit()
        return company_id

    def get_calling_company(self, company_id: int) -> sqlite3.Row | None:
        p = placeholder(self.backend)
        return self.conn.execute(
            f"""
            SELECT cc.*, s.name AS server_name, c.name AS country_name
            FROM calling_companies cc
            JOIN servers s ON s.id = cc.server_id
            JOIN countries c ON c.id = cc.country_id
            WHERE cc.id = {p}
            """,
            (company_id,),
        ).fetchone()

    def update_calling_company(
        self,
        company_id: int,
        *,
        server_id: int,
        country_id: int,
        company_name: str,
        line_count: int,
        dial_set_count: int,
        has_autorotation: bool,
        retry_interval_seconds: int,
        is_active: bool,
        comment: str | None,
        updated_by: int,
        expected_updated_at: str | None = None,
    ) -> None:
        old = self.conn.execute("SELECT * FROM calling_companies WHERE id = ?", (company_id,)).fetchone()
        if old is None:
            raise BusinessRuleError("Calling company not found")
        changes = []
        new_server = self.conn.execute("SELECT name FROM servers WHERE id = ?", (server_id,)).fetchone()
        old_server = self.conn.execute("SELECT name FROM servers WHERE id = ?", (old["server_id"],)).fetchone()
        specs = [
            ("Сервер", old_server["name"] if old_server else old["server_id"], new_server["name"] if new_server else server_id, "text"),
            ("Название", old["company_name"], company_name, "text"),
            ("Активна", old["is_active"], 1 if is_active else 0, "bool"),
            ("Количество наборов", old["dial_set_count"], dial_set_count, "number"),
            ("Интервал, сек.", old["retry_interval_seconds"], retry_interval_seconds, "number"),
            ("Количество линий", old["line_count"], line_count, "number"),
            ("Комментарий", old["comment"], comment, "text"),
        ]
        for label, old_value, new_value, kind in specs:
            change = _company_history_change(label, old_value, new_value, kind)
            if change:
                changes.append(change)
        update_params = [server_id, country_id, company_name, int(line_count), int(dial_set_count), int(retry_interval_seconds), 1 if is_active else 0, comment, updated_by, company_id]
        token_clause = ""
        if expected_updated_at is not None:
            token_clause = " AND updated_at = ?"
            update_params.append(expected_updated_at)
        cur = self.conn.execute(
            f"""
            UPDATE calling_companies
            SET server_id = ?, country_id = ?, company_name = ?, line_count = ?, dial_set_count = ?,
                retry_interval_seconds = ?, is_active = ?, comment = ?, updated_by = ?,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?{token_clause}
            """,
            tuple(update_params),
        )
        if cur.rowcount == 0:
            if self.conn.execute("SELECT 1 FROM calling_companies WHERE id = ?", (company_id,)).fetchone() is None:
                raise BusinessRuleError("Calling company not found")
            raise ConcurrencyConflict("Запись была изменена другим пользователем. Обновите страницу и повторите действие.")
        if changes:
            only_active = len(changes) == 1 and changes[0].startswith("Активна:")
            if only_active and int(old["is_active"] or 0) == 0 and is_active:
                event = description = "Компания активирована"
            elif only_active and int(old["is_active"] or 0) == 1 and not is_active:
                event = description = "Компания деактивирована"
            else:
                event = "Компания изменена"
                description = f"Изменено полей: {len(changes)}"
            details = "; ".join(changes)
            self._change_log(
                "calling_company",
                company_id,
                "calling_company.updated",
                updated_by,
                old_values={"company_name": old["company_name"], "comment": old["comment"]},
                new_values={"event": event, "description": description, "details": details, "search_text": f"{old['company_name']} {company_name} {old['comment'] or ''} {comment or ''} {details}"},
                summary=event,
            )
        self.conn.commit()

    def create_server(self, name: str, comment: str | None = None, *, commit: bool = True) -> int:
        p = placeholder(self.backend)
        sql = prepare_insert_returning_id(
            f"INSERT INTO servers(name, comment, is_active) VALUES ({p}, {p}, {p})",
            self.backend,
        )
        try:
            cur = self.conn.execute(sql, (name, comment, to_db_bool(True, self.backend)))
            server_id = extract_inserted_id(cur, self.backend)
            if commit:
                self.conn.commit()
            return server_id
        except Exception:
            if commit:
                self.conn.rollback()
            raise


    def get_phone_number(self, phone_id: int) -> sqlite3.Row | None:
        p = placeholder(self.backend)
        return self.conn.execute(
            """
            SELECT pn.*, c.name AS country_name, p.name AS provider_name
            FROM phone_numbers pn
            JOIN countries c ON c.id = pn.country_id
            LEFT JOIN providers p ON p.id = pn.provider_id
            WHERE pn.id = {p}
            """.format(p=p),
            (phone_id,),
        ).fetchone()

    def get_route(self, route_id: int) -> sqlite3.Row | None:
        p = placeholder(self.backend)
        return self.conn.execute(
            """
            SELECT r.*, c.name AS country_name, p.name AS provider_name
            FROM routes r
            JOIN countries c ON c.id = r.country_id
            JOIN providers p ON p.id = r.provider_id
            WHERE r.id = {p}
            """.format(p=p),
            (route_id,),
        ).fetchone()

    def list_phone_history(self, phone_id: int) -> list[sqlite3.Row]:
        p = placeholder(self.backend)
        return list(self.conn.execute(
            """
            SELECT 'phone' AS source, pnh.action, pnh.changed_at, u.display_name AS user_name,
                   pnh.field_name, pnh.old_value, pnh.new_value, pnh.reason, pnh.comment,
                   NULL AS route_name, pn.number AS phone_number
            FROM phone_number_history pnh
            LEFT JOIN users u ON u.id = pnh.changed_by
            LEFT JOIN phone_numbers pn ON pn.id = pnh.phone_number_id
            WHERE pnh.phone_number_id = {p}
            UNION ALL
            SELECT 'route_phone' AS source, rpnh.action, rpnh.changed_at, u.display_name AS user_name,
                   NULL AS field_name, rpnh.old_values AS old_value, rpnh.new_values AS new_value, rpnh.reason, rpnh.comment,
                   r.name AS route_name, pn.number AS phone_number
            FROM route_phone_number_history rpnh
            LEFT JOIN users u ON u.id = rpnh.changed_by
            LEFT JOIN routes r ON r.id = rpnh.route_id
            LEFT JOIN phone_numbers pn ON pn.id = rpnh.phone_number_id
            WHERE rpnh.phone_number_id = {p} OR rpnh.old_phone_number_id = {p} OR rpnh.new_phone_number_id = {p}
            ORDER BY changed_at DESC
            """.format(p=p),
            (phone_id, phone_id, phone_id, phone_id),
        ))

    def list_route_history(self, route_id: int) -> list[sqlite3.Row]:
        p = placeholder(self.backend)
        return list(self.conn.execute(
            """
            SELECT 'route' AS source, rh.action, rh.changed_at, u.display_name AS user_name,
                   rh.field_name, rh.old_value, rh.new_value, rh.reason, rh.comment,
                   r.name AS route_name, NULL AS phone_number
            FROM route_history rh
            LEFT JOIN users u ON u.id = rh.changed_by
            LEFT JOIN routes r ON r.id = rh.route_id
            WHERE rh.route_id = {p}
            UNION ALL
            SELECT 'route_phone' AS source, rpnh.action, rpnh.changed_at, u.display_name AS user_name,
                   NULL AS field_name, rpnh.old_values AS old_value, rpnh.new_values AS new_value, rpnh.reason, rpnh.comment,
                   r.name AS route_name, pn.number AS phone_number
            FROM route_phone_number_history rpnh
            LEFT JOIN users u ON u.id = rpnh.changed_by
            LEFT JOIN routes r ON r.id = rpnh.route_id
            LEFT JOIN phone_numbers pn ON pn.id = rpnh.phone_number_id
            WHERE rpnh.route_id = {p}
            ORDER BY changed_at DESC
            """.format(p=p),
            (route_id, route_id),
        ))

    def list_routes(self, filters: dict | None = None) -> list[sqlite3.Row]:
        route_filters = dict(filters or {})
        prefix_id = route_filters.pop("prefix_id", None)
        is_actual = route_filters.get("is_actual")
        if is_actual in (True, 1, "1"):
            route_filters["is_actual"] = to_db_bool(True, self.backend)
        elif is_actual in (False, 0, "0") and is_actual not in (None, ""):
            route_filters["is_actual"] = to_db_bool(False, self.backend)
        where, params = query_filters(
            route_filters,
            {
                "country_id": "r.country_id",
                "provider_id": "r.provider_id",
                "is_actual": "r.is_actual",
                "search_like": "r.name",
            },
            backend=self.backend,
        )
        if prefix_id == "__none__":
            if where:
                where += " AND r.provider_prefix_id IS NULL"
            else:
                where = " WHERE r.provider_prefix_id IS NULL"
        elif prefix_id not in (None, "", "all"):
            prefix_clause = f"r.provider_prefix_id = {placeholder(self.backend)}"
            if where:
                where += " AND " + prefix_clause
            else:
                where = " WHERE " + prefix_clause
            params.append(prefix_id)
        return list(
            self.conn.execute(
                f"""
                SELECT r.*, c.name AS country_name, p.name AS provider_name, pp.prefix AS prefix,
                    (SELECT COUNT(*) FROM route_phone_numbers rpn WHERE rpn.route_id = r.id AND rpn.is_active = {placeholder(self.backend)}) AS phone_count
                FROM routes r
                JOIN countries c ON c.id = r.country_id
                JOIN providers p ON p.id = r.provider_id
                LEFT JOIN provider_prefixes pp ON pp.id = r.provider_prefix_id
                {where}
                ORDER BY c.name, r.name
                """,
                [to_db_bool(True, self.backend), *params],
            )
        )

    def list_phone_numbers(self, filters: dict | None = None) -> list[sqlite3.Row]:
        phone_filters = dict(filters or {})
        supported, normalized_review = self._normalize_optional_bool_filter(phone_filters.get("review_required"))
        if not supported:
            return []
        if normalized_review is None:
            phone_filters.pop("review_required", None)
        else:
            phone_filters["review_required"] = normalized_review

        where, filter_params = query_filters(
            phone_filters,
            {
                "country_id": "pn.country_id",
                "provider_id": "COALESCE(pn.provider_id, 0)",
                "project": "pn.project_label",
                "project_like": "pn.project_label",
                "assignment_type": "pn.assignment_type",
                "status": "pn.status",
                "number_like": "pn.number",
                "review_required": "pn.review_required",
            },
            backend=self.backend,
        )
        p = placeholder(self.backend)
        route_names_expr = (
            f"""
                    COALESCE((
                        SELECT STRING_AGG(r.name, ', ' ORDER BY r.name)
                        FROM route_phone_numbers rpn
                        JOIN routes r ON r.id = rpn.route_id
                        WHERE rpn.phone_number_id = pn.id AND rpn.is_active = {p}
                    ), '') AS route_names"""
            if self.backend == "postgres"
            else f"""
                    COALESCE((
                        SELECT GROUP_CONCAT(ordered_routes.name, ', ')
                        FROM (
                            SELECT r.name
                            FROM route_phone_numbers rpn
                            JOIN routes r ON r.id = rpn.route_id
                            WHERE rpn.phone_number_id = pn.id AND rpn.is_active = {p}
                            ORDER BY r.name
                        ) AS ordered_routes
                    ), '') AS route_names"""
        )
        params = [to_db_bool(True, self.backend), *filter_params]
        return list(
            self.conn.execute(
                f"""
                SELECT pn.*, COALESCE(pn.country_label, c.name) AS country_name, COALESCE(pn.provider_label, p.name) AS provider_name, COALESCE(pn.currency_label, cur.code) AS currency_code,
                    COALESCE(pn.assignment_label, pat.name, pn.assignment_type) AS assignment_type_label,
{route_names_expr}
                FROM phone_numbers pn
                JOIN countries c ON c.id = pn.country_id
                LEFT JOIN providers p ON p.id = pn.provider_id
                LEFT JOIN currencies cur ON cur.id = pn.currency_id
                LEFT JOIN phone_assignment_types pat ON pat.code = pn.assignment_type
                {where}
                ORDER BY pn.number
                """,
                params,
            )
        )

    def route_numbers(self, route_id: int) -> list[sqlite3.Row]:
        p = placeholder(self.backend)
        return list(
            self.conn.execute(
                """
                SELECT rpn.id AS link_id, pn.id AS phone_id, pn.number, pn.status, pn.assignment_type, pn.connection_cost, pn.monthly_fee,
                    pn.outgoing_rate, pn.incoming_rate, pn.comment AS phone_comment, rpn.comment AS link_comment, rpn.usage_type, rpn.is_active
                FROM route_phone_numbers rpn
                JOIN phone_numbers pn ON pn.id = rpn.phone_number_id
                WHERE rpn.route_id = {p}
                  AND rpn.is_active = {p}
                  AND pn.is_active = {p}
                ORDER BY pn.number
                """.format(p=p),
                (route_id, to_db_bool(True, self.backend), to_db_bool(True, self.backend)),
            )
        )

    def add_phone_to_route_by_number(
        self,
        *,
        route_id: int,
        number: str,
        usage_type: str,
        added_by: int,
        comment: str | None = None,
    ) -> PhoneLinkResult:
        normalized = validate_phone_number(number)
        phone = self.conn.execute(
            "SELECT id FROM phone_numbers WHERE number = ? OR normalized_number = ?",
            (normalized, normalized),
        ).fetchone()
        if phone is None:
            raise BusinessRuleError("Номер не найден в справочнике купленных номеров")
        existing = self.conn.execute(
            "SELECT id FROM route_phone_numbers WHERE route_id = ? AND phone_number_id = ? AND is_active = 1",
            (route_id, phone["id"]),
        ).fetchone()
        if existing:
            raise BusinessRuleError("Номер уже добавлен в этот маршрут")
        return self.add_phone_to_route(
            route_id=route_id,
            phone_number_id=int(phone["id"]),
            usage_type=usage_type,
            added_by=added_by,
            comment=comment,
        )

    def remove_phone_links_from_route(self, *, route_id: int, link_ids: list[int], removed_by: int, reason: str | None = None) -> int:
        removed = 0
        for link_id in link_ids:
            link = self.conn.execute(
                "SELECT id, phone_number_id FROM route_phone_numbers WHERE id = ? AND route_id = ? AND is_active = 1",
                (link_id, route_id),
            ).fetchone()
            if not link:
                continue
            self.conn.execute(
                "UPDATE route_phone_numbers SET is_active = 0, removed_at = CURRENT_TIMESTAMP, removed_by = ? WHERE id = ?",
                (removed_by, link_id),
            )
            self.conn.execute(
                "INSERT INTO route_phone_number_history(route_id, phone_number_id, action, changed_by, reason) VALUES (?, ?, 'removed', ?, ?)",
                (route_id, link["phone_number_id"], removed_by, reason),
            )
            self._change_log("route_phone_number", link_id, "route_phone_number.removed", removed_by, summary=reason)
            removed += 1
        self.conn.commit()
        return removed


    def update_phone_number(
        self,
        phone_id: int,
        *,
        country_id: int,
        provider_id: int | None,
        number: str,
        assignment_type: str,
        status: str,
        is_active: bool,
        updated_by: int,
        project_label: str | None = None,
        connection_cost: str | None = None,
        monthly_fee: str | None = None,
        currency_id: int | None = None,
        phone_type: str | None = None,
        tariff_label: str | None = None,
        comment: str | None = None,
        review_required: bool = False,
    ) -> None:
        existing = self.conn.execute("SELECT * FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
        if existing is None:
            raise BusinessRuleError("Phone number not found")
        normalized = validate_phone_number(number)
        old_values = dict(existing)
        requested_active = 1 if is_active else 0
        forced_review_required = 1 if (requested_active == 1 and int(existing["is_active"]) == 0) else 0
        final_review_required = 1 if review_required or forced_review_required else 0
        if provider_id is None and final_review_required == 0:
            raise BusinessRuleError("Нельзя снять флаг проверки, пока не выбран провайдер")
        final_status = normalize_phone_status(status)
        if requested_active == 0 and int(existing["is_active"]) == 1 and existing["status"] == "used":
            final_status = "problem"
        labels = self._phone_snapshot_labels(country_id, provider_id, assignment_type, currency_id)
        self.conn.execute(
            """
            UPDATE phone_numbers
            SET number = ?, normalized_number = ?, country_id = ?, provider_id = ?, country_label = ?, provider_label = ?, project_label = ?,
                assignment_type = ?, assignment_label = ?, status = ?, is_active = ?, connection_cost = ?, monthly_fee = ?,
                currency_id = ?, currency_label = ?, phone_type = ?, tariff_label = ?, comment = ?, review_required = ?,
                deactivated_at = CASE WHEN ? = 0 AND deactivated_at IS NULL THEN CURRENT_TIMESTAMP ELSE deactivated_at END,
                updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                normalized, normalized, country_id, provider_id, labels["country_label"], labels["provider_label"], project_label, assignment_type, labels["assignment_label"], final_status,
                requested_active, connection_cost, monthly_fee, currency_id, labels["currency_label"], phone_type, tariff_label, comment,
                final_review_required, requested_active, updated_by, phone_id,
            ),
        )
        if requested_active == 0:
            links = list(self.conn.execute(
                "SELECT id, route_id, phone_number_id FROM route_phone_numbers WHERE phone_number_id = ? AND is_active = 1",
                (phone_id,),
            ))
            for link in links:
                self.conn.execute(
                    "UPDATE route_phone_numbers SET is_active = 0, removed_at = CURRENT_TIMESTAMP, removed_by = ? WHERE id = ?",
                    (updated_by, link["id"]),
                )
                self.conn.execute(
                    """
                    INSERT INTO route_phone_number_history(route_id, phone_number_id, action, changed_by, old_values, new_values, reason)
                    VALUES (?, ?, 'removed', ?, ?, ?, ?)
                    """,
                    (link["route_id"], link["phone_number_id"], updated_by,
                     json.dumps({"is_active": 1}, ensure_ascii=False),
                     json.dumps({"is_active": 0, "removed_by": updated_by}, ensure_ascii=False),
                     "phone_number.deactivated"),
                )
                self._change_log(
                    "route_phone_number",
                    int(link["id"]),
                    "route_phone_number.removed_by_phone_deactivation",
                    updated_by,
                    old_values={"is_active": 1},
                    new_values={"is_active": 0, "phone_number_id": phone_id},
                    summary="Phone provider deactivation closed active route link",
                )
        new_values = {
            **old_values,
            "number": normalized,
            "country_id": country_id,
            "provider_id": provider_id,
            "project_label": project_label,
            "assignment_type": assignment_type,
            "status": final_status,
            "is_active": requested_active,
            "connection_cost": connection_cost,
            "monthly_fee": monthly_fee,
            "currency_id": currency_id,
            "phone_type": phone_type,
            "tariff_label": tariff_label,
            "comment": comment,
            "review_required": final_review_required,
        }
        self.record_phone_update_history(phone_id, updated_by, old_values, new_values, comment)
        self._change_log(
            "phone_number",
            phone_id,
            "phone_number.updated",
            updated_by,
            old_values={"number": existing["number"], "status": existing["status"], "is_active": existing["is_active"], "review_required": existing["review_required"]},
            new_values={"number": normalized, "status": final_status, "is_active": requested_active, "review_required": final_review_required},
        )
        self.conn.commit()

    def _name_by_id(self, table: str, value: object, display_column: str = "name") -> str:
        if value in (None, ""):
            return "—"
        row = self.conn.execute(f"SELECT {display_column} AS label FROM {table} WHERE id = ?", (value,)).fetchone()
        return str(row["label"]) if row and row["label"] not in (None, "") else str(value)

    def _currency_label(self, value: object) -> str:
        return self._name_by_id("currencies", value, "code")

    def _phone_field_changes(self, old: dict, new: dict) -> list[str]:
        status_labels = {"used": "Используется", "free": "Свободен", "problem": "Проблемный", "unknown": "Неизвестно"}
        specs = [
            ("provider_id", "Провайдер", lambda v: self._name_by_id("providers", v), "default"),
            ("country_id", "GEO", lambda v: self._name_by_id("countries", v), "default"),
            ("project_label", "Проект", _empty_label, "optional_text"),
            ("assignment_type", "Назначение", _empty_label, "optional_text"),
            ("status", "Рабочий статус", lambda v: status_labels.get(str(v), _empty_label(v)), "optional_text"),
            ("is_active", "Активен у провайдера", _bool_label, "bool"),
            ("review_required", "Требует проверки", _bool_label, "bool"),
            ("phone_type", "Тип номера", _empty_label, "optional_text"),
            ("connection_cost", "Стоимость подключения", _empty_label, "money"),
            ("monthly_fee", "Абонентская плата", _empty_label, "money"),
            ("outgoing_rate", "Исходящий тариф", _empty_label, "money"),
            ("incoming_rate", "Входящий тариф", _empty_label, "money"),
            ("currency_id", "Валюта", self._currency_label, "default"),
            ("tariff_label", "Тариф", _empty_label, "optional_text"),
            ("imported_created_by", "Создал в Excel", _empty_label, "optional_text"),
        ]
        changes: list[str] = []
        for key, label, formatter, kind in specs:
            if not _values_equal(old.get(key), new.get(key), kind):
                changes.append(f"{label}: {formatter(old.get(key))} → {formatter(new.get(key))}")
        if not _values_equal(old.get("comment"), new.get("comment"), "optional_text"):
            changes.append(f"Комментарий: {_truncate_history_text(old.get('comment'))} → {_truncate_history_text(new.get('comment'))}")
        return changes

    def _route_field_changes(self, old: dict, new: dict) -> list[str]:
        priority_labels = {"priority": "Приоритетный", "alternative": "Альтернативный", "unknown": "Неизвестно"}
        specs = [
            ("name", "Название маршрута", _empty_label, "optional_text"),
            ("country_id", "GEO", lambda v: self._name_by_id("countries", v), "default"),
            ("provider_id", "Провайдер", lambda v: self._name_by_id("providers", v), "default"),
            ("provider_prefix_id", "Префикс", lambda v: self._name_by_id("provider_prefixes", v, "prefix"), "optional_text"),
            ("project_label", "Проект", _empty_label, "optional_text"),
            ("is_actual", "Активность маршрута", _bool_label, "bool"),
            ("priority_status", "Приоритет", lambda v: priority_labels.get(str(v), _empty_label(v)), "optional_text"),
            ("cli_source_type", "Источник АОН", _empty_label, "optional_text"),
            ("cli_source_label", "Метка АОН", _empty_label, "optional_text"),
            ("aon_pool", "АОН/пул", _empty_label, "optional_text"),
            ("rnd_type", "Тип RND", _empty_label, "optional_text"),
            ("rnd_pool_owner", "Какой пул / принадлежность", _empty_label, "optional_text"),
        ]
        changes: list[str] = []
        for key, label, formatter, kind in specs:
            if not _values_equal(old.get(key), new.get(key), kind):
                changes.append(f"{label}: {formatter(old.get(key))} → {formatter(new.get(key))}")
        if not _values_equal(old.get("comment"), new.get("comment"), "optional_text"):
            changes.append(f"Комментарий: {_truncate_history_text(old.get('comment'))} → {_truncate_history_text(new.get('comment'))}")
        return changes

    def record_phone_update_history(self, phone_id: int, changed_by: int, old_values: dict, new_values: dict, comment: str | None = None) -> None:
        changes = self._phone_field_changes(old_values, new_values)
        if not changes:
            return
        details = "; ".join(changes)
        payload = {"changes": changes, "description": f"Изменено полей: {len(changes)}", "details": details}
        self.conn.execute(
            "INSERT INTO phone_number_history(phone_number_id, action, changed_by, field_name, old_value, new_value, comment) VALUES (?, 'updated', ?, 'changes', ?, ?, ?)",
            (phone_id, changed_by, json.dumps({"changes": changes}, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), comment),
        )

    def update_route(
        self,
        route_id: int,
        *,
        name: str,
        provider_id: int | None = None,
        provider_prefix_id: int | None,
        comment: str | None,
        is_actual: bool,
        priority_status: str,
        updated_by: int,
        cli_source_type: str | None = None,
        cli_source_label: str | None = None,
        aon_pool: str | None = None,
        rnd_type: str | None = None,
        rnd_pool_owner: str | None = None,
        expected_updated_at: str | None = None,
        commit: bool = True,
    ) -> None:
        existing = self.conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        if existing is None:
            raise BusinessRuleError("Route not found")
        old_values = dict(existing)
        final_provider_id = provider_id if provider_id is not None else int(existing["provider_id"])
        final_cli_source_type = cli_source_type if cli_source_type is not None else existing["cli_source_type"]
        final_cli_source_label = cli_source_label if cli_source_label is not None else existing["cli_source_label"]
        update_params = [
            name,
            final_provider_id,
            provider_prefix_id,
            final_cli_source_type,
            final_cli_source_label,
            aon_pool,
            rnd_type,
            rnd_pool_owner,
            comment,
            1 if is_actual else 0,
            priority_status,
            updated_by,
            route_id,
        ]
        token_clause = ""
        if expected_updated_at is not None:
            token_clause = " AND updated_at = ?"
            update_params.append(expected_updated_at)
        cur = self.conn.execute(
            f"""
            UPDATE routes
            SET name = ?, provider_id = ?, provider_prefix_id = ?, cli_source_type = ?, cli_source_label = ?,
                aon_pool = ?, rnd_type = ?, rnd_pool_owner = ?, comment = ?, is_actual = ?,
                priority_status = ?, updated_by = ?,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', MAX(julianday('now'), julianday(updated_at) + (1.0 / 86400000.0)))
            WHERE id = ?{token_clause}
            """,
            tuple(update_params),
        )
        if cur.rowcount == 0:
            if self.conn.execute("SELECT 1 FROM routes WHERE id = ?", (route_id,)).fetchone() is None:
                raise BusinessRuleError("Route not found")
            raise ConcurrencyConflict("Запись была изменена другим пользователем. Обновите страницу и повторите действие.")
        new_values = {**old_values, "name": name, "provider_id": final_provider_id, "provider_prefix_id": provider_prefix_id, "cli_source_type": final_cli_source_type, "cli_source_label": final_cli_source_label, "aon_pool": aon_pool, "rnd_type": rnd_type, "rnd_pool_owner": rnd_pool_owner, "comment": comment, "is_actual": 1 if is_actual else 0, "priority_status": priority_status}
        changes = self._route_field_changes(old_values, new_values)
        if changes:
            payload = {"changes": changes, "description": f"Изменено полей: {len(changes)}", "details": "; ".join(changes)}
            self.conn.execute(
                "INSERT INTO route_history(route_id, action, changed_by, field_name, old_value, new_value, comment) VALUES (?, 'updated', ?, 'changes', ?, ?, ?)",
                (route_id, updated_by, json.dumps({"changes": changes}, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), comment),
            )
        if commit:
            self.conn.commit()

    def find_tariff_by_identity(self, country_id: int, provider_id: int, provider_prefix_id: int | None) -> sqlite3.Row | None:
        p = placeholder(self.backend)
        return self.conn.execute(
            """
            SELECT t.*, c.name AS country_name, p.name AS provider_name, pp.prefix AS prefix
            FROM tariffs t
            JOIN countries c ON c.id = t.country_id
            JOIN providers p ON p.id = t.provider_id
            LEFT JOIN provider_prefixes pp ON pp.id = t.provider_prefix_id
            WHERE t.country_id = {p} AND t.provider_id = {p} AND COALESCE(t.provider_prefix_id, 0) = COALESCE({p}, 0)
            ORDER BY t.id DESC
            LIMIT 1
            """.format(p=p),
            (country_id, provider_id, provider_prefix_id),
        ).fetchone()

    def _duplicate_tariff_message(self, tariff: sqlite3.Row) -> str:
        if tariff["is_current"]:
            return "Такой тариф уже существует"
        prefix = tariff["prefix"] or "без префикса"
        return f"Тариф {tariff['country_name']} / {tariff['provider_name']} / {prefix} уже существует, но неактивен. Активируйте его и обновите цену."

    def create_tariff(
        self,
        *,
        country_id: int,
        provider_id: int,
        provider_currency_id: int,
        price_in_provider_currency: str,
        conversion_rate_to_eur: str,
        conversion_rate_date: str,
        created_by: int,
        provider_prefix_id: int | None = None,
        currency_rate_id: int | None = None,
        priority_status: str = "unknown",
        is_estimated: bool = False,
        comment: str | None = None,
    ) -> int:
        price_value = validate_tariff_price(price_in_provider_currency)
        duplicate = self.find_tariff_by_identity(country_id, provider_id, provider_prefix_id)
        if duplicate is not None:
            raise BusinessRuleError(self._duplicate_tariff_message(duplicate))
        price_eur = eur_price(price_value, conversion_rate_to_eur)
        cur = self.conn.execute(
            """
            INSERT INTO tariffs(
                country_id, provider_id, provider_prefix_id, provider_currency_id,
                price_in_provider_currency, conversion_rate_to_eur, conversion_rate_date,
                currency_rate_id, eur_price, priority_status, is_estimated, comment,
                is_current, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                country_id,
                provider_id,
                provider_prefix_id,
                provider_currency_id,
                str(price_value),
                str(conversion_rate_to_eur),
                conversion_rate_date,
                currency_rate_id,
                str(price_eur),
                priority_status,
                1 if is_estimated else 0,
                comment,
                created_by,
            ),
        )
        tariff_id = int(cur.lastrowid)
        country = self.conn.execute("SELECT name FROM countries WHERE id = ?", (country_id,)).fetchone()
        provider = self.conn.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()
        prefix = None
        if provider_prefix_id:
            prefix = self.conn.execute("SELECT prefix FROM provider_prefixes WHERE id = ?", (provider_prefix_id,)).fetchone()
        self.conn.execute(
            """
            INSERT INTO tariff_change_history(
                tariff_id, changed_by, country_id, country_name_snapshot,
                provider_id, provider_name_snapshot, provider_prefix_id, prefix_snapshot,
                new_provider_currency_id, new_price_in_provider_currency,
                new_conversion_rate_to_eur, new_conversion_rate_date, new_eur_price, comment
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tariff_id,
                created_by,
                country_id,
                country["name"] if country else "",
                provider_id,
                provider["name"] if provider else "",
                provider_prefix_id,
                prefix["prefix"] if prefix else None,
                provider_currency_id,
                str(price_value),
                str(conversion_rate_to_eur),
                conversion_rate_date,
                str(price_eur),
                comment,
            ),
        )
        self._change_log("tariff", tariff_id, "tariff.created", created_by, new_values={"eur_price": str(price_eur)})
        self.conn.commit()
        return tariff_id

    def get_tariff(self, tariff_id: int) -> sqlite3.Row | None:
        p = placeholder(self.backend)
        return self.conn.execute(
            """
            SELECT t.*, c.name AS country_name, p.name AS provider_name, pp.prefix AS prefix, cur.code AS currency_code
            FROM tariffs t
            JOIN countries c ON c.id = t.country_id
            JOIN providers p ON p.id = t.provider_id
            LEFT JOIN provider_prefixes pp ON pp.id = t.provider_prefix_id
            JOIN currencies cur ON cur.id = t.provider_currency_id
            WHERE t.id = {p}
            """.format(p=p),
            (tariff_id,),
        ).fetchone()

    def _insert_tariff_history(self, tariff: sqlite3.Row, changed_by: int, reason: str, details: str | None = None, *, old_currency_id: int | None = None, old_price: object | None = None, old_rate: object | None = None, old_rate_date: str | None = None, old_eur_price: object | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO tariff_change_history(
                tariff_id, changed_by, country_id, country_name_snapshot, provider_id, provider_name_snapshot, provider_prefix_id, prefix_snapshot,
                old_provider_currency_id, new_provider_currency_id, old_price_in_provider_currency, new_price_in_provider_currency,
                old_conversion_rate_to_eur, new_conversion_rate_to_eur, old_conversion_rate_date, new_conversion_rate_date, old_eur_price, new_eur_price, reason, comment
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tariff["id"], changed_by, tariff["country_id"], tariff["country_name"], tariff["provider_id"], tariff["provider_name"], tariff["provider_prefix_id"], tariff["prefix"], old_currency_id, tariff["provider_currency_id"], old_price, tariff["price_in_provider_currency"], old_rate, tariff["conversion_rate_to_eur"], old_rate_date, tariff["conversion_rate_date"], old_eur_price, tariff["eur_price"], reason, details),
        )

    def update_tariff(self, tariff_id: int, *, provider_currency_id: int, price_in_provider_currency: str, conversion_rate_to_eur: str, conversion_rate_date: str, currency_rate_id: int | None, comment: str | None, updated_by: int, is_current: bool | None = None, expected_updated_at: str | None = None) -> bool:
        old = self.get_tariff(tariff_id)
        if old is None:
            raise BusinessRuleError("Тариф не найден")
        if expected_updated_at is not None and old["updated_at"] != expected_updated_at:
            raise ConcurrencyConflict("Запись была изменена другим пользователем. Обновите страницу и повторите действие.")
        price_value = validate_tariff_price(price_in_provider_currency)
        price_eur = eur_price(price_value, conversion_rate_to_eur)
        requested_current = bool(old["is_current"]) if is_current is None else bool(is_current)
        changes = []
        if Decimal(str(old["price_in_provider_currency"])) != price_value:
            changes.append(f"Цена провайдера: {old['price_in_provider_currency']} → {price_value}")
        if int(old["provider_currency_id"]) != int(provider_currency_id):
            old_code = old["currency_code"]
            new_code_row = self.conn.execute("SELECT code FROM currencies WHERE id = ?", (provider_currency_id,)).fetchone()
            changes.append(f"Валюта: {old_code} → {new_code_row['code'] if new_code_row else provider_currency_id}")
        if (old["comment"] or "") != (comment or ""):
            changes.append(f"Комментарий: {old['comment'] or '—'} → {comment or '—'}")
        if bool(old["is_current"]) != requested_current:
            changes.append(f"Активность: {'Да' if old['is_current'] else 'Нет'} → {'Да' if requested_current else 'Нет'}")
        if not changes:
            return False
        update_params = [provider_currency_id, str(price_value), str(conversion_rate_to_eur), conversion_rate_date, currency_rate_id, str(price_eur), comment, 1 if requested_current else 0, updated_by, tariff_id]
        token_clause = ""
        if expected_updated_at is not None:
            token_clause = " AND updated_at = ?"
            update_params.append(expected_updated_at)
        cur = self.conn.execute(
            f"""
            UPDATE tariffs
            SET provider_currency_id = ?, price_in_provider_currency = ?, conversion_rate_to_eur = ?,
                conversion_rate_date = ?, currency_rate_id = ?, eur_price = ?, comment = ?,
                is_current = ?, updated_by = ?,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', MAX(julianday('now'), julianday(updated_at) + (1.0 / 86400000.0)))
            WHERE id = ?{token_clause}
            """,
            tuple(update_params),
        )
        if cur.rowcount == 0:
            if self.conn.execute("SELECT 1 FROM tariffs WHERE id = ?", (tariff_id,)).fetchone() is None:
                raise BusinessRuleError("Тариф не найден")
            raise ConcurrencyConflict("Запись была изменена другим пользователем. Обновите страницу и повторите действие.")
        new = self.get_tariff(tariff_id)
        status_changed = bool(old["is_current"]) != requested_current
        reason = "tariff.changed"
        if status_changed and len(changes) == 1:
            reason = "tariff.activated" if requested_current else "tariff.deactivated"
        details = "; ".join(changes)
        if reason == "tariff.activated":
            details = "Тариф активирован" if len(changes) == 1 else details
        elif reason == "tariff.deactivated":
            details = "Тариф деактивирован" if len(changes) == 1 else details
        self._insert_tariff_history(new, updated_by, reason, details, old_currency_id=old["provider_currency_id"], old_price=old["price_in_provider_currency"], old_rate=old["conversion_rate_to_eur"], old_rate_date=old["conversion_rate_date"], old_eur_price=old["eur_price"])
        self._change_log("tariff", tariff_id, reason, updated_by, old_values={"price": str(old["price_in_provider_currency"]), "currency_id": old["provider_currency_id"], "comment": old["comment"], "is_current": old["is_current"]}, new_values={"price": str(price_value), "currency_id": provider_currency_id, "comment": comment, "is_current": 1 if requested_current else 0}, summary=details)
        self.conn.commit()
        return True

    def set_tariff_active(self, tariff_id: int, *, is_current: bool, changed_by: int) -> None:
        tariff = self.get_tariff(tariff_id)
        if tariff is None:
            raise BusinessRuleError("Тариф не найден")
        if bool(tariff["is_current"]) == is_current:
            return
        self.conn.execute("UPDATE tariffs SET is_current = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (1 if is_current else 0, changed_by, tariff_id))
        updated = self.get_tariff(tariff_id)
        reason = "tariff.activated" if is_current else "tariff.deactivated"
        self._insert_tariff_history(updated, changed_by, reason)
        self._change_log("tariff", tariff_id, reason, changed_by, old_values={"is_current": tariff["is_current"]}, new_values={"is_current": 1 if is_current else 0})
        self.conn.commit()

    def list_tariff_history(self, tariff_id: int) -> list[sqlite3.Row]:
        p = placeholder(self.backend)
        return list(self.conn.execute(
            """
            SELECT tch.*, u.display_name AS user_name
            FROM tariff_change_history tch
            LEFT JOIN users u ON u.id = tch.changed_by
            WHERE tch.tariff_id = {p}
            ORDER BY tch.changed_at DESC, tch.id DESC
            """.format(p=p),
            (tariff_id,),
        ))


    def create_currency_rate(
        self,
        currency_id: int,
        rate_to_eur: str | Decimal | float,
        rate_date: str,
        updated_by: int | None,
        source: str = "manual",
        comment: str | None = None,
        *,
        commit: bool = True,
    ) -> sqlite3.Row | int:
        rate_value = validate_currency_rate(rate_to_eur)
        cur = self.conn.execute(
            """
            INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, source, comment)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (currency_id, str(rate_value), rate_date, updated_by, source, comment),
        )
        if commit:
            self.conn.commit()
        return cur.lastrowid

    def get_currency_rate(self, currency_rate_id: int) -> sqlite3.Row | None:
        p = placeholder(self.backend)
        return self.conn.execute(
            f"""
            SELECT cr.*, c.code AS currency_code
            FROM currency_rates cr
            JOIN currencies c ON c.id = cr.currency_id
            WHERE cr.id = {p}
            """,
            (currency_rate_id,),
        ).fetchone()

    def _currency_rate_log_values(self, rate: sqlite3.Row, currency_code: str) -> dict:
        return {
            "currency_id": int(rate["currency_id"]),
            "currency_code": currency_code,
            "currency_rate_id": int(rate["id"]),
            "rate_to_eur": str(rate["rate_to_eur"]),
            "rate_date": str(rate["rate_date"]),
            "source": rate["source"],
        }

    def recalculate_current_tariffs_for_currency_rate(
        self,
        currency_rate_id: int,
        changed_by: int | None,
    ) -> list[dict]:
        rate = self.get_currency_rate(currency_rate_id)
        if rate is None:
            raise BusinessRuleError("Курс валюты не найден")
        tariffs = self.conn.execute(
            """
            SELECT t.*, c.name AS country_name, p.name AS provider_name, pp.prefix AS prefix, cur.code AS currency_code
            FROM tariffs t
            JOIN countries c ON c.id = t.country_id
            JOIN providers p ON p.id = t.provider_id
            LEFT JOIN provider_prefixes pp ON pp.id = t.provider_prefix_id
            JOIN currencies cur ON cur.id = t.provider_currency_id
            WHERE t.provider_currency_id = ? AND t.is_current = 1
            ORDER BY t.id
            """,
            (rate["currency_id"],),
        ).fetchall()
        recalculated: list[dict] = []
        new_rate_value = validate_currency_rate(rate["rate_to_eur"])
        for tariff in tariffs:
            old_values = {
                "currency_rate_id": tariff["currency_rate_id"],
                "conversion_rate_to_eur": str(tariff["conversion_rate_to_eur"]),
                "conversion_rate_date": str(tariff["conversion_rate_date"]),
                "eur_price": str(tariff["eur_price"]),
            }
            new_eur_price = eur_price(tariff["price_in_provider_currency"], new_rate_value)
            self.conn.execute(
                """
                UPDATE tariffs
                SET conversion_rate_to_eur = ?, conversion_rate_date = ?, currency_rate_id = ?,
                    eur_price = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(new_rate_value), rate["rate_date"], currency_rate_id, str(new_eur_price), changed_by, tariff["id"]),
            )
            updated = self.get_tariff(int(tariff["id"]))
            if updated is None:
                raise BusinessRuleError("Тариф не найден после пересчёта")
            details = (
                f"Пересчёт тарифа из-за обновления курса {rate['currency_code']} к EUR: "
                f"{_clean_number_label(tariff['conversion_rate_to_eur'])} → {_clean_number_label(new_rate_value)}. "
                f"EUR price: {_clean_number_label(tariff['eur_price'])} → {_clean_number_label(new_eur_price)}. "
                f"currency_rate_id: {tariff['currency_rate_id'] or '—'} → {currency_rate_id}."
            )
            self._insert_tariff_history(
                updated,
                changed_by if changed_by is not None else int(updated["created_by"]),
                "tariff.currency_rate_recalculated",
                details,
                old_currency_id=tariff["provider_currency_id"],
                old_price=tariff["price_in_provider_currency"],
                old_rate=tariff["conversion_rate_to_eur"],
                old_rate_date=tariff["conversion_rate_date"],
                old_eur_price=tariff["eur_price"],
            )
            new_values = {
                "currency_rate_id": currency_rate_id,
                "conversion_rate_to_eur": str(new_rate_value),
                "conversion_rate_date": str(rate["rate_date"]),
                "eur_price": str(new_eur_price),
            }
            summary = (
                f"Тариф пересчитан из-за обновления курса {rate['currency_code']} к EUR: "
                f"{_clean_number_label(tariff['eur_price'])} EUR → {_clean_number_label(new_eur_price)} EUR."
            )
            self._change_log(
                "tariff",
                int(tariff["id"]),
                "tariff.currency_rate_recalculated",
                changed_by,
                old_values=old_values,
                new_values=new_values,
                summary=summary,
                source="ui",
            )
            recalculated.append({"tariff_id": int(tariff["id"]), "old_values": old_values, "new_values": new_values})
        return recalculated

    def log_currency_rate_change(
        self,
        currency_rate_id: int,
        currency_id: int,
        currency_code: str,
        old_rate: sqlite3.Row | None,
        new_rate: sqlite3.Row,
        changed_by: int | None,
        source: str = "ui",
        recalculated_active_tariffs_count: int | None = None,
    ) -> None:
        old_values = self._currency_rate_log_values(old_rate, currency_code) if old_rate is not None else None
        new_values = self._currency_rate_log_values(new_rate, currency_code)
        if recalculated_active_tariffs_count is not None:
            new_values["recalculated_active_tariffs_count"] = int(recalculated_active_tariffs_count)
            suffix = (
                f" Активных тарифов пересчитано: {recalculated_active_tariffs_count}."
                if recalculated_active_tariffs_count
                else " Активных тарифов для пересчёта нет."
            )
        else:
            suffix = ""
        if old_rate is not None:
            summary = f"Курс {currency_code} к EUR обновлён вручную: {old_values['rate_to_eur']} → {new_values['rate_to_eur']}.{suffix}"
        else:
            summary = f"Курс {currency_code} к EUR добавлен вручную: {new_values['rate_to_eur']}.{suffix}"
        self._change_log(
            "currency_rate",
            currency_rate_id,
            "currency_rate.manual_created",
            changed_by,
            old_values=old_values,
            new_values=new_values,
            summary=summary,
            source=source,
        )

    def latest_currency_rate(self, currency_id: int) -> sqlite3.Row | None:
        p = placeholder(self.backend)
        return self.conn.execute(
            f"""
            SELECT * FROM currency_rates
            WHERE currency_id = {p}
            ORDER BY rate_date DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (currency_id,),
        ).fetchone()

    def list_tariffs(self, filters: dict | None = None) -> list[sqlite3.Row]:
        filters = dict(filters or {})
        status = filters.pop("status", "active")
        where, params = query_filters(
            filters,
            {
                "country_id": "t.country_id",
                "provider_id": "t.provider_id",
            },
            backend=self.backend,
        )
        clauses = []
        if where:
            clauses.append(where[7:])
        p = placeholder(self.backend)
        if status == "active":
            clauses.append(f"t.is_current = {p}")
            params.append(to_db_bool(True, self.backend))
        elif status == "inactive":
            clauses.append(f"t.is_current = {p}")
            params.append(to_db_bool(False, self.backend))
        final_where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return list(
            self.conn.execute(
                f"""
                SELECT t.*, c.name AS country_name, p.name AS provider_name, pp.prefix AS prefix,
                       cur.code AS currency_code
                FROM tariffs t
                JOIN countries c ON c.id = t.country_id
                JOIN providers p ON p.id = t.provider_id
                LEFT JOIN provider_prefixes pp ON pp.id = t.provider_prefix_id
                JOIN currencies cur ON cur.id = t.provider_currency_id
                {final_where}
                ORDER BY c.name, p.name, COALESCE(pp.prefix, '')
                """,
                params,
            )
        )

    def _normalize_optional_bool_filter(self, value: object) -> tuple[bool, object | None]:
        if value in (None, "", "all"):
            return True, None
        if value is True or value == 1 or value == "1":
            return True, to_db_bool(True, self.backend)
        if value is False or value == 0 or value == "0":
            return True, to_db_bool(False, self.backend)
        return False, None

    def list_calling_companies(self, filters: dict | None = None) -> list[sqlite3.Row]:
        company_filters = dict(filters or {})
        for key in ("has_autorotation", "is_active"):
            supported, normalized = self._normalize_optional_bool_filter(company_filters.get(key))
            if not supported:
                return []
            if normalized is None:
                company_filters.pop(key, None)
            else:
                company_filters[key] = normalized

        current_autorotation_filter = (
            "COALESCE(active_crs.has_autorotation, FALSE)"
            if self.backend == "postgres"
            else "COALESCE(active_crs.has_autorotation, 0)"
        )
        where, params = query_filters(
            company_filters,
            {
                "server_id": "cc.server_id",
                "country_id": "cc.country_id",
                "company_like": "cc.company_name",
                "external_id_like": "cc.company_id_external",
                "has_autorotation": current_autorotation_filter,
                "is_active": "cc.is_active",
            },
            backend=self.backend,
        )
        inactive = to_db_bool(False, self.backend)
        active = to_db_bool(True, self.backend)
        p = placeholder(self.backend)
        active_join = f"active_crs.is_active = {p}"
        return list(
            self.conn.execute(
                f"""
                SELECT cc.*, s.name AS server_name, c.name AS country_name,
                       COALESCE(active_crs.has_autorotation, {p}) AS current_has_autorotation,
                       active_crs.routing_mode AS current_routing_mode, active_crs.route_id AS current_route_id
                FROM calling_companies cc
                JOIN servers s ON s.id = cc.server_id
                JOIN countries c ON c.id = cc.country_id
                LEFT JOIN company_routing_settings active_crs
                  ON active_crs.calling_company_id = cc.id
                 AND {active_join}
                 AND active_crs.valid_to IS NULL
                {where}
                ORDER BY c.name, s.name, cc.company_name
                """,
                [inactive, active, *params],
            )
        )

    def list_calling_company_history(self, company_id: int) -> list[sqlite3.Row]:
        p = placeholder(self.backend)
        routing_company_id_expr = (
            "NULLIF(cl.new_values ->> 'calling_company_id', '')::BIGINT"
            if self.backend == "postgres"
            else "json_extract(cl.new_values, '$.calling_company_id')"
        )
        return list(self.conn.execute(
            f"""
            SELECT cl.changed_at, u.display_name AS user_name, cl.change_type AS action,
                   cl.old_values AS old_value, cl.new_values AS new_value, cl.summary AS comment,
                   cc.company_name AS current_company_name, cc.company_id_external
            FROM change_log cl
            LEFT JOIN users u ON u.id = cl.changed_by
            LEFT JOIN calling_companies cc ON cc.id = CASE
                WHEN cl.entity_type = 'calling_company' THEN cl.entity_id
                ELSE {routing_company_id_expr}
            END
            WHERE (cl.entity_type = 'calling_company' AND cl.entity_id = {p})
               OR (cl.entity_type = 'routing_event' AND {routing_company_id_expr} = {p})
            ORDER BY cl.changed_at DESC, cl.id DESC
            """,
            (company_id, company_id),
        ))

    def _calling_company_event_query_parts(self, search: str | None) -> tuple[str, str, list[object]]:
        """Build the shared, read-only calling-company event JOIN and predicate."""
        p = placeholder(self.backend)
        routing_company_id = (
            "NULLIF(cl.new_values ->> 'calling_company_id', '')::BIGINT"
            if self.backend == "postgres"
            else "CAST(NULLIF(json_extract(cl.new_values, '$.calling_company_id'), '') AS INTEGER)"
        )
        company_join = f"CASE WHEN cl.entity_type = 'calling_company' THEN cl.entity_id ELSE {routing_company_id} END"
        where = f"WHERE (cl.entity_type = 'calling_company' OR (cl.entity_type = 'routing_event' AND {routing_company_id} IS NOT NULL))"
        normalized_search = normalize_search_text(search)
        params: list[object] = []
        if normalized_search is not None:
            fields = (
                f"COALESCE(cc.id, {routing_company_id}, cl.entity_id)",
                "cc.company_id_external", "cc.company_name", "cl.summary", "cl.old_values", "cl.new_values",
            )
            if self.backend == "postgres":
                predicates = [f"POSITION(LOWER(CAST({p} AS TEXT)) IN LOWER(COALESCE(CAST({field} AS TEXT), ''))) > 0" for field in fields]
            else:
                predicates = [f"search_text_matches(CAST({field} AS TEXT), {p}) = 1" for field in fields]
            where += " AND (" + " OR ".join(predicates) + ")"
            params = [normalized_search] * 6
        return company_join, where, params

    def list_calling_company_events(self, *, search: str | None = None, limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
        p = placeholder(self.backend)
        company_join, where, params = self._calling_company_event_query_parts(search)
        params.extend([limit, offset])
        return list(self.conn.execute(
            f"""
            SELECT cl.id, cl.entity_id AS company_id, cl.changed_at, u.display_name AS user_name,
                   cl.change_type AS action, cl.old_values AS old_value, cl.new_values AS new_value,
                   cl.summary AS comment, cc.company_name AS current_company_name, cc.company_id_external
            FROM change_log cl
            LEFT JOIN users u ON u.id = cl.changed_by
            LEFT JOIN calling_companies cc ON cc.id = {company_join}
            {where}
            ORDER BY cl.changed_at DESC, cl.id DESC
            LIMIT {p} OFFSET {p}
            """,
            params,
        ))

    def count_calling_company_events(self, *, search: str | None = None) -> int:
        company_join, where, params = self._calling_company_event_query_parts(search)
        row = self.conn.execute(f"SELECT COUNT(*) AS count FROM change_log cl LEFT JOIN calling_companies cc ON cc.id = {company_join} {where}", params).fetchone()
        values = row_to_dict(row) if row else {}
        return int(values["count"]) if values else 0

    def _validate_company_routing_values(
        self,
        *,
        calling_company_id: int,
        country_id: int,
        server_id: int,
        route_id: int | None,
        routing_mode: str,
        has_autorotation: bool,
    ) -> None:
        if routing_mode not in {"server_priority", "campaign_route", "autorotation", "mixed"}:
            raise BusinessRuleError("Некорректный режим маршрутизации")
        if routing_mode == "campaign_route" and not route_id:
            raise BusinessRuleError("Для режима campaign_route обязателен маршрут")
        if routing_mode == "autorotation" and not has_autorotation:
            raise BusinessRuleError("Для режима autorotation должна быть включена авторотация")
        company = self.conn.execute("SELECT id, country_id, server_id FROM calling_companies WHERE id = ?", (calling_company_id,)).fetchone()
        if not company:
            raise BusinessRuleError("Кампания прозвона не найдена")
        if int(company["country_id"]) != int(country_id):
            raise BusinessRuleError("GEO схемы маршрутизации должен совпадать с GEO выбранной кампании")
        if int(company["server_id"]) != int(server_id):
            raise BusinessRuleError("Сервер схемы маршрутизации должен совпадать с сервером выбранной кампании")
        if not self.conn.execute("SELECT id FROM countries WHERE id = ?", (country_id,)).fetchone():
            raise BusinessRuleError("GEO не найден")
        if not self.conn.execute("SELECT id FROM servers WHERE id = ?", (server_id,)).fetchone():
            raise BusinessRuleError("Сервер не найден")
        if route_id:
            route = self.conn.execute("SELECT id, country_id FROM routes WHERE id = ?", (route_id,)).fetchone()
            if not route:
                raise BusinessRuleError("Маршрут не найден")
            if int(route["country_id"]) != int(country_id):
                raise BusinessRuleError("Маршрут кампании должен относиться к выбранному GEO")

    def _company_routing_summary(
        self,
        *,
        calling_company_id: int,
        country_id: int,
        server_id: int,
        old_values: dict | None = None,
        new_values: dict | None = None,
    ) -> str:
        company = self.conn.execute("SELECT company_id_external, company_name FROM calling_companies WHERE id = ?", (calling_company_id,)).fetchone()
        country = self.conn.execute("SELECT name FROM countries WHERE id = ?", (country_id,)).fetchone()
        server = self.conn.execute("SELECT name FROM servers WHERE id = ?", (server_id,)).fetchone()

        def route_label(route_id: int | None) -> str:
            if not route_id:
                return "—"
            route = self.conn.execute("SELECT name FROM routes WHERE id = ?", (route_id,)).fetchone()
            return f"{route_id} / {route['name']}" if route else str(route_id)

        parts = [
            f"GEO: {country['name'] if country else country_id}",
            f"Сервер: {server['name'] if server else server_id}",
            f"ID кампании: {company['company_id_external'] if company else calling_company_id}",
            f"Кампания: {company['company_name'] if company else calling_company_id}",
        ]
        if old_values:
            parts.extend([
                f"Старый routing_mode: {old_values.get('routing_mode')}",
                f"Старый route: {route_label(old_values.get('route_id'))}",
                f"Старая авторотация: {old_values.get('has_autorotation')}",
            ])
            if old_values.get("valid_to"):
                parts.append(f"valid_to старой версии: {old_values['valid_to']}")
        if new_values:
            parts.extend([
                f"Новый routing_mode: {new_values.get('routing_mode')}",
                f"Новый route: {route_label(new_values.get('route_id'))}",
                f"Новая авторотация: {new_values.get('has_autorotation')}",
            ])
            if new_values.get("valid_from"):
                parts.append(f"valid_from новой версии: {new_values['valid_from']}")
        return "; ".join(parts)

    def list_company_routing_settings(self, filters: dict | None = None) -> list[sqlite3.Row]:
        routing_filters = dict(filters or {})
        include_history_value = routing_filters.pop("include_history", None)
        show_history_value = routing_filters.pop("show_history", None)
        is_active_value = routing_filters.pop("is_active", None)
        company_id_external = routing_filters.pop("company_id_external", None)

        include_supported, include_normalized = self._normalize_optional_bool_filter(include_history_value)
        show_supported, show_normalized = self._normalize_optional_bool_filter(show_history_value)
        if not include_supported or not show_supported:
            return []
        include_history = include_normalized == to_db_bool(True, self.backend) or show_normalized == to_db_bool(True, self.backend)

        if company_id_external not in (None, "", "all"):
            routing_filters["company_id_external_like"] = company_id_external

        where, params = query_filters(
            routing_filters,
            {
                "country_id": "crs.country_id",
                "server_id": "crs.server_id",
                "routing_mode": "crs.routing_mode",
                "calling_company_id": "crs.calling_company_id",
                "company_id_external_like": "cc.company_id_external",
            },
            backend=self.backend,
        )
        clauses: list[str] = []
        if where:
            clauses.extend(where.removeprefix(" WHERE ").split(" AND "))

        p = placeholder(self.backend)
        if include_history:
            supported, normalized_active = self._normalize_optional_bool_filter(is_active_value)
            if not supported:
                return []
            if normalized_active is not None:
                clauses.append(f"crs.is_active = {p}")
                params.append(normalized_active)
        else:
            clauses.insert(0, f"crs.is_active = {p}")
            clauses.insert(1, "crs.valid_to IS NULL")
            params.insert(0, to_db_bool(True, self.backend))

        final_where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return list(
            self.conn.execute(
                f"""
                SELECT crs.*, c.name AS country_name, s.name AS server_name,
                       cc.company_id_external, cc.company_name,
                       r.name AS route_name, p.name AS provider_name,
                       u.username AS updated_by_username
                FROM company_routing_settings crs
                JOIN calling_companies cc ON cc.id = crs.calling_company_id
                JOIN countries c ON c.id = crs.country_id
                JOIN servers s ON s.id = crs.server_id
                LEFT JOIN routes r ON r.id = crs.route_id
                LEFT JOIN providers p ON p.id = r.provider_id
                LEFT JOIN users u ON u.id = crs.updated_by
                {final_where}
                ORDER BY c.name, s.name, cc.company_name, crs.valid_from DESC, crs.id DESC
                """,
                params,
            )
        )


    def get_company_routing_setting(self, setting_id: int) -> sqlite3.Row | None:
        p = placeholder(self.backend)
        return self.conn.execute(
            f"""
            SELECT crs.*, c.name AS country_name, s.name AS server_name,
                   cc.company_id_external, cc.company_name,
                   r.name AS route_name, p.name AS provider_name
            FROM company_routing_settings crs
            JOIN calling_companies cc ON cc.id = crs.calling_company_id
            JOIN countries c ON c.id = crs.country_id
            JOIN servers s ON s.id = crs.server_id
            LEFT JOIN routes r ON r.id = crs.route_id
            LEFT JOIN providers p ON p.id = r.provider_id
            WHERE crs.id = {p}
            """,
            (setting_id,),
        ).fetchone()

    def list_company_routing_setting_history(self, setting_id: int) -> list[sqlite3.Row]:
        setting = self.get_company_routing_setting(setting_id)
        if not setting:
            return []
        p = placeholder(self.backend)
        return list(self.conn.execute(
            f"""
            SELECT re.*, u.display_name AS user_name, c.name AS country_name, s.name AS company_server_name,
                   cc.company_id_external, cc.company_name,
                   old_route.name AS old_route_name, old_provider.name AS old_provider_name,
                   new_route.name AS new_route_name, new_provider.name AS new_provider_name
            FROM routing_events re
            LEFT JOIN users u ON u.id = re.created_by
            LEFT JOIN countries c ON c.id = re.country_id
            LEFT JOIN calling_companies cc ON cc.id = re.calling_company_id
            LEFT JOIN servers s ON s.id = cc.server_id
            LEFT JOIN routes old_route ON old_route.id = re.old_company_route_id
            LEFT JOIN providers old_provider ON old_provider.id = old_route.provider_id
            LEFT JOIN routes new_route ON new_route.id = re.new_company_route_id
            LEFT JOIN providers new_provider ON new_provider.id = new_route.provider_id
            WHERE re.apply_scope = 'campaign_setting'
              AND re.calling_company_id = {p}
              AND re.is_active = {p}
            ORDER BY re.event_at DESC, re.id DESC
            """,
            (setting["calling_company_id"], to_db_bool(True, self.backend)),
        ))

    def create_company_routing_setting(
        self,
        *,
        calling_company_id: int,
        country_id: int,
        server_id: int,
        route_id: int | None,
        routing_mode: str,
        has_autorotation: bool,
        comment: str | None,
        created_by: int,
        effective_at: str | None = None,
    ) -> int:
        self._validate_company_routing_values(
            calling_company_id=calling_company_id,
            country_id=country_id,
            server_id=server_id,
            route_id=route_id,
            routing_mode=routing_mode,
            has_autorotation=has_autorotation,
        )
        if self.conn.execute(
            "SELECT id FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL",
            (calling_company_id,),
        ).fetchone():
            raise BusinessRuleError("У кампании уже есть активная схема маршрутизации")
        now = effective_at or self.conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        cur = self.conn.execute(
            """
            INSERT INTO company_routing_settings(
                calling_company_id, country_id, server_id, route_id, routing_mode,
                has_autorotation, is_active, comment, valid_from, created_by, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (calling_company_id, country_id, server_id, route_id, routing_mode, 1 if has_autorotation else 0, comment, now, created_by, created_by),
        )
        setting_id = int(cur.lastrowid)
        new_values = {
            "routing_mode": routing_mode,
            "route_id": route_id,
            "has_autorotation": 1 if has_autorotation else 0,
            "country_id": country_id,
            "server_id": server_id,
            "valid_from": now,
        }
        self._change_log(
            "company_routing_setting",
            setting_id,
            "company_routing_setting.created",
            created_by,
            new_values=new_values,
            summary=self._company_routing_summary(calling_company_id=calling_company_id, country_id=country_id, server_id=server_id, new_values=new_values),
        )
        self.conn.commit()
        return setting_id

    def update_company_routing_setting_comment(self, *, setting_id: int, comment: str | None, updated_by: int) -> int:
        existing = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (setting_id,)).fetchone()
        if not existing:
            raise BusinessRuleError("Схема маршрутизации кампании не найдена")
        if not existing["is_active"] or existing["valid_to"] is not None:
            raise BusinessRuleError("Можно редактировать только активную схему маршрутизации")
        old_values = {
            "routing_mode": existing["routing_mode"],
            "route_id": existing["route_id"],
            "has_autorotation": existing["has_autorotation"],
            "country_id": existing["country_id"],
            "server_id": existing["server_id"],
            "comment": existing["comment"],
        }
        new_values = {**old_values, "comment": comment}
        self.conn.execute(
            "UPDATE company_routing_settings SET comment = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
            (comment, updated_by, setting_id),
        )
        self._change_log(
            "company_routing_setting",
            setting_id,
            "company_routing_setting.updated",
            updated_by,
            old_values=old_values,
            new_values=new_values,
            summary=self._company_routing_summary(
                calling_company_id=existing["calling_company_id"],
                country_id=existing["country_id"],
                server_id=existing["server_id"],
                old_values=old_values,
                new_values=new_values,
            ),
        )
        self.conn.commit()
        return setting_id

    def update_company_routing_setting(
        self,
        *,
        setting_id: int,
        country_id: int,
        server_id: int,
        route_id: int | None,
        routing_mode: str,
        has_autorotation: bool,
        comment: str | None,
        updated_by: int,
        effective_at: str | None = None,
    ) -> int:
        existing = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (setting_id,)).fetchone()
        if not existing:
            raise BusinessRuleError("Схема маршрутизации кампании не найдена")
        if not existing["is_active"] or existing["valid_to"] is not None:
            raise BusinessRuleError("Можно редактировать только активную схему маршрутизации")
        self._validate_company_routing_values(
            calling_company_id=existing["calling_company_id"],
            country_id=country_id,
            server_id=server_id,
            route_id=route_id,
            routing_mode=routing_mode,
            has_autorotation=has_autorotation,
        )
        new_autorotation = 1 if has_autorotation else 0
        routing_changed = any(
            int(existing[key]) != int(value) if key in {"country_id", "server_id"} else existing[key] != value
            for key, value in {
                "country_id": country_id,
                "server_id": server_id,
                "route_id": route_id,
                "routing_mode": routing_mode,
                "has_autorotation": new_autorotation,
            }.items()
        )
        old_values = {
            "routing_mode": existing["routing_mode"],
            "route_id": existing["route_id"],
            "has_autorotation": existing["has_autorotation"],
            "country_id": existing["country_id"],
            "server_id": existing["server_id"],
            "comment": existing["comment"],
        }
        if not routing_changed:
            self.conn.execute(
                "UPDATE company_routing_settings SET comment = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
                (comment, updated_by, setting_id),
            )
            self._change_log(
                "company_routing_setting",
                setting_id,
                "company_routing_setting.updated",
                updated_by,
                old_values=old_values,
                new_values={**old_values, "comment": comment},
                summary=self._company_routing_summary(
                    calling_company_id=existing["calling_company_id"],
                    country_id=country_id,
                    server_id=server_id,
                    old_values=old_values,
                    new_values={**old_values, "comment": comment},
                ),
            )
            self.conn.commit()
            return setting_id

        now = effective_at or self.conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        self.conn.execute(
            """
            UPDATE company_routing_settings
            SET valid_to = ?, is_active = 0, updated_at = CURRENT_TIMESTAMP, updated_by = ?
            WHERE id = ?
            """,
            (now, updated_by, setting_id),
        )
        cur = self.conn.execute(
            """
            INSERT INTO company_routing_settings(
                calling_company_id, country_id, server_id, route_id, routing_mode,
                has_autorotation, is_active, comment, valid_from, created_by, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (existing["calling_company_id"], country_id, server_id, route_id, routing_mode, new_autorotation, comment, now, updated_by, updated_by),
        )
        new_id = int(cur.lastrowid)
        closed_old_values = {**old_values, "valid_to": now}
        new_values = {
            "routing_mode": routing_mode,
            "route_id": route_id,
            "has_autorotation": new_autorotation,
            "country_id": country_id,
            "server_id": server_id,
            "comment": comment,
            "valid_from": now,
        }
        self._change_log(
            "company_routing_setting",
            new_id,
            "company_routing_setting.version_created",
            updated_by,
            old_values=closed_old_values,
            new_values=new_values,
            summary=self._company_routing_summary(
                calling_company_id=existing["calling_company_id"],
                country_id=country_id,
                server_id=server_id,
                old_values=closed_old_values,
                new_values=new_values,
            ),
        )
        self.conn.commit()
        return new_id

    def deactivate_company_routing_setting(self, *, setting_id: int, updated_by: int, effective_at: str | None = None) -> None:
        existing = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (setting_id,)).fetchone()
        if not existing:
            raise BusinessRuleError("Схема маршрутизации кампании не найдена")
        if not existing["is_active"] or existing["valid_to"] is not None:
            raise BusinessRuleError("Схема маршрутизации уже неактивна")
        now = effective_at or self.conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        self.conn.execute(
            """
            UPDATE company_routing_settings
            SET valid_to = ?, is_active = 0, updated_at = CURRENT_TIMESTAMP, updated_by = ?
            WHERE id = ?
            """,
            (now, updated_by, setting_id),
        )
        old_values = {
            "routing_mode": existing["routing_mode"],
            "route_id": existing["route_id"],
            "has_autorotation": existing["has_autorotation"],
            "country_id": existing["country_id"],
            "server_id": existing["server_id"],
            "comment": existing["comment"],
            "valid_to": now,
        }
        self._change_log(
            "company_routing_setting",
            setting_id,
            "company_routing_setting.deactivated",
            updated_by,
            old_values=old_values,
            summary=self._company_routing_summary(
                calling_company_id=existing["calling_company_id"],
                country_id=existing["country_id"],
                server_id=existing["server_id"],
                old_values=old_values,
            ),
        )
        self.conn.commit()


    ROUTING_EVENT_REASONS_BY_SCOPE = {
        "none": (
            "Обновление/смена АОНов",
            "Провайдер сменил маршрут",
            "Другое",
        ),
        "server_priority": (
            "Массовый отбои/занято",
            "Обратная смена провайдера",
            "Задача руководства",
            "Другое",
        ),
        "campaign_setting": (
            "Задача руководства",
            "Массовые отбои / занято",
            "Плохой дозвон",
            "Провайдер не отвечает",
            "Авария у провайдера",
            "Тест нового маршрута",
            "Плановое переключение",
            "Обновление пула / АОН",
            "Проблема с префиксом",
            "Другое",
        ),
    }
    ROUTING_EVENT_REASONS = ROUTING_EVENT_REASONS_BY_SCOPE["campaign_setting"]

    def _require_text(self, value: str | None, message: str) -> str:
        if not value or not value.strip():
            raise BusinessRuleError(message)
        return value.strip()

    def _name_by_id(self, table: str, row_id: int | None, column: str = "name") -> str | None:
        if not row_id:
            return None
        row = self.conn.execute(f"SELECT {column} FROM {table} WHERE id = ?", (row_id,)).fetchone()
        return row[column] if row else None

    def _route_label(self, route_id: int | None) -> str | None:
        if not route_id:
            return None
        row = self.conn.execute(
            """
            SELECT r.name, p.name AS provider_name
            FROM routes r JOIN providers p ON p.id = r.provider_id
            WHERE r.id = ?
            """,
            (route_id,),
        ).fetchone()
        return f"{row['provider_name']} / {row['name']}" if row else str(route_id)

    def _active_company_routing_setting(self, calling_company_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT *
            FROM company_routing_settings
            WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (calling_company_id,),
        ).fetchone()

    def _company_routing_mode_for_state(self, route_id: int | None, has_autorotation: bool) -> str:
        if route_id and has_autorotation:
            return "mixed"
        if route_id:
            return "campaign_route"
        if has_autorotation:
            return "autorotation"
        return "server_priority"

    def _company_setting_state_requires_active_version(self, route_id: int | None, has_autorotation: bool) -> bool:
        return bool(route_id) or bool(has_autorotation)

    def _company_old_state(self, calling_company_id: int) -> dict:
        setting = self._active_company_routing_setting(calling_company_id)
        if setting:
            return {
                "routing_mode": setting["routing_mode"],
                "route_id": setting["route_id"],
                "has_autorotation": bool(setting["has_autorotation"]),
            }
        return {"routing_mode": "server_priority", "route_id": None, "has_autorotation": False}

    def _routing_event_snapshot(self, values: dict) -> dict:
        company = None
        if values.get("calling_company_id"):
            company = self.conn.execute(
                "SELECT company_id_external, company_name FROM calling_companies WHERE id = ?",
                (values.get("calling_company_id"),),
            ).fetchone()
        snapshot = {
            "country_name": self._name_by_id("countries", values.get("country_id")),
            "server_name": None if values.get("apply_scope") == "campaign_setting" else self._name_by_id("servers", values.get("server_id")),
            "provider_name": self._name_by_id("providers", values.get("provider_id")),
            "affected_route_name": self._route_label(values.get("affected_route_id")),
            "old_route_name": self._route_label(values.get("old_route_id")),
            "new_route_name": self._route_label(values.get("new_route_id")),
            "calling_company_external_id": company["company_id_external"] if company else None,
            "calling_company_name": company["company_name"] if company else None,
            "old_company_routing_mode": values.get("old_company_routing_mode"),
            "new_company_routing_mode": values.get("new_company_routing_mode"),
            "comment": values.get("comment"),
            "reason": values.get("reason"),
            "apply_scope": values.get("apply_scope"),
        }
        if values.get("apply_scope") == "server_priority":
            snapshot["affected_servers"] = values.get("affected_servers", [])
            snapshot["has_overflow"] = values.get("has_overflow", 0)
            snapshot["overflow_route_name"] = self._name_by_id('routes', values.get("overflow_route_id")) if values.get("overflow_route_id") else None
            snapshot["overflow_provider_name"] = None
            if values.get("overflow_route_id"):
                row = self.conn.execute("SELECT p.name FROM routes r JOIN providers p ON p.id = r.provider_id WHERE r.id = ?", (values.get("overflow_route_id"),)).fetchone()
                snapshot["overflow_provider_name"] = row["name"] if row else None
        return snapshot

    def _routing_event_summary(self, values: dict) -> str:
        scope = values.get("apply_scope")
        parts = [
            f"Дата события: {values.get('event_at')}",
            f"Область: {ROUTING_SCOPE_LABELS.get(scope, scope)}",
        ]
        if values.get("country_id"):
            parts.append(f"GEO: {self._name_by_id('countries', values.get('country_id')) or '—'}")
        if scope == "none":
            if values.get("provider_id"):
                parts.append(f"Провайдер: {self._name_by_id('providers', values.get('provider_id'))}")
            if values.get("affected_route_id"):
                parts.append(f"Маршрут/префикс: {self._route_label(values.get('affected_route_id'))}")
        elif scope == "server_priority":
            if values.get("server_id"):
                parts.append(f"Сервер: {self._name_by_id('servers', values.get('server_id'))}")
            parts.append(f"Маршрут: {self._route_label(values.get('old_route_id')) or '—'} → {self._route_label(values.get('new_route_id')) or '—'}")
            if values.get('has_overflow'):
                overflow_provider = None
                if values.get('overflow_route_id'):
                    row = self.conn.execute("SELECT p.name FROM routes r JOIN providers p ON p.id = r.provider_id WHERE r.id = ?", (values.get('overflow_route_id'),)).fetchone()
                    overflow_provider = row["name"] if row else None
                parts.append(f"Перелив: да; Провайдер перелива: {overflow_provider or '—'}; Маршрут перелива: {self._route_label(values.get('overflow_route_id')) or '—'}")
            else:
                parts.append("Перелив: нет")
        elif scope == "campaign_setting":
            if values.get("calling_company_id"):
                company = self.conn.execute("SELECT company_id_external, company_name FROM calling_companies WHERE id = ?", (values.get("calling_company_id"),)).fetchone()
                if company:
                    parts.append(f"Кампания: {company['company_id_external']} / {company['company_name']}")
            if values.get("company_change_type"):
                parts.append(f"Тип изменения кампании: {COMPANY_CHANGE_LABELS.get(values.get('company_change_type'), values.get('company_change_type'))}")
            if values.get("old_company_routing_mode") or values.get("new_company_routing_mode"):
                parts.append(f"Режим кампании: {values.get('old_company_routing_mode') or '—'} → {values.get('new_company_routing_mode') or '—'}")
            if values.get("old_company_route_id") or values.get("new_company_route_id"):
                parts.append(f"Маршрут кампании: {self._route_label(values.get('old_company_route_id')) or '—'} → {self._route_label(values.get('new_company_route_id')) or '—'}")
            if values.get("old_company_has_autorotation") is not None or values.get("new_company_has_autorotation") is not None:
                old_auto = 'Да' if values.get("old_company_has_autorotation") else 'Нет'
                new_auto = 'Да' if values.get("new_company_has_autorotation") else 'Нет'
                parts.append(f"Авторотация: {old_auto} → {new_auto}")
        parts.append(f"Причина: {values.get('reason')}")
        parts.append(f"Комментарий: {values.get('comment')}")
        return "; ".join(parts)


    def _server_priority_apply_summary(self, *, country_id: int, server_id: int, old_route_id: int | None, new_route_id: int, previous_after_update: int | None, comment: str) -> str:
        return "; ".join([
            f"GEO: {self._name_by_id('countries', country_id) or country_id}",
            f"Сервер: {self._name_by_id('servers', server_id) or server_id}",
            f"Старый current route: {self._route_label(old_route_id) or '—'}",
            f"Новый current route: {self._route_label(new_route_id) or new_route_id}",
            f"previous route after update: {self._route_label(previous_after_update) or '—'}",
            f"Комментарий: {comment}",
        ])

    def _normalize_server_priority_server_ids(self, *, server_id: int | None, server_ids) -> list[int]:
        raw_ids = server_ids if server_ids is not None else ([server_id] if server_id else [])
        normalized: list[int] = []
        for raw_id in raw_ids:
            if raw_id in (None, ""):
                continue
            sid = int(raw_id)
            if sid not in normalized:
                normalized.append(sid)
        if not normalized:
            raise BusinessRuleError("Сервер обязателен для серверного приоритета")
        return normalized

    def _server_priority_affected_servers(self, *, country_id: int, server_ids: list[int], new_route_id: int, has_overflow: int, overflow_route_id: int | None) -> list[dict]:
        affected = []
        for server_id in server_ids:
            server = self.conn.execute("SELECT id, name, is_active FROM servers WHERE id = ?", (server_id,)).fetchone()
            if not server:
                raise BusinessRuleError("Сервер не найден")
            if not server["is_active"]:
                raise BusinessRuleError("Нельзя выбрать неактивный сервер")
            current = self.conn.execute(
                "SELECT id, current_route_id, has_overflow, overflow_route_id FROM server_route_priorities WHERE country_id = ? AND server_id = ?",
                (country_id, server_id),
            ).fetchone()
            old_route_id = current["current_route_id"] if current else None
            old_has_overflow = int(current["has_overflow"]) if current else 0
            old_overflow_route_id = current["overflow_route_id"] if current else None
            is_noop = (
                old_route_id is not None
                and int(old_route_id) == int(new_route_id)
                and old_has_overflow == int(has_overflow)
                and (int(old_overflow_route_id) if old_overflow_route_id is not None else None) == (int(overflow_route_id) if overflow_route_id is not None else None)
            )
            affected.append({
                "server_id": server_id,
                "server_name": server["name"],
                "old_route_id": old_route_id,
                "old_route": self._route_label(old_route_id),
                "new_route_id": new_route_id,
                "new_route": self._route_label(new_route_id),
                "server_route_priority_id": current["id"] if current else None,
                "old_has_overflow": old_has_overflow,
                "old_overflow_route_id": old_overflow_route_id,
                "status": "skipped_noop" if is_noop else "applied",
            })
        if all(row["status"] == "skipped_noop" for row in affected):
            raise BusinessRuleError("Выбранный маршрут уже установлен для всех выбранных серверов")
        return affected

    def _upsert_company_routing_setting_from_event(self, values: dict, *, updated_by: int) -> None:
        active = self._active_company_routing_setting(values["calling_company_id"])
        if active is None:
            self.create_company_routing_setting(
                calling_company_id=values["calling_company_id"],
                country_id=values["country_id"],
                server_id=values["server_id"],
                route_id=values["new_company_route_id"],
                routing_mode=values["new_company_routing_mode"],
                has_autorotation=bool(values["new_company_has_autorotation"]),
                comment=values["comment"],
                created_by=updated_by,
                effective_at=values["event_at"],
            )
            return

        self.update_company_routing_setting(
            setting_id=active["id"],
            country_id=values["country_id"],
            server_id=values["server_id"],
            route_id=values["new_company_route_id"],
            routing_mode=values["new_company_routing_mode"],
            has_autorotation=bool(values["new_company_has_autorotation"]),
            comment=values["comment"],
            updated_by=updated_by,
            effective_at=values["event_at"],
        )

    def _deactivate_company_routing_setting_from_event(self, values: dict, *, updated_by: int) -> None:
        active = self._active_company_routing_setting(values["calling_company_id"])
        if active is not None:
            self.deactivate_company_routing_setting(
                setting_id=active["id"],
                updated_by=updated_by,
                effective_at=values["event_at"],
            )

    def _apply_campaign_setting_event(self, values: dict, *, updated_by: int) -> None:
        if self._company_setting_state_requires_active_version(
            values["new_company_route_id"], bool(values["new_company_has_autorotation"])
        ):
            self._upsert_company_routing_setting_from_event(values, updated_by=updated_by)
        else:
            self._deactivate_company_routing_setting_from_event(values, updated_by=updated_by)

    def create_routing_event(self, **kwargs) -> int:
        apply_scope = kwargs.get("apply_scope")
        if apply_scope not in {"none", "server_priority", "campaign_setting"}:
            raise BusinessRuleError("Некорректная область применения")
        values = {
            "event_at": self._require_text(kwargs.get("event_at"), "Дата события обязательна").replace("T", " "),
            "apply_scope": apply_scope,
            "reason": self._require_text(kwargs.get("reason"), "Причина обязательна"),
            "comment": (kwargs.get("comment") or "").strip(),
            "country_id": kwargs.get("country_id"),
            "server_id": kwargs.get("server_id"),
            "server_ids": kwargs.get("server_ids"),
            "provider_id": kwargs.get("provider_id"),
            "affected_route_id": kwargs.get("affected_route_id"),
            "old_route_id": kwargs.get("old_route_id"),
            "new_route_id": kwargs.get("new_route_id"),
            "calling_company_id": kwargs.get("calling_company_id"),
            "company_change_type": kwargs.get("company_change_type"),
            "old_company_routing_mode": kwargs.get("old_company_routing_mode"),
            "new_company_routing_mode": kwargs.get("new_company_routing_mode"),
            "old_company_route_id": kwargs.get("old_company_route_id"),
            "new_company_route_id": kwargs.get("new_company_route_id"),
            "old_company_has_autorotation": kwargs.get("old_company_has_autorotation"),
            "new_company_has_autorotation": kwargs.get("new_company_has_autorotation"),
            "has_overflow": 1 if kwargs.get("has_overflow") else 0,
            "overflow_route_id": kwargs.get("overflow_route_id"),
        }
        created_by = kwargs.get("created_by")
        if not created_by:
            raise BusinessRuleError("Пользователь обязателен")
        allowed_reasons = self.ROUTING_EVENT_REASONS_BY_SCOPE[apply_scope]
        if values["reason"] not in allowed_reasons:
            raise BusinessRuleError("Некорректная причина")
        if apply_scope == "none" and values["reason"] == "Другое" and not values["comment"]:
            raise BusinessRuleError("Требуется понятный комментарий")

        if apply_scope == "none":
            if not values["provider_id"]:
                raise BusinessRuleError("Провайдер обязателен")
            if values["affected_route_id"]:
                route = self.conn.execute("SELECT country_id, provider_id FROM routes WHERE id = ?", (values["affected_route_id"],)).fetchone()
                if not route:
                    raise BusinessRuleError("Маршрут/префикс не найден")
                if int(route["provider_id"]) != int(values["provider_id"]):
                    raise BusinessRuleError("Маршрут/префикс должен относиться к выбранному провайдеру")
                if values["country_id"] and int(route["country_id"]) != int(values["country_id"]):
                    raise BusinessRuleError("Маршрут/префикс должен относиться к выбранному GEO")
            for field in (
                "server_id", "old_route_id", "new_route_id", "calling_company_id", "company_change_type",
                "old_company_routing_mode", "new_company_routing_mode", "old_company_route_id", "new_company_route_id",
                "old_company_has_autorotation", "new_company_has_autorotation", "overflow_route_id",
            ):
                values[field] = None
            values["server_ids"] = None
            values["affected_servers"] = None
            values["has_overflow"] = 0
            values["overflow_route_id"] = None
        elif apply_scope == "server_priority":
            if not values["country_id"] or not values["new_route_id"]:
                raise BusinessRuleError("GEO, сервер и новый маршрут обязательны для серверного приоритета")
            server_ids = self._normalize_server_priority_server_ids(server_id=values["server_id"], server_ids=values.get("server_ids"))
            route = self.conn.execute("SELECT country_id, provider_id FROM routes WHERE id = ?", (values["new_route_id"],)).fetchone()
            if not route:
                raise BusinessRuleError("Новый маршрут не найден")
            if int(route["country_id"]) != int(values["country_id"]):
                raise BusinessRuleError("Новый маршрут должен относиться к выбранному GEO")
            if values["provider_id"] and int(route["provider_id"]) != int(values["provider_id"]):
                raise BusinessRuleError("Новый маршрут должен относиться к выбранному новому провайдеру")
            values["provider_id"] = route["provider_id"]
            values["server_ids"] = server_ids
            values["server_id"] = server_ids[0] if len(server_ids) == 1 else None
            if values["has_overflow"]:
                if not values["overflow_route_id"]:
                    raise BusinessRuleError("Маршрут перелива обязателен")
                if not kwargs.get("overflow_provider_id"):
                    raise BusinessRuleError("Провайдер перелива обязателен")
                overflow = self.conn.execute("SELECT country_id, provider_id, is_actual FROM routes WHERE id = ?", (values["overflow_route_id"],)).fetchone()
                if not overflow or not int(overflow["is_actual"]) or int(overflow["country_id"]) != int(values["country_id"]):
                    raise BusinessRuleError("Маршрут перелива должен быть активным и относиться к выбранному GEO")
                if int(overflow["provider_id"]) != int(kwargs.get("overflow_provider_id")):
                    raise BusinessRuleError("Маршрут перелива должен относиться к выбранному провайдеру перелива")
            else:
                values["has_overflow"] = 0
                values["overflow_route_id"] = None
            values["affected_servers"] = self._server_priority_affected_servers(
                country_id=values["country_id"], server_ids=server_ids, new_route_id=values["new_route_id"],
                has_overflow=values["has_overflow"], overflow_route_id=values["overflow_route_id"]
            )
            values["old_route_id"] = values["affected_servers"][0]["old_route_id"] if len(server_ids) == 1 else None
        else:
            if not values["calling_company_id"] or not values["company_change_type"]:
                raise BusinessRuleError("Кампания и тип изменения обязательны")
            company = self.conn.execute("SELECT country_id, server_id FROM calling_companies WHERE id = ?", (values["calling_company_id"],)).fetchone()
            if not company:
                raise BusinessRuleError("Кампания прозвона не найдена")
            values["country_id"] = values["country_id"] or company["country_id"]
            company_server_id = company["server_id"]
            values["server_id"] = company_server_id
            old_state = self._company_old_state(values["calling_company_id"])
            values["old_company_routing_mode"] = old_state["routing_mode"]
            values["old_company_route_id"] = old_state["route_id"]
            values["old_company_has_autorotation"] = 1 if old_state["has_autorotation"] else 0
            ctype = values["company_change_type"]
            if ctype == "enable_autorotation":
                if old_state["has_autorotation"]:
                    raise BusinessRuleError("В этой компании уже включена авторотация.")
                values["new_company_route_id"] = old_state["route_id"]
                values["new_company_has_autorotation"] = 1
            elif ctype == "disable_autorotation":
                if not old_state["has_autorotation"]:
                    raise BusinessRuleError("В этой компании авторотация уже выключена.")
                values["new_company_route_id"] = old_state["route_id"]
                values["new_company_has_autorotation"] = 0
            elif ctype == "set_campaign_route":
                if not values["new_company_route_id"]:
                    raise BusinessRuleError("Новый маршрут кампании обязателен")
                route = self.conn.execute("SELECT country_id, provider_id FROM routes WHERE id = ?", (values["new_company_route_id"],)).fetchone()
                if not route or int(route["country_id"]) != int(values["country_id"]):
                    raise BusinessRuleError("Маршрут кампании должен относиться к выбранному GEO")
                if values["provider_id"] and int(route["provider_id"]) != int(values["provider_id"]):
                    raise BusinessRuleError("Маршрут кампании должен относиться к выбранному провайдеру")
                if old_state["route_id"] and int(values["new_company_route_id"]) == int(old_state["route_id"]):
                    raise BusinessRuleError("Этот маршрут уже прописан для выбранной компании.")
                if not values["provider_id"]:
                    values["provider_id"] = route["provider_id"]
                values["new_company_has_autorotation"] = values["old_company_has_autorotation"]
            elif ctype == "remove_campaign_route":
                values["new_company_route_id"] = None
                values["new_company_has_autorotation"] = values["old_company_has_autorotation"]
            else:
                raise BusinessRuleError("Некорректный тип изменения кампании")
            values["new_company_routing_mode"] = self._company_routing_mode_for_state(
                values["new_company_route_id"], bool(values["new_company_has_autorotation"])
            )
            values["server_ids"] = None
            values["affected_servers"] = None
            values["has_overflow"] = 0
            values["overflow_route_id"] = None

        values["snapshot_json"] = json.dumps(self._routing_event_snapshot(values), ensure_ascii=False)
        cur = self.conn.execute(
            """
            INSERT INTO routing_events(
                event_at, apply_scope, reason, country_id, server_id, provider_id, affected_route_id,
                old_route_id, new_route_id, calling_company_id, company_change_type,
                old_company_routing_mode, new_company_routing_mode, old_company_route_id, new_company_route_id,
                old_company_has_autorotation, new_company_has_autorotation, has_overflow, overflow_route_id, comment, snapshot_json,
                created_by, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["event_at"], values["apply_scope"], values["reason"], values["country_id"], None if values["apply_scope"] == "campaign_setting" else values["server_id"],
                values["provider_id"], values["affected_route_id"], values["old_route_id"], values["new_route_id"],
                values["calling_company_id"], values["company_change_type"], values["old_company_routing_mode"],
                values["new_company_routing_mode"], values["old_company_route_id"], values["new_company_route_id"],
                values["old_company_has_autorotation"], values["new_company_has_autorotation"], values["has_overflow"], values["overflow_route_id"], values["comment"],
                values["snapshot_json"], created_by, created_by,
            ),
        )
        event_id = int(cur.lastrowid)
        self._change_log("routing_event", event_id, "routing_event.created", created_by, new_values=values, summary=self._routing_event_summary(values))

        if apply_scope == "server_priority":
            for affected in values["affected_servers"]:
                priority_id = affected["server_route_priority_id"]
                previous_after = affected["old_route_id"]
                if affected["status"] == "applied":
                    if priority_id:
                        self.conn.execute(
                            """
                            UPDATE server_route_priorities
                            SET previous_route_id = current_route_id, current_route_id = ?,
                                has_overflow = ?, overflow_route_id = ?, changed_at = ?,
                                changed_by = ?, reason = ?, comment = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (values["new_route_id"], values["has_overflow"], values["overflow_route_id"], values["event_at"], created_by, values["reason"], values["comment"], created_by, priority_id),
                        )
                    else:
                        cur2 = self.conn.execute(
                            """
                            INSERT INTO server_route_priorities(
                                country_id, server_id, current_route_id, previous_route_id, has_overflow,
                                overflow_route_id, changed_at, changed_by, reason, comment, created_by, updated_by
                            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (values["country_id"], affected["server_id"], values["new_route_id"], values["has_overflow"], values["overflow_route_id"], values["event_at"], created_by, values["reason"], values["comment"], created_by, created_by),
                        )
                        priority_id = int(cur2.lastrowid)
                    affected["server_route_priority_id"] = priority_id
                    self._change_log(
                        "server_route_priority",
                        priority_id,
                        "routing_event.applied_to_server_priority",
                        created_by,
                        old_values={"current_route_id": affected["old_route_id"]},
                        new_values={"current_route_id": values["new_route_id"], "previous_route_id": previous_after, "routing_event_id": event_id},
                        summary=self._server_priority_apply_summary(country_id=values["country_id"], server_id=affected["server_id"], old_route_id=affected["old_route_id"], new_route_id=values["new_route_id"], previous_after_update=previous_after, comment=values["comment"]),
                    )
                self.conn.execute(
                    """
                    INSERT INTO routing_event_servers(
                        routing_event_id, server_id, old_route_id, new_route_id, server_route_priority_id, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (event_id, affected["server_id"], affected["old_route_id"], values["new_route_id"], priority_id, affected["status"]),
                )
        elif apply_scope == "campaign_setting":
            self._apply_campaign_setting_event(values, updated_by=created_by)
        self.conn.commit()
        return event_id

    def list_routing_events(self, filters: dict | None = None) -> list[sqlite3.Row]:
        routing_filters = dict(filters or {})
        include_inactive = routing_filters.pop("include_inactive", None)
        date_from = routing_filters.pop("date_from", None)
        date_to = routing_filters.pop("date_to", None)
        server_id = routing_filters.pop("server_id", None)
        campaign_id = routing_filters.pop("campaign_id", None)

        include_supported, include_normalized = self._normalize_optional_bool_filter(include_inactive)
        if not include_supported:
            return []

        p = placeholder(self.backend)
        clauses = []
        filter_params: list = []
        if include_normalized != to_db_bool(True, self.backend):
            clauses.append(f"re.is_active = {p}")
            filter_params.append(to_db_bool(True, self.backend))
        if date_from:
            clauses.append(f"re.event_at >= {p}")
            filter_params.append(date_from)
        if date_to:
            clauses.append(f"re.event_at <= {p}")
            filter_params.append(date_to)

        equality_filters = {
            "country_id": routing_filters.get("country_id"),
            "apply_scope": routing_filters.get("apply_scope"),
            "calling_company_id": routing_filters.get("calling_company_id"),
            "provider_id": routing_filters.get("provider_id"),
        }
        equality_where, equality_params = query_filters(
            equality_filters,
            {
                "country_id": "re.country_id",
                "apply_scope": "re.apply_scope",
                "calling_company_id": "re.calling_company_id",
                "provider_id": "re.provider_id",
            },
            backend=self.backend,
        )
        if equality_where:
            clauses.extend(equality_where.removeprefix(" WHERE ").split(" AND "))
            filter_params.extend(equality_params)

        if server_id not in (None, "", "all"):
            clauses.append(
                f"""
                (
                    (
                        re.apply_scope = 'server_priority'
                        AND (
                            re.server_id = {p}
                            OR EXISTS (
                                SELECT 1
                                FROM routing_event_servers res
                                WHERE res.routing_event_id = re.id
                                  AND res.server_id = {p}
                            )
                        )
                    )
                    OR (re.apply_scope = 'campaign_setting' AND cc.server_id = {p})
                )
                """
            )
            filter_params.extend([server_id, server_id, server_id])
        campaign_where, campaign_params = query_filters(
            {"campaign_id_like": campaign_id},
            {"campaign_id_like": "cc.company_id_external"},
            backend=self.backend,
        )
        if campaign_where:
            clauses.append(campaign_where.removeprefix(" WHERE "))
            filter_params.extend(campaign_params)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        current_flag = to_db_bool(True, self.backend)
        params = [current_flag, current_flag, current_flag, current_flag, *filter_params]
        return list(self.conn.execute(f"""
            SELECT re.*, c.name AS country_name, s.name AS server_name, p.name AS provider_name,
                   ar.name AS affected_route_name, nr.name AS new_route_name, oldr.name AS old_route_name,
                   overflowr.name AS overflow_route_name,
                   oldcr.name AS old_company_route_name, newcr.name AS new_company_route_name,
                   oldcp.name AS old_company_route_provider_name, newcp.name AS new_company_route_provider_name,
                   CASE re.apply_scope
                       WHEN 'campaign_setting' THEN old_company_tariff.eur_price
                       ELSE old_route_tariff.eur_price
                   END AS old_price_eur,
                   CASE re.apply_scope
                       WHEN 'campaign_setting' THEN new_company_tariff.eur_price
                       ELSE new_route_tariff.eur_price
                   END AS new_price_eur,
                   CASE
                       WHEN (CASE re.apply_scope WHEN 'campaign_setting' THEN old_company_tariff.eur_price ELSE old_route_tariff.eur_price END) IS NULL
                         OR (CASE re.apply_scope WHEN 'campaign_setting' THEN new_company_tariff.eur_price ELSE new_route_tariff.eur_price END) IS NULL
                       THEN NULL
                       ELSE (CASE re.apply_scope WHEN 'campaign_setting' THEN new_company_tariff.eur_price ELSE new_route_tariff.eur_price END)
                          - (CASE re.apply_scope WHEN 'campaign_setting' THEN old_company_tariff.eur_price ELSE old_route_tariff.eur_price END)
                   END AS price_delta_eur,
                   cc.company_id_external, cc.company_name, cs.name AS company_server_name,
                   COALESCE(u.display_name, u.username) AS author_name
            FROM routing_events re
            LEFT JOIN countries c ON c.id = re.country_id
            LEFT JOIN servers s ON s.id = re.server_id
            LEFT JOIN providers p ON p.id = re.provider_id
            LEFT JOIN routes ar ON ar.id = re.affected_route_id
            LEFT JOIN routes nr ON nr.id = re.new_route_id
            LEFT JOIN routes oldr ON oldr.id = re.old_route_id
            LEFT JOIN routes overflowr ON overflowr.id = re.overflow_route_id
            LEFT JOIN routes oldcr ON oldcr.id = re.old_company_route_id
            LEFT JOIN routes newcr ON newcr.id = re.new_company_route_id
            LEFT JOIN tariffs old_route_tariff ON old_route_tariff.id = (
                SELECT t.id FROM tariffs t
                WHERE t.country_id = re.country_id AND t.provider_id = oldr.provider_id
                  AND COALESCE(t.provider_prefix_id, 0) = COALESCE(oldr.provider_prefix_id, 0)
                  AND t.is_current = {p}
                ORDER BY t.created_at DESC, t.id DESC LIMIT 1
            )
            LEFT JOIN tariffs new_route_tariff ON new_route_tariff.id = (
                SELECT t.id FROM tariffs t
                WHERE t.country_id = re.country_id AND t.provider_id = nr.provider_id
                  AND COALESCE(t.provider_prefix_id, 0) = COALESCE(nr.provider_prefix_id, 0)
                  AND t.is_current = {p}
                ORDER BY t.created_at DESC, t.id DESC LIMIT 1
            )
            LEFT JOIN tariffs old_company_tariff ON old_company_tariff.id = (
                SELECT t.id FROM tariffs t
                WHERE t.country_id = re.country_id AND t.provider_id = oldcr.provider_id
                  AND COALESCE(t.provider_prefix_id, 0) = COALESCE(oldcr.provider_prefix_id, 0)
                  AND t.is_current = {p}
                ORDER BY t.created_at DESC, t.id DESC LIMIT 1
            )
            LEFT JOIN tariffs new_company_tariff ON new_company_tariff.id = (
                SELECT t.id FROM tariffs t
                WHERE t.country_id = re.country_id AND t.provider_id = newcr.provider_id
                  AND COALESCE(t.provider_prefix_id, 0) = COALESCE(newcr.provider_prefix_id, 0)
                  AND t.is_current = {p}
                ORDER BY t.created_at DESC, t.id DESC LIMIT 1
            )
            LEFT JOIN providers oldcp ON oldcp.id = oldcr.provider_id
            LEFT JOIN providers newcp ON newcp.id = newcr.provider_id
            LEFT JOIN calling_companies cc ON cc.id = re.calling_company_id
            LEFT JOIN servers cs ON cs.id = cc.server_id
            LEFT JOIN users u ON u.id = re.created_by
            {where}
            ORDER BY re.event_at DESC, re.id DESC
        """, params))

    def get_routing_event(self, event_id: int) -> sqlite3.Row | None:
        rows = self.list_routing_events({"include_inactive": True})
        for row in rows:
            if int(row["id"]) == int(event_id):
                data = dict(row)
                if data.get("apply_scope") == "server_priority":
                    p = placeholder(self.backend)
                    server_rows = self.conn.execute(
                        f"""
                        SELECT s.name
                        FROM routing_event_servers res
                        JOIN servers s ON s.id = res.server_id
                        WHERE res.routing_event_id = {p}
                        ORDER BY s.name
                        """,
                        (event_id,),
                    ).fetchall()
                    if server_rows:
                        data["affected_server_names"] = ", ".join(server["name"] for server in server_rows)
                return data
        return None

    def update_routing_event(self, event_id: int, *, updated_by: int, **kwargs) -> None:
        existing = self.conn.execute("SELECT * FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        if not existing:
            raise BusinessRuleError("Событие маршрутизации не найдено")
        updated_at_original = kwargs.get("updated_at_original")
        if updated_at_original is not None and updated_at_original != existing["updated_at"]:
            raise BusinessRuleError("Запись была изменена другим пользователем. Обновите страницу и повторите действие.")
        comment = self._require_text(kwargs.get("comment"), "Комментарий обязателен")
        if comment == existing["comment"]:
            return
        self.conn.execute(
            """
            UPDATE routing_events
            SET comment = ?, updated_by = ?, updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
            """,
            (comment, updated_by, event_id),
        )
        self._change_log(
            "routing_event", event_id, "routing_event.comment_updated", updated_by,
            old_values={"comment": existing["comment"]}, new_values={"comment": comment},
            summary="Комментарий события изменён",
        )
        self._sync_company_routing_comment_from_event(existing, comment=comment, updated_by=updated_by)
        self.conn.commit()

    def _sync_company_routing_comment_from_event(self, event: sqlite3.Row, *, comment: str, updated_by: int) -> None:
        if event["apply_scope"] != "campaign_setting" or event["calling_company_id"] is None:
            return
        active = self._active_company_routing_setting(event["calling_company_id"])
        if active is None:
            return
        later_event = self.conn.execute(
            """
            SELECT id
            FROM routing_events
            WHERE apply_scope = 'campaign_setting'
              AND calling_company_id = ?
              AND is_active = 1
              AND (event_at > ? OR (event_at = ? AND id > ?))
            LIMIT 1
            """,
            (event["calling_company_id"], event["event_at"], event["event_at"], event["id"]),
        ).fetchone()
        if later_event is not None:
            return
        if (
            active["routing_mode"] != event["new_company_routing_mode"]
            or active["route_id"] != event["new_company_route_id"]
            or active["has_autorotation"] != event["new_company_has_autorotation"]
        ):
            return
        self.conn.execute(
            """
            UPDATE company_routing_settings
            SET comment = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (comment, updated_by, active["id"]),
        )

    def deactivate_routing_event(self, event_id: int, *, reason: str, deactivated_by: int) -> None:
        existing = self.conn.execute("SELECT * FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        if not existing:
            raise BusinessRuleError("Событие маршрутизации не найдено")
        if not existing["is_active"]:
            raise BusinessRuleError("Событие уже деактивировано")
        reason = self._require_text(reason, "Причина деактивации обязательна")
        self.conn.execute(
            """
            UPDATE routing_events
            SET is_active = 0, deactivation_reason = ?, deactivated_at = CURRENT_TIMESTAMP,
                deactivated_by = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (reason, deactivated_by, deactivated_by, event_id),
        )
        self._change_log("routing_event", event_id, "routing_event.deactivated", deactivated_by, old_values=dict(existing), new_values={"deactivation_reason": reason}, summary=f"Событие #{event_id} деактивировано. Причина: {reason}")
        self.conn.commit()

    def list_provider_changes(self, filters: dict | None = None) -> list[sqlite3.Row]:
        filters = filters or {}

        def search_clause(column, value):
            where, params = query_filters(
                {"value_like": value},
                {"value_like": column},
                backend=self.backend,
            )
            return where.removeprefix(" WHERE ") if where else None, params

        p = placeholder(self.backend)
        clauses = []
        params: list = []
        if filters.get("date_from"):
            clauses.append(f"pcl.changed_at >= {p}")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            clauses.append(f"pcl.changed_at <= {p}")
            params.append(filters["date_to"])
        if filters.get("country_id"):
            clauses.append(f"pcl.country_id = {p}")
            params.append(filters["country_id"])
        if filters.get("provider_id"):
            clauses.append(f"(pcl.provider_before_id = {p} OR pcl.provider_after_id = {p})")
            params.extend([filters["provider_id"], filters["provider_id"]])
        route_before_clause, route_before_params = search_clause("rb.name", filters.get("route_like"))
        route_after_clause, route_after_params = search_clause("ra.name", filters.get("route_like"))
        if route_before_clause and route_after_clause:
            clauses.append(f"({route_before_clause} OR {route_after_clause})")
            params.extend(route_before_params + route_after_params)
        reason_clause, reason_params = search_clause("pcl.reason_text", filters.get("reason_like"))
        if reason_clause:
            clauses.append(reason_clause)
            params.extend(reason_params)
        if filters.get("user_id"):
            clauses.append(f"pcl.created_by = {p}")
            params.append(filters["user_id"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        if self.backend == "postgres":
            server_names_sql = """
                       (
                           SELECT STRING_AGG(s.name, ', ' ORDER BY s.name)
                           FROM provider_change_log_servers p2
                           JOIN servers s ON s.id = p2.server_id
                           WHERE p2.provider_change_log_id = pcl.id
                       ) AS server_names"""
        else:
            server_names_sql = """
                       (
                           SELECT GROUP_CONCAT(s2.name, ', ')
                           FROM (
                               SELECT s.name
                               FROM provider_change_log_servers p2
                               JOIN servers s ON s.id = p2.server_id
                               WHERE p2.provider_change_log_id = pcl.id
                               ORDER BY s.name
                           ) s2
                       ) AS server_names"""
        return list(
            self.conn.execute(
                f"""
                SELECT pcl.*, c.name AS country_name,
                       pb.name AS provider_before_name, pa.name AS provider_after_name,
                       rb.name AS route_before_name, ra.name AS route_after_name,
                       u.username AS created_by_username,
{server_names_sql}
                FROM provider_change_logs pcl
                JOIN countries c ON c.id = pcl.country_id
                JOIN providers pb ON pb.id = pcl.provider_before_id
                JOIN providers pa ON pa.id = pcl.provider_after_id
                LEFT JOIN routes rb ON rb.id = pcl.route_before_id
                LEFT JOIN routes ra ON ra.id = pcl.route_after_id
                JOIN users u ON u.id = pcl.created_by
                {where}
                ORDER BY pcl.changed_at DESC, pcl.id DESC
                """,
                params,
            )
        )

    def _route_prefix_id(self, route_id: int | None) -> int | None:
        if not route_id:
            return None
        row = self.conn.execute("SELECT provider_prefix_id FROM routes WHERE id = ?", (route_id,)).fetchone()
        return int(row["provider_prefix_id"]) if row and row["provider_prefix_id"] is not None else None

    def _current_tariff(self, country_id: int, provider_id: int, provider_prefix_id: int | None) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM tariffs
            WHERE country_id = ? AND provider_id = ?
              AND COALESCE(provider_prefix_id, 0) = COALESCE(?, 0)
              AND is_current = 1
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (country_id, provider_id, provider_prefix_id),
        ).fetchone()

    def create_provider_change(
        self,
        *,
        changed_at: str,
        country_id: int,
        provider_before_id: int,
        provider_after_id: int,
        created_by: int,
        route_before_id: int | None = None,
        route_after_id: int | None = None,
        provider_prefix_before_id: int | None = None,
        provider_prefix_after_id: int | None = None,
        reason_text: str | None = None,
        comment: str | None = None,
        server_ids: list[int] | None = None,
    ) -> int:
        provider_changed = provider_before_id != provider_after_id
        if not reason_text or not reason_text.strip():
            raise BusinessRuleError("Причина замены обязательна")
        if provider_changed and not server_ids:
            raise BusinessRuleError("Сервер обязателен при смене провайдера")
        for route_id, provider_id, label in (
            (route_before_id, provider_before_id, "Маршрут до"),
            (route_after_id, provider_after_id, "Маршрут после"),
        ):
            if route_id:
                route = self.conn.execute("SELECT provider_id FROM routes WHERE id = ?", (route_id,)).fetchone()
                if route is None:
                    raise BusinessRuleError(f"{label} не найден")
                if int(route["provider_id"]) != int(provider_id):
                    raise BusinessRuleError(f"{label} не принадлежит выбранному провайдеру")
        provider_prefix_before_id = provider_prefix_before_id if provider_prefix_before_id is not None else self._route_prefix_id(route_before_id)
        provider_prefix_after_id = provider_prefix_after_id if provider_prefix_after_id is not None else self._route_prefix_id(route_after_id)
        tariff_before = self._current_tariff(country_id, provider_before_id, provider_prefix_before_id)
        tariff_after = self._current_tariff(country_id, provider_after_id, provider_prefix_after_id)
        price_delta_eur = None
        if tariff_before and tariff_after:
            price_delta_eur = eur_price(tariff_after["eur_price"], "1") - eur_price(tariff_before["eur_price"], "1")
        cur = self.conn.execute(
            """
            INSERT INTO provider_change_logs(
                changed_at, country_id, route_before_id, provider_before_id, provider_prefix_before_id,
                tariff_before_id, price_before_provider_currency_id, price_before_in_provider_currency,
                price_before_conversion_rate_to_eur, price_before_conversion_rate_date, price_before_eur,
                route_after_id, provider_after_id, provider_prefix_after_id,
                tariff_after_id, price_after_provider_currency_id, price_after_in_provider_currency,
                price_after_conversion_rate_to_eur, price_after_conversion_rate_date, price_after_eur,
                price_delta_eur, provider_changed, reason_text, comment, telegram_status, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'not_sent', ?)
            """,
            (
                changed_at,
                country_id,
                route_before_id,
                provider_before_id,
                provider_prefix_before_id,
                tariff_before["id"] if tariff_before else None,
                tariff_before["provider_currency_id"] if tariff_before else None,
                tariff_before["price_in_provider_currency"] if tariff_before else None,
                tariff_before["conversion_rate_to_eur"] if tariff_before else None,
                tariff_before["conversion_rate_date"] if tariff_before else None,
                tariff_before["eur_price"] if tariff_before else None,
                route_after_id,
                provider_after_id,
                provider_prefix_after_id,
                tariff_after["id"] if tariff_after else None,
                tariff_after["provider_currency_id"] if tariff_after else None,
                tariff_after["price_in_provider_currency"] if tariff_after else None,
                tariff_after["conversion_rate_to_eur"] if tariff_after else None,
                tariff_after["conversion_rate_date"] if tariff_after else None,
                tariff_after["eur_price"] if tariff_after else None,
                str(price_delta_eur) if price_delta_eur is not None else None,
                1 if provider_changed else 0,
                reason_text,
                comment,
                created_by,
            ),
        )
        change_id = int(cur.lastrowid)
        for server_id in server_ids or []:
            self.conn.execute(
                "INSERT INTO provider_change_log_servers(provider_change_log_id, server_id) VALUES (?, ?)",
                (change_id, server_id),
            )
            if provider_changed and route_after_id:
                existing = self.conn.execute(
                    "SELECT id, current_route_id FROM server_route_priorities WHERE country_id = ? AND server_id = ?",
                    (country_id, server_id),
                ).fetchone()
                if existing:
                    self.conn.execute(
                        """
                        UPDATE server_route_priorities
                        SET previous_route_id = current_route_id,
                            current_route_id = ?, has_overflow = 0, overflow_route_id = NULL,
                            provider_change_log_id = ?, changed_at = CURRENT_TIMESTAMP,
                            changed_by = ?, reason = ?, comment = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (route_after_id, change_id, created_by, reason_text, comment, created_by, existing["id"]),
                    )
                else:
                    self.conn.execute(
                        """
                        INSERT INTO server_route_priorities(
                            country_id, server_id, current_route_id, provider_change_log_id,
                            changed_at, changed_by, reason, comment, created_by
                        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
                        """,
                        (country_id, server_id, route_after_id, change_id, created_by, reason_text, comment, created_by),
                    )
        self._change_log(
            "provider_change_log",
            change_id,
            "provider_change_log.created",
            created_by,
            new_values={"provider_changed": provider_changed, "server_ids": server_ids or []},
        )
        self.conn.commit()
        return change_id

    def _server_route_priority_summary(
        self,
        *,
        country_id: int,
        server_id: int,
        old_values: dict,
        new_values: dict,
    ) -> str:
        country = self.conn.execute("SELECT name FROM countries WHERE id = ?", (country_id,)).fetchone()
        server = self.conn.execute("SELECT name FROM servers WHERE id = ?", (server_id,)).fetchone()

        def route_details(route_id: int | None) -> tuple[str, str, str]:
            if not route_id:
                return "—", "—", "—"
            route = self.conn.execute(
                """
                SELECT r.name AS route_name, p.name AS provider_name
                FROM routes r
                JOIN providers p ON p.id = r.provider_id
                WHERE r.id = ?
                """,
                (route_id,),
            ).fetchone()
            if not route:
                return str(route_id), str(route_id), "—"
            return str(route_id), route["route_name"], route["provider_name"]

        _, old_route, old_provider = route_details(old_values.get("current_route_id"))
        _, new_route, new_provider = route_details(new_values.get("current_route_id"))
        previous_route_id, previous_route, _ = route_details(new_values.get("previous_route_id"))
        previous_label = previous_route if previous_route_id == "—" else f"{previous_route_id} / {previous_route}"
        parts = [
            f"GEO: {country['name'] if country else country_id}",
            f"Сервер: {server['name'] if server else server_id}",
            f"Старый current route: {old_route}",
            f"Старый provider: {old_provider}",
            f"Новый current route: {new_route}",
            f"Новый provider: {new_provider}",
            f"Previous route после изменения: {previous_label}",
        ]
        if new_values.get("comment"):
            parts.append(f"Комментарий: {new_values['comment']}")
        return "; ".join(parts)

    def update_server_route_priority(
        self,
        *,
        priority_id: int,
        current_route_id: int,
        comment: str | None,
        changed_by: int,
    ) -> None:
        existing = self.conn.execute(
            """
            SELECT id, country_id, server_id, current_route_id, previous_route_id, comment
            FROM server_route_priorities
            WHERE id = ?
            """,
            (priority_id,),
        ).fetchone()
        if not existing:
            raise BusinessRuleError("Приоритет по серверу не найден")

        route = self.conn.execute(
            "SELECT id, country_id FROM routes WHERE id = ?",
            (current_route_id,),
        ).fetchone()
        if not route:
            raise BusinessRuleError("Маршрут не найден")
        if int(route["country_id"]) != int(existing["country_id"]):
            raise BusinessRuleError("Маршрут должен принадлежать GEO приоритета")

        old_values = {
            "current_route_id": existing["current_route_id"],
            "previous_route_id": existing["previous_route_id"],
            "comment": existing["comment"],
        }
        route_changed = int(existing["current_route_id"]) != int(current_route_id)
        if route_changed:
            self.conn.execute(
                """
                UPDATE server_route_priorities
                SET previous_route_id = current_route_id,
                    current_route_id = ?,
                    changed_at = CURRENT_TIMESTAMP,
                    changed_by = ?,
                    comment = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    updated_by = ?
                WHERE id = ?
                """,
                (current_route_id, changed_by, comment, changed_by, priority_id),
            )
        else:
            self.conn.execute(
                """
                UPDATE server_route_priorities
                SET changed_at = CURRENT_TIMESTAMP,
                    changed_by = ?,
                    comment = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    updated_by = ?
                WHERE id = ?
                """,
                (changed_by, comment, changed_by, priority_id),
            )
        new_values = {
            "current_route_id": current_route_id,
            "previous_route_id": existing["current_route_id"] if route_changed else existing["previous_route_id"],
            "comment": comment,
        }
        self._change_log(
            "server_route_priority",
            priority_id,
            "server_route_priority.current_route_updated",
            changed_by,
            old_values=old_values,
            new_values=new_values,
            summary=self._server_route_priority_summary(
                country_id=existing["country_id"],
                server_id=existing["server_id"],
                old_values=old_values,
                new_values=new_values,
            ),
        )
        self.conn.commit()

    def list_countries(self) -> list[dict]:
        return rows_to_dicts(self.conn.execute("SELECT * FROM countries ORDER BY name"))

    def get_country(self, country_id: int) -> dict | None:
        p = placeholder(self.backend)
        row = self.conn.execute(f"SELECT * FROM countries WHERE id = {p}", (country_id,)).fetchone()
        return row_to_dict(row)

    def list_countries_by_ids(self, country_ids: list[int]) -> list[dict]:
        clause, params = build_in_clause("id", country_ids, self.backend)
        return rows_to_dicts(
            self.conn.execute(
                f"SELECT * FROM countries WHERE {clause} ORDER BY name",
                params,
            )
        )

    def get_country_by_name(self, name: str) -> dict | None:
        p = placeholder(self.backend)
        row = self.conn.execute(f"SELECT id, name, code, is_active FROM countries WHERE name = {p}", (name,)).fetchone()
        return row_to_dict(row)

    def get_provider_by_normalized_name(self, normalized_name: str) -> dict | None:
        p = placeholder(self.backend)
        row = self.conn.execute(f"SELECT id, name, normalized_name, is_active FROM providers WHERE normalized_name = {p}", (normalized_name,)).fetchone()
        return row_to_dict(row)

    def get_currency_by_code(self, code: str) -> dict | None:
        p = placeholder(self.backend)
        row = self.conn.execute(f"SELECT id, code, name, is_active FROM currencies WHERE code = {p}", (code,)).fetchone()
        return row_to_dict(row)

    def get_project_by_name(self, name: str) -> dict | None:
        p = placeholder(self.backend)
        row = self.conn.execute(f"SELECT id, name, is_active FROM projects WHERE name = {p}", (name,)).fetchone()
        return row_to_dict(row)

    def get_phone_number_type_by_name(self, name: str) -> dict | None:
        p = placeholder(self.backend)
        row = self.conn.execute(f"SELECT id, name, is_active FROM phone_number_types WHERE name = {p}", (name,)).fetchone()
        return row_to_dict(row)

    def get_phone_assignment_type_by_code_or_name(self, value: str) -> dict | None:
        p = placeholder(self.backend)
        row = self.conn.execute(
            f"SELECT id, code, name, is_active FROM phone_assignment_types WHERE code = {p} OR name = {p}",
            (value, value),
        ).fetchone()
        return row_to_dict(row)

    def ensure_project_exists(self, name: str, commit: bool = True) -> int:
        sql = insert_ignore_statement("projects", ["name", "is_active"], ["name"], self.backend)
        try:
            cur = self.conn.execute(sql, (name, to_db_bool(True, self.backend)))
            if commit:
                self.conn.commit()
            return int(cur.rowcount)
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def ensure_phone_number_type_exists(self, name: str, commit: bool = True) -> int:
        sql = insert_ignore_statement("phone_number_types", ["name", "is_active"], ["name"], self.backend)
        try:
            cur = self.conn.execute(sql, (name, to_db_bool(True, self.backend)))
            if commit:
                self.conn.commit()
            return int(cur.rowcount)
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def ensure_phone_assignment_type_exists(self, code: str, name: str | None = None, commit: bool = True) -> int:
        sql = insert_ignore_statement("phone_assignment_types", ["code", "name", "is_active"], ["code"], self.backend)
        try:
            cur = self.conn.execute(sql, (code, name if name is not None else code, to_db_bool(True, self.backend)))
            if commit:
                self.conn.commit()
            return int(cur.rowcount)
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def get_server_by_name(self, name: str) -> dict | None:
        p = placeholder(self.backend)
        row = self.conn.execute(f"SELECT id, name, is_active FROM servers WHERE name = {p}", (name,)).fetchone()
        return row_to_dict(row)


    def route_exists_by_country_name_and_name(self, country_name: str, route_name: str) -> bool:
        p = placeholder(self.backend)
        row = self.conn.execute(
            f"""
            SELECT 1 FROM routes r JOIN countries c ON c.id = r.country_id
            WHERE c.name = {p} AND r.name = {p}
            """,
            (country_name, route_name),
        ).fetchone()
        return row is not None

    def update_route_import_fields(
        self,
        *,
        country_id: int,
        name: str,
        provider_id: int,
        provider_prefix_id: int | None,
        project_label: str | None,
        cli_source_type: str,
        cli_source_label: str,
        comment: str | None,
        updated_by: int,
        commit: bool = True,
    ) -> int:
        p = placeholder(self.backend)
        cursor = self.conn.execute(
            f"""
            UPDATE routes
            SET provider_id = {p}, provider_prefix_id = {p}, project_label = {p},
                cli_source_type = {p}, cli_source_label = {p}, comment = {p},
                updated_by = {p}, updated_at = CURRENT_TIMESTAMP
            WHERE country_id = {p} AND name = {p}
            """,
            (
                provider_id,
                provider_prefix_id,
                project_label,
                cli_source_type,
                cli_source_label,
                comment,
                updated_by,
                country_id,
                name,
            ),
        )
        if commit:
            self.conn.commit()
        return int(cursor.rowcount)

    def phone_number_exists_by_normalized_number(self, normalized_number: str) -> bool:
        p = placeholder(self.backend)
        row = self.conn.execute(
            f"SELECT 1 FROM phone_numbers WHERE normalized_number = {p}",
            (normalized_number,),
        ).fetchone()
        return row is not None

    def get_phone_number_import_identity_by_normalized_number(self, normalized_number: str) -> dict | None:
        p = placeholder(self.backend)
        row = self.conn.execute(
            f"SELECT id, imported_created_by, review_required, deactivated_at FROM phone_numbers WHERE normalized_number = {p}",
            (normalized_number,),
        ).fetchone()
        return row_to_dict(row)

    def update_phone_number_import_fields_with_history(
        self,
        *,
        normalized_number: str,
        phone_number_id: int,
        country_id: int,
        provider_id: int | None,
        project_label: str | None,
        assignment_type: str | None,
        status: str,
        is_active: bool,
        connection_cost: str | None,
        monthly_fee: str | None,
        outgoing_rate: str | None,
        incoming_rate: str | None,
        currency_id: int,
        phone_type: str | None,
        tariff_label: str | None,
        comment: str | None,
        review_required: bool,
        imported_created_by: str | None,
        deactivated_at: str | None,
        updated_by: int,
        history_changed_by: int,
        history_new_value: str,
        history_comment: str,
        commit: bool = True,
    ) -> int:
        p = placeholder(self.backend)
        cursor = self.conn.execute(
            f"""
            UPDATE phone_numbers
            SET country_id = {p}, provider_id = {p}, project_label = {p}, assignment_type = {p},
                status = {p}, is_active = {p}, connection_cost = {p}, monthly_fee = {p},
                outgoing_rate = {p}, incoming_rate = {p}, currency_id = {p}, phone_type = {p},
                tariff_label = {p}, comment = {p}, review_required = {p},
                imported_created_by = {p}, deactivated_at = {p}, updated_by = {p},
                updated_at = CURRENT_TIMESTAMP
            WHERE normalized_number = {p}
            """,
            (
                country_id, provider_id, project_label, assignment_type, status,
                to_db_bool(is_active, self.backend), connection_cost, monthly_fee,
                outgoing_rate, incoming_rate, currency_id, phone_type, tariff_label,
                comment, to_db_bool(review_required, self.backend), imported_created_by,
                deactivated_at, updated_by, normalized_number,
            ),
        )
        rowcount = int(cursor.rowcount)
        if rowcount:
            self.conn.execute(
                f"""
                INSERT INTO phone_number_history(
                    phone_number_id, action, changed_by, field_name, old_value, new_value, comment
                ) VALUES ({p}, 'updated', {p}, 'import', NULL, {p}, {p})
                """,
                (phone_number_id, history_changed_by, history_new_value, history_comment),
            )
        if commit:
            self.conn.commit()
        return rowcount

    def calling_company_exists_by_server_country_external_id(self, server_name: str, country_name: str, external_id: str) -> bool:
        p = placeholder(self.backend)
        row = self.conn.execute(
            f"""
            SELECT 1 FROM calling_companies cc
            JOIN servers s ON s.id = cc.server_id
            JOIN countries c ON c.id = cc.country_id
            WHERE s.name = {p} AND c.name = {p} AND cc.company_id_external = {p}
            """,
            (server_name, country_name, external_id),
        ).fetchone()
        return row is not None

    def update_calling_company_import_fields(
        self,
        *,
        server_id: int,
        country_id: int,
        company_id_external: str,
        company_name: str,
        has_autorotation: bool,
        comment: str | None,
        is_active: bool,
        updated_by: int,
        commit: bool = True,
    ) -> int:
        p = placeholder(self.backend)
        cursor = self.conn.execute(
            f"""
            UPDATE calling_companies
            SET company_name = {p}, has_autorotation = {p}, comment = {p}, is_active = {p},
                updated_by = {p}, updated_at = CURRENT_TIMESTAMP
            WHERE server_id = {p} AND country_id = {p} AND company_id_external = {p}
            """,
            (
                company_name,
                to_db_bool(has_autorotation, self.backend),
                comment,
                to_db_bool(is_active, self.backend),
                updated_by,
                server_id,
                country_id,
                company_id_external,
            ),
        )
        if commit:
            self.conn.commit()
        return int(cursor.rowcount)

    def current_tariff_exists_by_country_provider_prefix(self, country_name: str, provider_name: str, prefix: str | None) -> bool:
        p = placeholder(self.backend)
        row = self.conn.execute(
            f"""
            SELECT 1 FROM tariffs t
            JOIN countries c ON c.id = t.country_id
            JOIN providers p ON p.id = t.provider_id
            LEFT JOIN provider_prefixes pp ON pp.id = t.provider_prefix_id
            WHERE t.is_current = {p} AND c.name = {p} AND p.name = {p} AND COALESCE(pp.prefix, '') = COALESCE({p}, '')
            """,
            (to_db_bool(True, self.backend), country_name, provider_name, prefix or None),
        ).fetchone()
        return row is not None

    def list_currencies(self) -> list[dict]:
        return rows_to_dicts(self.conn.execute("SELECT * FROM currencies ORDER BY code"))

    def list_providers(self) -> list[dict]:
        return rows_to_dicts(self.conn.execute("SELECT * FROM providers ORDER BY name"))

    def list_projects(self) -> list[dict]:
        return rows_to_dicts(self.conn.execute("SELECT * FROM projects ORDER BY sort_order, name"))

    def list_servers(self) -> list[dict]:
        return rows_to_dicts(self.conn.execute("SELECT * FROM servers ORDER BY name"))

    def list_phone_number_types(self) -> list[dict]:
        return rows_to_dicts(self.conn.execute("SELECT * FROM phone_number_types ORDER BY name"))

    def list_phone_assignment_types(self) -> list[dict]:
        return rows_to_dicts(self.conn.execute("SELECT * FROM phone_assignment_types ORDER BY sort_order, name"))

    def list_provider_prefixes(self, provider_id: int | None = None) -> list[dict]:
        p = placeholder(self.backend)
        if provider_id is None:
            rows = self.conn.execute("SELECT * FROM provider_prefixes ORDER BY prefix")
        else:
            rows = self.conn.execute(f"SELECT * FROM provider_prefixes WHERE provider_id = {p} ORDER BY prefix", (provider_id,))
        return rows_to_dicts(rows)

    def list_provider_prefixes_with_provider(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT pp.*, p.name AS provider_name
                FROM provider_prefixes pp JOIN providers p ON p.id = pp.provider_id
                ORDER BY p.name, COALESCE(pp.prefix, '')
                """
            )
        )

    def dictionary_counts(self) -> dict[str, int]:
        count_queries = {
            "countries": "SELECT COUNT(*) AS count FROM countries",
            "providers": "SELECT COUNT(*) AS count FROM providers",
            "currencies": "SELECT COUNT(*) AS count FROM currencies",
            "prefixes": "SELECT COUNT(*) AS count FROM provider_prefixes",
            "servers": "SELECT COUNT(*) AS count FROM servers",
            "phone-types": "SELECT COUNT(*) AS count FROM phone_number_types",
            "projects": "SELECT COUNT(*) AS count FROM projects",
            "phone-assignments": "SELECT COUNT(*) AS count FROM phone_assignment_types",
        }
        counts = {}
        for key, sql in count_queries.items():
            row = row_to_dict(self.conn.execute(sql).fetchone())
            counts[key] = int(row["count"])
        return counts

    def list_providers_with_currency(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT p.*, c.code AS currency_code
                FROM providers p
                LEFT JOIN currencies c ON c.id = p.default_currency_id
                ORDER BY p.name
                """
            )
        )

    def list_change_reasons(self) -> list[dict]:
        return rows_to_dicts(self.conn.execute("SELECT * FROM change_reasons ORDER BY is_active DESC, name"))

    def list_active_change_reasons(self) -> list[dict]:
        p = placeholder(self.backend)
        return rows_to_dicts(
            self.conn.execute(
                f"SELECT * FROM change_reasons WHERE is_active = {p} ORDER BY name",
                (to_db_bool(True, self.backend),),
            )
        )

    def create_change_reason(
        self,
        name: str,
        created_by: int | None = None,
        comment: str | None = None,
        is_active: bool = True,
        *,
        commit: bool = True,
    ) -> int:
        p = placeholder(self.backend)
        sql = prepare_insert_returning_id(
            f"INSERT INTO change_reasons(name, description, is_active) VALUES ({p}, {p}, {p})",
            self.backend,
        )
        try:
            cur = self.conn.execute(sql, (name.strip(), comment, to_db_bool(is_active, self.backend)))
            reason_id = extract_inserted_id(cur, self.backend)
            self._change_log("change_reason", reason_id, "change_reason.created", created_by, new_values={"name": name.strip()})
            if commit:
                self.conn.commit()
            return int(reason_id)
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def get_or_create_country(self, name: str, *, commit: bool = True) -> int:
        param = placeholder(self.backend)
        row = self.conn.execute(f"SELECT id FROM countries WHERE name = {param}", (name,)).fetchone()
        return int(row["id"]) if row else self.create_country(name, commit=commit)

    def get_or_create_currency(self, code: str, *, commit: bool = True) -> int:
        param = placeholder(self.backend)
        row = self.conn.execute(f"SELECT id FROM currencies WHERE code = {param}", (code,)).fetchone()
        return int(row["id"]) if row else self.create_currency(code, code, commit=commit)

    def get_or_create_provider(self, name: str, currency_id: int | None = None, *, commit: bool = True) -> int:
        normalized = normalize_provider_name(name)
        param = placeholder(self.backend)
        row = self.conn.execute(f"SELECT id FROM providers WHERE normalized_name = {param}", (normalized,)).fetchone()
        return int(row["id"]) if row else self.create_provider(name, default_currency_id=currency_id, commit=commit)

    def get_or_create_prefix(self, provider_id: int, prefix: str | None, *, commit: bool = True) -> int | None:
        if is_no_prefix_text(prefix):
            return None
        normalized_prefix = normalize_real_prefix(prefix)
        param = placeholder(self.backend)
        row = self.conn.execute(
            f"SELECT id FROM provider_prefixes WHERE provider_id = {param} AND COALESCE(prefix, '') = COALESCE({param}, '')",
            (provider_id, normalized_prefix),
        ).fetchone()
        return int(row["id"]) if row else self.create_prefix(provider_id, normalized_prefix, commit=commit)

    def _change_log(
        self,
        entity_type: str,
        entity_id: int,
        change_type: str,
        changed_by: int | None,
        *,
        old_values: dict | None = None,
        new_values: dict | None = None,
        summary: str | None = None,
        source: str = "ui",
    ) -> None:
        p = placeholder(self.backend)
        self.conn.execute(
            f"""
            INSERT INTO change_log(entity_type, entity_id, change_type, changed_by, old_values, new_values, summary, source)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
            """,
            (
                entity_type,
                entity_id,
                change_type,
                changed_by,
                json.dumps(old_values, ensure_ascii=False) if old_values is not None else None,
                json.dumps(new_values, ensure_ascii=False) if new_values is not None else None,
                summary,
                source,
            ),
        )
