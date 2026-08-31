from pathlib import Path
import py_compile


ROOT = Path.cwd()

repository_path = ROOT / "app" / "repository.py"
server_path = ROOT / "app" / "server.py"
test_server_path = ROOT / "tests" / "test_server.py"


for path in (repository_path, server_path, test_server_path):
    if not path.exists():
        raise RuntimeError(f"Файл не найден: {path}")


repository = repository_path.read_text(encoding="utf-8")
server = server_path.read_text(encoding="utf-8")
test_server = test_server_path.read_text(encoding="utf-8")


def replace_once(text, old, new, label):
    count = text.count(old)

    if count != 1:
        raise RuntimeError(
            f"{label}: ожидалось 1 совпадение, найдено {count}. "
            "Файлы НЕ изменены."
        )

    return text.replace(old, new, 1)


# ============================================================
# app/repository.py
# ============================================================

repository = replace_once(
    repository,
    '''        row = self.conn.execute(f"SELECT {column} FROM {table} WHERE id = ?", (row_id,)).fetchone()
''',
    '''        p = placeholder(self.backend)
        row = self.conn.execute(
            f"SELECT {column} FROM {table} WHERE id = {p}",
            (row_id,),
        ).fetchone()
''',
    "repository._name_by_id",
)


repository = replace_once(
    repository,
    '''        row = self.conn.execute(
            """
            SELECT r.name, p.name AS provider_name
            FROM routes r JOIN providers p ON p.id = r.provider_id
            WHERE r.id = ?
            """,
            (route_id,),
        ).fetchone()
''',
    '''        p = placeholder(self.backend)
        row = self.conn.execute(
            f"""
            SELECT r.name, p.name AS provider_name
            FROM routes r JOIN providers p ON p.id = r.provider_id
            WHERE r.id = {p}
            """,
            (route_id,),
        ).fetchone()
''',
    "repository._route_label",
)


# ============================================================
# app/server.py
# ============================================================

server = replace_once(
    server,
    '''def build_route_name(repo: Repository, country_id: int, provider_id: int, project_label: str | None, cli_source_label: str, provider_prefix_id: int | None) -> str:
    country = repo.conn.execute("SELECT name FROM countries WHERE id = ?", (country_id,)).fetchone()
    provider = repo.conn.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()
    prefix = repo.conn.execute("SELECT prefix FROM provider_prefixes WHERE id = ?", (provider_prefix_id,)).fetchone() if provider_prefix_id else None
''',
    '''def build_route_name(repo: Repository, country_id: int, provider_id: int, project_label: str | None, cli_source_label: str, provider_prefix_id: int | None) -> str:
    p = placeholder(repo.backend)
    country = repo.conn.execute(
        f"SELECT name FROM countries WHERE id = {p}",
        (country_id,),
    ).fetchone()
    provider = repo.conn.execute(
        f"SELECT name FROM providers WHERE id = {p}",
        (provider_id,),
    ).fetchone()
    prefix = (
        repo.conn.execute(
            f"SELECT prefix FROM provider_prefixes WHERE id = {p}",
            (provider_prefix_id,),
        ).fetchone()
        if provider_prefix_id
        else None
    )
''',
    "server.build_route_name",
)


server = replace_once(
    server,
    '''    return select_options(
        repo,
        """
        SELECT r.id, r.name AS label
        FROM routes r
        WHERE r.country_id = ?
        ORDER BY r.name
        """,
        (country_id,),
        selected=selected,
        empty=empty,
    )
''',
    '''    p = placeholder(repo.backend)
    return select_options(
        repo,
        f"""
        SELECT r.id, r.name AS label
        FROM routes r
        WHERE r.country_id = {p}
        ORDER BY r.name
        """,
        (country_id,),
        selected=selected,
        empty=empty,
    )
''',
    "server.route_options_for_country",
)


server = replace_once(
    server,
    '''            existing_phone = repo.conn.execute("SELECT is_active FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
''',
    '''            p = placeholder(repo.backend)
            existing_phone = repo.conn.execute(
                f"SELECT is_active FROM phone_numbers WHERE id = {p}",
                (phone_id,),
            ).fetchone()
''',
    "server.existing_phone",
)


