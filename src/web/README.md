# Веб-слой «Медмонитора»

## Установка

1. Скопируйте `app.py`, каталог `templates` и `static` в корень Python-проекта `clinical_compass`, рядом с `pyproject.toml`.
2. Добавьте зависимости в существующий массив `dependencies` секции `[project]` в `pyproject.toml`:

```toml
dependencies = [
    "Flask>=3.1,<4.0",
    "Flask-WTF>=1.2,<2.0",
    "qrcode[pil]>=8.0,<9.0",
]
```

Если массив `dependencies` уже существует, добавьте строки в него, не создавая второй массив.

3. Добавьте в `SQLiteRepository` метод `get_user_role`, приведённый ниже.
4. Установите зависимости и проект:

```bash
python -m pip install -e .
python -m pip install "Flask>=3.1,<4.0" "Flask-WTF>=1.2,<2.0" "qrcode[pil]>=8.0,<9.0"
```

5. Задайте секретный ключ и запустите приложение.

PowerShell:

```powershell
$env:SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
python app.py
```

macOS/Linux:

```bash
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
python app.py
```

Откройте `http://127.0.0.1:5000`.

## Метод для SQLiteRepository

Добавьте следующий метод внутрь класса `SQLiteRepository` в `infrastructure/sqlite_repository.py`:

```python
def get_user_role(self, user_id: int) -> str | None:
    with self._connect() as connection:
        row = connection.execute(
            """
            SELECT CASE
                WHEN EXISTS (SELECT 1 FROM Doctors WHERE id = ?) THEN 'doctor'
                WHEN EXISTS (SELECT 1 FROM Patients WHERE id = ?) THEN 'patient'
                ELSE NULL
            END AS role
            """,
            (user_id, user_id),
        ).fetchone()
    return row["role"] if row is not None else None
```

## Примечания

- Регистрация создаёт запись пользователя и запись в `Doctors` или `Patients` в зависимости от флажка «Я врач».
- Сессия хранит только идентификатор, публичные данные пользователя и роль. Хеш пароля обрабатывается существующим репозиторием.
- Включена CSRF-защита форм.
- Врач может создать QR-код для подключения пациента. Код действует пять минут, хранится в базе в виде SHA-256-хеша и используется один раз. При создании нового кода предыдущие неиспользованные коды врача аннулируются.
- Пациенту нужно открыть ссылку из QR-кода, войти или зарегистрироваться, а затем подтвердить подключение. Связь записывается в `Doctor_Has_Patient`; список связанных пользователей отображается в обоих кабинетах.
- Чтобы сканирование с телефона работало, откройте приложение на компьютере по его LAN-адресу, доступному телефону в той же сети, а не через `127.0.0.1` или `localhost`. QR-код использует адрес, по которому врач открыл сайт.
- Кабинеты пока содержат пустые функциональные разделы. Запись дневника, переписка, назначение препаратов и аналитика ещё не подключены к интерфейсу.
- Флажок «Я врач» пока открыт для всех желающих. Для реального медицинского сервиса роль врача нужно подтверждать отдельно.
- Не используйте встроенный сервер Flask и `debug=True` для публичного развёртывания. Перед работой с реальными медицинскими данными нужны HTTPS, безопасная конфигурация, управление доступом и резервное копирование.
