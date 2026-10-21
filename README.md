# Клинический компас: слой доступа к SQLite

Проект разделяет модели данных, интерфейс операций и SQLite-реализацию. В дальнейшем HTTP API или другой бэкенд можно добавить отдельным слоем, не перенося SQL-запросы в обработчики запросов.

## Структура

```text
clinical_compass/
├── .gitignore
├── pyproject.toml
├── README.md
├── data/
│   └── clinical_compass.sqlite3   # создаётся при запуске
├── examples/
│   └── basic_usage.py
└── src/
    └── clinical_compass/
        ├── __init__.py
        ├── domain/
        │   ├── __init__.py
        │   └── models.py
        ├── repositories/
        │   ├── __init__.py
        │   └── clinical_compass_repository.py
        └── infrastructure/
            ├── __init__.py
            └── sqlite_repository.py
```

## Требования

- Python 3.10 или новее.
- SQLite входит в стандартную библиотеку Python, отдельный пакет устанавливать не нужно.
- Внешние зависимости для запуска отсутствуют.

Проверьте версию Python:

```bash
python --version
```

## Создание виртуальной среды

Перейдите в каталог `clinical_compass`.

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Если PowerShell запрещает активацию скрипта, можно вызвать интерпретатор среды напрямую:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

## Запуск примера

Из корня проекта, после активации среды:

```bash
python examples/basic_usage.py
```

При первом запуске создастся каталог `data` и файл `data/clinical_compass.sqlite3`. Таблицы и индексы создаются автоматически. Повторный запуск примера добавит новые записи, так как демонстрационные данные не очищаются.

Чтобы посмотреть базу, можно открыть файл `data/clinical_compass.sqlite3` в DB Browser for SQLite или выполнить запросы через `sqlite3`.

## Использование в коде

```python
from datetime import date

from clinical_compass import SQLiteRepository

repository = SQLiteRepository("data/clinical_compass.sqlite3")

user = repository.create_user(
    name="Мария",
    surname="Иванова",
    password="use-a-unique-password",
)
patient = repository.create_patient(user_id=user.id)

emotion = repository.create_emotion(
    user_id=patient.id,
    anxiety=3,
    mood=7,
    energy=5,
    description="Спокойный день.",
)

drug = repository.create_drug(
    name="Пример препарата",
    prescription="Информация о рецептурном статусе.",
)
prescription = repository.create_medication_prescription(
    patient_id=patient.id,
    drug_id=drug.id,
    dosage="По назначению врача",
    start_date=date.today(),
)
repository.set_medication_schedule(
    prescription.id,
    {
        "version": 1,
        "frequency": "daily",
        "times": ["08:00", "20:00"],
        "timezone": "Europe/Moscow",
    },
)
repository.record_medication_event(prescription.id)
```

## Основные операции

`SQLiteRepository` реализует интерфейс `ClinicalCompassRepository`:

- создание пользователя, врача и пациента;
- связывание врача с пациентом;
- создание и чтение записей эмоционального дневника;
- получение врачом записей только связанного с ним пациента;
- ведение справочника препаратов и связей пациента с препаратами;
- создание назначения, сохранение расписания и запись фактического приема;
- получение событий приема по назначению.

Операции возвращают типизированные объекты из `domain/models.py`. Ошибки целостности SQLite (например, ссылка на несуществующий идентификатор или повторная связь) передаются вызывающему коду как исключения `sqlite3.IntegrityError`.

## Формат расписания

Поле `schedule_config` хранится как JSON. Пока это хранилище конфигурации, а не движок вычисления будущих приемов. Для единообразия предлагается начать с такого формата:

```json
{
  "version": 1,
  "frequency": "daily",
  "times": ["09:00", "21:00"],
  "timezone": "Europe/Moscow"
}
```

Это соглашение приложения, а не ограничение SQLite. Перед использованием в продакшене нужно формально описать допустимые значения, интервалы, дни недели, часовой пояс, дату окончания и поведение при изменении назначения.

## Важные решения и ограничения

- `Medication_Prescriptions.user_id` ссылается на `Patients.id`, то есть поле трактуется как идентификатор пациента. Это согласуется со смыслом назначения и защищает от назначения препарата произвольному пользователю.
- `Doctors.id` и `Patients.id` одновременно являются внешними ключами на `Users.id`.
- Пароли не хранятся в открытом виде: сохраняется соль и результат PBKDF2-HMAC-SHA256. Для полноценной авторизации также потребуются проверка пароля, ограничение попыток входа, управление сессиями и политика восстановления доступа.
- Оценки тревожности, настроения и энергии валидируются в диапазоне 0–10. Если продуктовая шкала другая, измените `_validate_scale`.
- `Drugs.prescription` оставлено текстовым полем, поскольку исходное назначение этого поля не определено.
- `Medication_Events` фиксирует только фактическое время приема. Пропуски и опоздания пока не представлены как отдельные события.
- Врачебный доступ проверяется методом `list_patient_emotions_for_doctor`. В веб-приложении аналогичную проверку нужно делать на каждом маршруте, который возвращает данные пациента.
- Пример предназначен для разработки и прототипирования. Медицинские данные требуют отдельной оценки безопасности, контроля доступа, резервного копирования и требований законодательства до эксплуатации с реальными пациентами.

## Подключение другого бэкенда

Обработчики HTTP-запросов должны зависеть от `ClinicalCompassRepository`, а не от `SQLiteRepository` напрямую. При переходе на PostgreSQL можно добавить другую реализацию интерфейса, сохранив модели и контракт операций. Сам интерфейс можно расширять по мере появления требований.