server = replace_once(
    server,
    '''            found_company = repo.conn.execute("""
                SELECT cc.id, cc.server_id, cc.company_id_external, s.name AS server_name
                FROM calling_companies cc
                JOIN servers s ON s.id = cc.server_id
                WHERE cc.company_id_external = ? AND cc.is_active = 1
                """, (campaign_id_search,)).fetchone()
''',
    '''            p = placeholder(repo.backend)
            found_company = repo.conn.execute(
                f"""
                SELECT cc.id, cc.server_id, cc.company_id_external, s.name AS server_name
                FROM calling_companies cc
                JOIN servers s ON s.id = cc.server_id
                WHERE cc.company_id_external = {p}
                  AND cc.is_active = {p}
                """,
                (
                    campaign_id_search,
                    to_db_bool(True, repo.backend),
                ),
            ).fetchone()
''',
    "server.found_company",
)


server = replace_once(
    server,
    '''                selected_server = repo.conn.execute("SELECT name FROM servers WHERE id = ?", (helper_server_id,)).fetchone()
''',
    '''                selected_server = repo.conn.execute(
                    f"SELECT name FROM servers WHERE id = {p}",
                    (helper_server_id,),
                ).fetchone()
''',
    "server.selected_server",
)


server = replace_once(
    server,
    '''                visible_ids = {int(row["id"]) for row in repo.conn.execute("SELECT id FROM calling_companies WHERE server_id = ? AND is_active = 1", (helper_server_id,)).fetchall()}
''',
    '''                visible_ids = {
                    int(row["id"])
                    for row in repo.conn.execute(
                        f"""
                        SELECT id
                        FROM calling_companies
                        WHERE server_id = {p}
                          AND is_active = {p}
                        """,
                        (
                            helper_server_id,
                            to_db_bool(True, repo.backend),
                        ),
                    ).fetchall()
                }
''',
    "server.visible_company_ids",
)


# Telegram SAVE - меняем конкретную строку целиком,
# поэтому naming-rules сюда случайно уже не попадёт.

server = replace_once(
    server,
    '''    if path == "/admin/telegram/save":
        repo.conn.execute("INSERT INTO telegram_settings(is_enabled, chat_id, bot_token_secret_ref, message_template, updated_by) VALUES (?, ?, ?, ?, ?)", (1 if data.get("is_enabled") == "1" else 0, data.get("chat_id"), data.get("bot_token_secret_ref"), data.get("message_template"), actor_id)); repo.conn.commit(); return "/admin/telegram"
''',
    '''    if path == "/admin/telegram/save":
        p = placeholder(repo.backend)
        repo.conn.execute(
            f"""
            INSERT INTO telegram_settings(
                is_enabled,
                chat_id,
                bot_token_secret_ref,
                message_template,
                updated_by
            )
            VALUES ({p}, {p}, {p}, {p}, {p})
            """,
            (
                to_db_bool(data.get("is_enabled") == "1", repo.backend),
                data.get("chat_id"),
                data.get("bot_token_secret_ref"),
                data.get("message_template"),
                actor_id,
            ),
        )
        repo.conn.commit()
        return "/admin/telegram"
''',
    "server.telegram_save",
)


server = replace_once(
    server,
    '''    if path == "/admin/telegram/test":
        repo.conn.execute("INSERT INTO telegram_settings(is_enabled, chat_id, bot_token_secret_ref, message_template, last_test_status, last_test_at, last_test_by, updated_by) VALUES (?, ?, ?, ?, 'success', CURRENT_TIMESTAMP, ?, ?)", (1 if data.get("is_enabled") == "1" else 0, data.get("chat_id"), data.get("bot_token_secret_ref"), data.get("message_template"), actor_id, actor_id)); repo.conn.execute("INSERT INTO change_log(entity_type, change_type, changed_by, summary, source) VALUES ('telegram', 'telegram.test_message_sent', ?, 'Test Telegram message requested', 'ui')", (actor_id,)); repo.conn.commit(); return "/admin/telegram"
''',
    '''    if path == "/admin/telegram/test":
        p = placeholder(repo.backend)

        repo.conn.execute(
            f"""
            INSERT INTO telegram_settings(
                is_enabled,
                chat_id,
                bot_token_secret_ref,
                message_template,
                last_test_status,
                last_test_at,
                last_test_by,
                updated_by
            )
            VALUES (
                {p},
                {p},
                {p},
                {p},
                'success',
                CURRENT_TIMESTAMP,
                {p},
                {p}
            )
            """,
            (
                to_db_bool(data.get("is_enabled") == "1", repo.backend),
                data.get("chat_id"),
                data.get("bot_token_secret_ref"),
                data.get("message_template"),
                actor_id,
                actor_id,
            ),
        )

        repo.conn.execute(
            f"""
            INSERT INTO change_log(
                entity_type,
                change_type,
                changed_by,
                summary,
                source
            )
            VALUES (
                'telegram',
                'telegram.test_message_sent',
                {p},
                'Test Telegram message requested',
                'ui'
            )
            """,
            (actor_id,),
        )

        repo.conn.commit()
        return "/admin/telegram"
''',
    "server.telegram_test",
)


server = replace_once(
    server,
    '''    if path == "/admin/naming-rules/create":
        if data.get("is_active") == "1": repo.conn.execute("UPDATE route_naming_rules SET is_active = 0")
        repo.conn.execute("INSERT INTO route_naming_rules(name, template, is_active, comment, created_by) VALUES (?, ?, ?, ?, ?)", (data["name"], data["template"], 1 if data.get("is_active") == "1" else 0, data.get("comment"), actor_id)); repo.conn.commit(); return "/admin/naming-rules"
''',
    '''    if path == "/admin/naming-rules/create":
        p = placeholder(repo.backend)

        if data.get("is_active") == "1":
            repo.conn.execute(
                f"UPDATE route_naming_rules SET is_active = {p}",
                (to_db_bool(False, repo.backend),),
            )

        repo.conn.execute(
            f"""
            INSERT INTO route_naming_rules(
                name,
                template,
                is_active,
                comment,
                created_by
            )
            VALUES ({p}, {p}, {p}, {p}, {p})
            """,
            (
                data["name"],
                data["template"],
                to_db_bool(data.get("is_active") == "1", repo.backend),
                data.get("comment"),
                actor_id,
            ),
        )

        repo.conn.commit()
        return "/admin/naming-rules"
''',
    "server.naming_rules",
)


# ============================================================
# tests/test_server.py
# ============================================================

test_server = replace_once(
    test_server,
    '''            conn.executemany(
                'INSERT INTO change_log (entity_type, entity_id, change_type, summary, source) VALUES (%s, %s, %s, %s, %s)',
                [
                    ("test_summary", 901, "test.short", short_summary, "test"),
                    ("test_summary", 902, "test.long", long_summary, "test"),
                ],
            )
''',
    '''            with conn.cursor() as cur:
                cur.executemany(
                    'INSERT INTO change_log (entity_type, entity_id, change_type, summary, source) VALUES (%s, %s, %s, %s, %s)',
                    [
                        ("test_summary", 901, "test.short", short_summary, "test"),
                        ("test_summary", 902, "test.long", long_summary, "test"),
                    ],
                )
''',
    "test_server.executemany",
)


# ============================================================
# Только теперь сохраняем.
# Если хоть одна проверка выше упала - сюда программа не дошла.
# ============================================================

repository_path.write_text(repository, encoding="utf-8")
server_path.write_text(server, encoding="utf-8")
test_server_path.write_text(test_server, encoding="utf-8")


# Проверяем синтаксис.
py_compile.compile(str(repository_path), doraise=True)
py_compile.compile(str(server_path), doraise=True)
py_compile.compile(str(test_server_path), doraise=True)


print()
print("OK - PostgreSQL runtime SQL patch applied")
print("OK - app/repository.py compiled")
print("OK - app/server.py compiled")
print("OK - tests/test_server.py compiled")
print()