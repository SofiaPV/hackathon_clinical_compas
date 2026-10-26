import hashlib
import io
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import qrcode
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFProtect

from clinical_compass import SQLiteRepository


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATABASE_PATH = Path(
    os.environ.get(
        "CLINICAL_COMPASS_DATABASE",
        PROJECT_ROOT / "data" / "clinical_compass.sqlite3",
    )
)

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL",
    "http://192.168.3.8:5000",
).rstrip("/")


# Declarative catalogue of timed tests. Adding a new test is just another entry
# here (new tests are picked up and assigned to every patient automatically).
# A question marked "critical" can raise a red alert for the patient and, later,
# notify the patient's doctor (see the hook in save_test_result()).
TEST_DEFINITIONS = [
    {
        "code": "PHQ_9",
        "title": "PHQ_9",
        "schedule_days": 14,
        "threshold": 10,
        "answer_kind": "scale",  # 'scale' -> 0..max, 'binary' -> Нет/Да
        "description": (
            "Оцените, пожалуйста, насколько часто следующие проблемы беспокоили "
            "вас в течение прошедших двух недель. Для ответов используйте "
            "следующую шкалу: 0 = «совсем нет»; 1 = «в течение нескольких дней»; "
            "2 «более, чем половину этого времени»; 3 = «почти каждый день»."
        ),
        "critical_message": (
            "Пожалуйста, если вы чувствуете, что можете причинить себе вред, "
            "не оставайтесь со своими переживаниями один на один. Обратитесь "
            "на горячую линию (телефон-заглушка)."
        ),
        # (text, min, max, is_critical)
        "questions": [
            ("Снижение интереса и удовольствия от привычных дел", 0, 3, False),
            ("Чувство подавленности или безнадежности", 0, 3, False),
            (
                "Проблемы со сном (неспособность заснуть, раннее пробуждение "
                "или слишком долгий сон)",
                0, 3, False,
            ),
            ("Чувство усталости или недостатка энергии", 0, 3, False),
            ("Плохой аппетит или переедание", 0, 3, False),
            (
                "Плохое мнение о себе или чувство, что не смог оправдать "
                "ожиданий моей семьи",
                0, 3, False,
            ),
            (
                "Проблемы с концентрацией внимания (например, при чтении газеты "
                "или просмотре телевизионной передачи).",
                0, 3, False,
            ),
            (
                "Замедленность движений или речи, которая стала заметна другим "
                "людям, или, напротив, суетливость, когда движения и речь стали "
                "более быстрыми и беспокойными",
                0, 3, False,
            ),
            (
                "Мысли о том, что мне бы хотелось умереть или причинить себе боль",
                0, 3, True,
            ),
        ],
    },
    {
        "code": "GAD-7",
        "title": "GAD-7",
        "schedule_days": 14,
        "threshold": None,
        "answer_kind": "scale",
        "description": (
            "Оцените, пожалуйста, насколько часто следующие проблемы беспокоили "
            "вас в течение прошедших двух недель. Для ответов используйте "
            "следующую шкалу: 0 = «совсем нет»; 1 = «в течение нескольких дней»; "
            "2 = «более чем половину этого времени»; 3 = «почти каждый день». "
            "Для каждого пункта выберите один ответ от 0 до 3."
        ),
        "critical_message": None,
        # Ключевые суммы баллов для дальнейшего анализа (сохраняются в БД).
        "severity_bands": [
            {"min": 5, "max": 9, "label": "Лёгкая"},
            {"min": 10, "max": 14, "label": "Умеренная"},
            {"min": 15, "max": 21, "label": "Тяжёлая"},
        ],
        "questions": [
            ("Чувство тревоги или раздражения.", 0, 3, False),
            ("Неспособность справиться со своим беспокойством.", 0, 3, False),
            ("Чрезмерное беспокойство по разным поводам.", 0, 3, False),
            ("Неспособность расслабляться.", 0, 3, False),
            (
                "Ощущение такого беспокойства, что трудно найти себе место.",
                0, 3, False,
            ),
            (
                "Склонность быстро испытывать злость или раздражительность.",
                0, 3, False,
            ),
            (
                "Чувство страха, как будто может случиться что-то ужасное.",
                0, 3, False,
            ),
        ],
    },
    {
        "code": "HAM-A",
        "title": "HAM-A",
        "audience": "doctor",
        "schedule_days": 14,
        "threshold": None,
        "answer_kind": "scale",
        "description": (
            "Обследование занимает 20–30 минут, в течение которых специалист "
            "выслушивает ответ испытуемого по теме вопроса и оценивает его по "
            "пятибалльной шкале: 0 — отсутствует, 1 — в слабой степени, "
            "2 — в умеренной степени, 3 — в тяжёлой степени, 4 — в очень тяжёлой "
            "степени. Первые шесть пунктов отражают тревогу в психической сфере, "
            "остальные восемь — в соматической сфере."
        ),
        "critical_message": None,
        "severity_bands": [
            {"min": 0, "max": 7, "label": "Отсутствие тревоги"},
            {"min": 8, "max": 17, "label": "Симптомы тревожного расстройства"},
            {"min": 18, "max": 24, "label": "Средняя выраженность"},
            {"min": 25, "max": 56, "label": "Тяжёлая степень"},
        ],
        "questions": [
            ("Тревожное настроение (озабоченность, ожидание наихудшего, тревожные опасения, раздражительность).", 0, 4, False),
            ("Напряжение (ощущение напряжения, вздрагивание, легко возникающая плаксивость, дрожь, чувство беспокойства, неспособность расслабиться).", 0, 4, False),
            ("Страхи (боязнь темноты, незнакомых людей, животных, транспорта, толпы, страх оставаться одному).", 0, 4, False),
            ("Инсомния (затруднённое засыпание, прерывистый сон, не приносящий отдыха сон с чувством разбитости при пробуждении, кошмарные сновидения).", 0, 4, False),
            ("Интеллектуальные нарушения (затруднённая концентрация внимания, ухудшение памяти).", 0, 4, False),
            ("Депрессивное настроение (утрата привычных интересов, утрата чувства удовольствия от хобби, подавленность, ранние пробуждения, суточные колебания состояния).", 0, 4, False),
            ("Соматические симптомы, мышечные (боли, мышечные подёргивания, напряжение, миоклонические судороги, «скрипение» зубами, срывающийся голос, повышенный мышечный тонус).", 0, 4, False),
            ("Соматические симптомы, сенсорные (звон в ушах, нечёткость зрения, приливы жара или холода, ощущение слабости, покалывание).", 0, 4, False),
            ("Сердечно-сосудистые симптомы (тахикардия, сердцебиение, боль в груди, пульсация в сосудах, чувство слабости, частые вздохи, диспноэ).", 0, 4, False),
            ("Респираторные симптомы (чувство давления или сжатия грудной клетки, ощущение удушья, частые вздохи, диспноэ).", 0, 4, False),
            ("Гастроинтестинальные симптомы (затруднённое глотание, метеоризм, боль в животе, изжога, чувство переполнения желудка, тошнота, рвота, урчание, диарея, снижение веса, запоры).", 0, 4, False),
            ("Мочеполовые симптомы (учащённое мочеиспускание, сильные позывы, аменорея, меноррагия, фригидность, преждевременная эякуляция, утрата либидо, импотенция).", 0, 4, False),
            ("Вегетативные симптомы (сухость во рту, покраснение или бледность кожных покровов, повышенное потоотделение, головные боли с чувством напряжения).", 0, 4, False),
            ("Поведение при осмотре (ёрзанье на месте, беспокойная жестикуляция или походка, тремор рук, нахмуривание бровей, напряжённое выражение лица, вздохи или учащённое дыхание, бледность лица, частое сглатывание слюны и т.д.).", 0, 4, False),
        ],
    },
    {
        "code": "C-SSRS",
        "title": "Колумбийская шкала оценки тяжести суицида (C-SSRS)",
        "audience": "doctor",
        "schedule_days": 14,
        "threshold": None,
        "answer_kind": "binary",
        "description": (
            "Ответьте на вопросы о мыслях и переживаниях за последний месяц. "
            "Вопрос 6 относится ко всей жизни; при ответе «Да» уточните, "
            "происходило ли это в последние 3 месяца. Сначала ответьте на "
            "вопросы 1 и 2. Если на вопрос 2 ответ «Да», переходите к вопросам "
            "3, 4, 5 и 6. Если «Нет» — сразу к вопросу 6. Баллы не суммируются: "
            "уровень риска определяется по наиболее серьёзному положительному "
            "ответу с учётом давности событий (вопросы 1–2 — низкий; вопросы 3 "
            "или 6 давностью более 3 месяцев — умеренный; вопросы 4 или 5 за "
            "последний месяц либо вопрос 6 за последние 3 месяца — высокий)."
        ),
        "critical_message": None,
        "severity_bands": None,
        "questions": [
            ("Возникало ли у Вас желание умереть или заснуть и не просыпаться?", 0, 1, False),
            ("Думали ли Вы в самом деле о том, чтобы убить себя?", 0, 1, False),
            ("Думали ли Вы о том, как Вы могли бы это сделать?", 0, 1, False),
            ("Посещали ли Вас подобные мысли, и было ли у Вас какое-нибудь намерение действовать в соответствии с ними?", 0, 1, False),
            ("Начали ли Вы разрабатывать или уже разработали детальный план самоубийства? Намереваетесь ли Вы осуществить этот план?", 0, 1, False),
            ("Вы когда-нибудь что-нибудь делали, начинали что-то делать или готовились сделать что-нибудь, чтобы покончить с собой?", 0, 1, False),
            ("Уточнение к вопросу 6: было ли это в последние 3 месяца?", 0, 1, False),
        ],
    },
    {
        "code": "HAM-D",
        "title": "HAM-D",
        "audience": "doctor",
        "schedule_days": 14,
        "threshold": None,
        "answer_kind": "scale",
        "description": (
            "Оценочная шкала депрессии Гамильтона. Клиническое интервью занимает "
            "примерно 20–25 минут. Оцениваемый период — неделя перед опросом "
            "(кроме пунктов 8, 9 — во время опроса, и 16 — перед эпизодом). "
            "Для каждого пункта выберите один ответ. Ключ по сумме баллов: "
            "0–7 — норма; 8–13 — лёгкое депрессивное расстройство; "
            "14–18 — средней степени тяжести; 19–22 — тяжёлое; "
            "23 и более — крайне тяжёлое."
        ),
        "critical_message": None,
        "severity_bands": [
            {"min": 0, "max": 7, "label": "Норма"},
            {"min": 8, "max": 13, "label": "Лёгкое"},
            {"min": 14, "max": 18, "label": "Средней тяжести"},
            {"min": 19, "max": 22, "label": "Тяжёлое"},
            {"min": 23, "max": 52, "label": "Крайне тяжёлое"},
        ],
        "questions": [
            ("Сниженное настроение — 0: отсутствует; 1: сообщает только при расспросе; 2: сообщает спонтанно (вербально); 3: выражает невербально (мимика, поза, голос, готовность к плачу); 4: выражает только эти чувства и спонтанно, и невербально.", 0, 4, False),
            ("Чувство вины — 0: отсутствует; 1: самоупрёки, считает, что подвёл других; 2: идеи виновности или размышления о прошлых ошибках/грехах; 3: болезнь как наказание, бредовые идеи виновности; 4: обвиняющие вербальные и/или угрожающие зрительные галлюцинации.", 0, 4, False),
            ("Суицидальные намерения — 0: отсутствуют; 1: ощущение малоценности жизни; 2: желание смерти или мысли о собственной смерти; 3: суицидальные высказывания или жесты; 4: попытки самоубийства.", 0, 4, False),
            ("Бессонница ранняя (засыпание) — 0: нет затруднений; 1: эпизодические затруднения (дольше ~0,5 часа); 2: невозможность заснуть каждую ночь.", 0, 2, False),
            ("Бессонница середины ночи — 0: отсутствует; 1: беспокойный сон всю ночь; 2: пробуждения ночью с подъёмом с постели (кроме физиологических потребностей).", 0, 2, False),
            ("Бессонница поздняя (ранние утренние часы) — 0: отсутствует; 1: просыпается рано, но снова засыпает; 2: заснуть повторно не удаётся.", 0, 2, False),
            ("Работоспособность и активность — 0: нет затруднений; 1: ощущение несостоятельности, усталость, слабость; 2: утрата интереса к работе/хобби; 3: снижение времени активности или продуктивности; 4: отказ от работы из-за болезни.", 0, 4, False),
            ("Заторможенность — 0: нормальная речь и мышление; 1: лёгкая заторможенность в беседе; 2: заметная заторможенность; 3: беседа затруднена; 4: полный ступор.", 0, 4, False),
            ("Возбуждение — 0: отсутствует; 1: суетливость; 2: беспокойные движения руками, теребление волос; 3: подвижность, неусидчивость; 4: заламывание рук, кусание ногтей/губ, выдёргивание волос.", 0, 4, False),
            ("Психическая тревога — 0: отсутствует; 1: субъективное напряжение и раздражительность; 2: беспокойство по незначительным поводам; 3: тревога в выражении лица и речи; 4: спонтанно излагает тревожные опасения.", 0, 4, False),
            ("Соматическая тревога (физиологические проявления) — 0: отсутствие; 1: лёгкая; 2: умеренная; 3: тяжёлая; 4: крайне тяжёлая.", 0, 4, False),
            ("Желудочно-кишечные соматические симптомы — 0: нет; 1: потеря аппетита, ест без помощи, тяжесть в желудке; 2: ест с трудом, просит слабительное/лекарства для ЖКТ.", 0, 2, False),
            ("Общие соматические симптомы — 0: нет; 1: тяжесть в конечностях/спине/голове, боли, утрата энергии, истощение; 2: любые ясно выраженные симптомы.", 0, 2, False),
            ("Половые симптомы (утрата либидо, менструальные проблемы) — 0: отсутствуют; 1: средне; 2: сильно.", 0, 2, False),
            ("Ипохондрия — 0: отсутствует; 1: иногда мысли о здоровье; 2: поглощён мыслями о здоровье; 3: частые жалобы, просьбы о помощи; 4: ипохондрический бред.", 0, 4, False),
            ("Потеря веса — 0: не потерял или по другой причине; 1: подозревается потеря веса из-за болезни; 2: уверенно подтверждает потерю веса из-за депрессии.", 0, 2, False),
            ("Критика — 0: подтверждает подавленность/болезнь или не в депрессии; 1: подтверждает болезнь, но винит внешние причины; 2: отвергает, что болен.", 0, 2, False),
        ],
    },
]


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("Set the SECRET_KEY environment variable.")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
        "SESSION_COOKIE_SECURE", "0"
    ) == "1"
    CSRFProtect(app)

    repository = SQLiteRepository(DATABASE_PATH)
    app.extensions["clinical_compass_repository"] = repository

    @contextmanager
    def connect_database() -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(DATABASE_PATH, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_link_tokens() -> None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS Doctor_Link_Tokens (
                    token_hash TEXT PRIMARY KEY,
                    doctor_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    FOREIGN KEY (doctor_id) REFERENCES Doctors(id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_doctor_link_tokens_expiry "
                "ON Doctor_Link_Tokens(expires_at)"
            )

    initialize_link_tokens()

    def initialize_tests() -> None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS Tests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    schedule_days INTEGER NOT NULL,
                    threshold INTEGER,
                    answer_kind TEXT NOT NULL DEFAULT 'scale',
                    critical_message TEXT,
                    severity_bands TEXT,
                    audience TEXT NOT NULL DEFAULT 'patient'
                );
                CREATE TABLE IF NOT EXISTS Test_Questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    test_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    min_value INTEGER NOT NULL DEFAULT 0,
                    max_value INTEGER NOT NULL DEFAULT 3,
                    is_critical INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (test_id) REFERENCES Tests(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS Patient_Has_Test (
                    patient_id INTEGER NOT NULL,
                    test_id INTEGER NOT NULL,
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY (patient_id, test_id),
                    FOREIGN KEY (patient_id) REFERENCES Patients(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (test_id) REFERENCES Tests(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS Test_Results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    test_id INTEGER NOT NULL,
                    patient_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    total_score INTEGER NOT NULL,
                    FOREIGN KEY (test_id) REFERENCES Tests(id) ON DELETE CASCADE,
                    FOREIGN KEY (patient_id) REFERENCES Patients(id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS Test_Answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    result_id INTEGER NOT NULL,
                    question_id INTEGER NOT NULL,
                    value INTEGER NOT NULL,
                    FOREIGN KEY (result_id) REFERENCES Test_Results(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (question_id) REFERENCES Test_Questions(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_test_results_patient
                    ON Test_Results(patient_id, test_id, created_at);
                """
            )
            # Migrations for databases created before these columns existed.
            test_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(Tests)")
            }
            if "severity_bands" not in test_columns:
                connection.execute("ALTER TABLE Tests ADD COLUMN severity_bands TEXT")
            if "audience" not in test_columns:
                connection.execute(
                    "ALTER TABLE Tests ADD COLUMN audience TEXT NOT NULL "
                    "DEFAULT 'patient'"
                )
            user_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(Users)")
            }
            if "birth_date" not in user_columns:
                connection.execute("ALTER TABLE Users ADD COLUMN birth_date TEXT")
            for spec in TEST_DEFINITIONS:
                existing = connection.execute(
                    "SELECT id FROM Tests WHERE code = ?", (spec["code"],)
                ).fetchone()
                if existing is not None:
                    continue
                bands = spec.get("severity_bands")
                cursor = connection.execute(
                    """
                    INSERT INTO Tests
                        (code, title, description, schedule_days, threshold,
                         answer_kind, critical_message, severity_bands, audience)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec["code"],
                        spec["title"],
                        spec["description"],
                        spec["schedule_days"],
                        spec.get("threshold"),
                        spec.get("answer_kind", "scale"),
                        spec.get("critical_message"),
                        json.dumps(bands, ensure_ascii=False) if bands else None,
                        spec.get("audience", "patient"),
                    ),
                )
                test_id = cursor.lastrowid
                for position, (text, low, high, critical) in enumerate(
                    spec["questions"], start=1
                ):
                    connection.execute(
                        """
                        INSERT INTO Test_Questions
                            (test_id, position, text, min_value, max_value,
                             is_critical)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (test_id, position, text, low, high, 1 if critical else 0),
                    )

    initialize_tests()

    @app.get("/")
    def index():
        if "user_id" not in session:
            return redirect(url_for("login"))
        return redirect(url_for("dashboard"))

    @app.get("/scan")
    def scan_qr():
        return render_template("scan_qr.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            surname = request.form.get("lastname", "").strip()
            name = request.form.get("firstname", "").strip()
            second_name = request.form.get("patronymic", "").strip() or None
            birth_date = request.form.get("birth_date", "").strip() or None
            is_doctor = request.form.get("is_doctor") == "on"

            if len(password) < 8:
                flash(
                    "Пароль должен содержать не менее 8 символов.",
                    "error",
                )
                return render_template("register.html")
            if not name or not surname:
                flash("Укажите имя и фамилию.", "error")
                return render_template("register.html")

            try:
                user = repository.create_user(
                    name=name,
                    surname=surname,
                    second_name=second_name,
                    email=email,
                    password=password,
                )
                if is_doctor:
                    repository.create_doctor(user.id)
                else:
                    repository.create_patient(user.id)
                if birth_date:
                    with connect_database() as connection:
                        connection.execute(
                            "UPDATE Users SET birth_date = ? WHERE id = ?",
                            (birth_date, user.id),
                        )
            except (ValueError, sqlite3.IntegrityError) as error:
                flash(str(error), "error")
                return render_template("register.html")

            pending_token = session.get("pending_link_token")
            session.clear()
            session["user_id"] = user.id
            session["email"] = user.email
            session["name"] = user.name
            session["surname"] = user.surname
            session["role"] = "doctor" if is_doctor else "patient"
            if pending_token and not is_doctor:
                return redirect(
                    url_for("connect_patient", token=pending_token)
                )
            return redirect(url_for("dashboard"))

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            try:
                user = repository.authenticate_user(email, password)
            except ValueError:
                user = None

            if user is None:
                flash(
                    "Неверная почта или пароль.",
                    "error",
                )
                return render_template("login.html")

            role = repository.get_user_role(user.id)
            if role is None:
                flash(
                    "Для аккаунта не настроен тип пользователя. "
                    "Обратитесь к администратору.",
                    "error",
                )
                return render_template("login.html")

            pending_token = session.get("pending_link_token")
            session.clear()
            session["user_id"] = user.id
            session["email"] = user.email
            session["name"] = user.name
            session["surname"] = user.surname
            session["role"] = role
            if pending_token and role == "patient":
                return redirect(
                    url_for("connect_patient", token=pending_token)
                )
            return redirect(url_for("dashboard"))

        return render_template("login.html")

    @app.get("/dashboard")
    def dashboard():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") == "doctor":
            with connect_database() as connection:
                patients = connection.execute(
                    """
                    SELECT Users.id, Users.name, Users.surname, Users.email
                    FROM Doctor_Has_Patient
                    JOIN Users ON Users.id = Doctor_Has_Patient.patient_id
                    WHERE Doctor_Has_Patient.doctor_id = ?
                    ORDER BY Users.surname, Users.name
                    """,
                    (session["user_id"],),
                ).fetchall()
                _, counts = all_patient_cards(connection)
            return render_template(
                "doctor.html",
                user=session_user(),
                patients=patients,
                counts=counts,
            )
        if session.get("role") == "patient":
            with connect_database() as connection:
                doctors = connection.execute(
                    """
                    SELECT Users.name, Users.surname, Users.email
                    FROM Doctor_Has_Patient
                    JOIN Users ON Users.id = Doctor_Has_Patient.doctor_id
                    WHERE Doctor_Has_Patient.patient_id = ?
                    ORDER BY Users.surname, Users.name
                    """,
                    (session["user_id"],),
                ).fetchall()
                ensure_test_assignments(connection, session["user_id"])
                pending_tests = patient_pending_tests(
                    connection, session["user_id"]
                )
                critical_alerts = patient_critical_alerts(
                    connection, session["user_id"]
                )
            entries = emotion_series(session["user_id"], days=14)
            return render_template(
                "patient.html",
                user=session_user(),
                doctors=doctors,
                entries=entries,
                pending_tests=pending_tests,
                critical_alerts=critical_alerts,
            )
        session.clear()
        flash(
            "Не удалось определить тип аккаунта. Войдите снова.",
            "error",
        )
        return redirect(url_for("login"))

    @app.post("/doctor/link/create")
    def create_patient_link():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "doctor":
            flash(
                "Создавать приглашения может только врач.",
                "error",
            )
            return redirect(url_for("dashboard"))

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        with connect_database() as connection:
            connection.execute(
                """
                UPDATE Doctor_Link_Tokens
                SET used_at = ?
                WHERE doctor_id = ? AND used_at IS NULL
                """,
                (datetime.now(timezone.utc).isoformat(), session["user_id"]),
            )
            connection.execute(
                """
                INSERT INTO Doctor_Link_Tokens
                    (token_hash, doctor_id, expires_at)
                VALUES (?, ?, ?)
                """,
                (token_hash, session["user_id"], expires_at.isoformat()),
            )

        session["doctor_link_token"] = token
        session["doctor_link_expires_at"] = expires_at.isoformat()
        return redirect(url_for("doctor_add_patient"))

    @app.get("/doctor/add-patient")
    def doctor_add_patient():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "doctor":
            flash(
                "Эта страница доступна только врачу.",
                "error",
            )
            return redirect(url_for("dashboard"))

        token = session.get("doctor_link_token")
        expires_at = session.get("doctor_link_expires_at")
        if expires_at:
            try:
                expires_at_value = datetime.fromisoformat(expires_at)
                is_expired = datetime.now(timezone.utc) >= expires_at_value
            except ValueError:
                is_expired = True
            if is_expired:
                token = None
                session.pop("doctor_link_token", None)
                session.pop("doctor_link_expires_at", None)

        return render_template(
            "doctor_add_patient.html",
            user=session_user(),
            has_token=bool(token),
            expires_at=expires_at if token else None,
        )

    @app.get("/doctor/add-patient/qr.png")
    def doctor_link_qr():
        if "user_id" not in session or session.get("role") != "doctor":
            return "", 403
        token = session.get("doctor_link_token")
        expires_at = session.get("doctor_link_expires_at")
        if not token or not expires_at:
            return "", 404
        if datetime.now(timezone.utc) >= datetime.fromisoformat(expires_at):
            session.pop("doctor_link_token", None)
            session.pop("doctor_link_expires_at", None)
            return "", 410

        connect_url = (
            f"{PUBLIC_BASE_URL}"
            f"{url_for('connect_patient', token=token)}"
        )
        qr_code = qrcode.QRCode(box_size=8, border=3)
        qr_code.add_data(connect_url)
        qr_code.make(fit=True)
        image = qr_code.make_image(fill_color="black", back_color="white")
        output = io.BytesIO()
        image.save(output, format="PNG")
        output.seek(0)
        response = send_file(output, mimetype="image/png", max_age=0)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/connect/<token>", methods=["GET", "POST"])
    def connect_patient(token: str):
        if "user_id" not in session:
            session["pending_link_token"] = token
            flash(
                "Войдите или зарегистрируйтесь, чтобы связаться "
                "с врачом.",
                "info",
            )
            return redirect(url_for("login"))
        if session.get("role") != "patient":
            flash(
                "Связать аккаунт с врачом может только пациент.",
                "error",
            )
            return redirect(url_for("dashboard"))

        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        with connect_database() as connection:
            invitation = connection.execute(
                """
                SELECT token_hash, doctor_id, expires_at, used_at
                FROM Doctor_Link_Tokens
                WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if invitation is None:
                flash(
                    "QR-код недействителен или уже использован.",
                    "error",
                )
                return redirect(url_for("dashboard"))
            if invitation["used_at"] is not None:
                flash(
                    "Этот QR-код уже использован. "
                    "Попросите врача создать новый.",
                    "error",
                )
                return redirect(url_for("dashboard"))
            if now >= datetime.fromisoformat(invitation["expires_at"]):
                flash(
                    "Срок действия QR-кода истёк. "
                    "Попросите врача создать новый.",
                    "error",
                )
                return redirect(url_for("dashboard"))

            doctor = connection.execute(
                "SELECT name, surname, email FROM Users WHERE id = ?",
                (invitation["doctor_id"],),
            ).fetchone()
            if doctor is None:
                flash("Аккаунт врача не найден.", "error")
                return redirect(url_for("dashboard"))

            if request.method == "POST":
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute(
                        """
                        SELECT doctor_id, expires_at, used_at
                        FROM Doctor_Link_Tokens WHERE token_hash = ?
                        """,
                        (token_hash,),
                    ).fetchone()
                    if (
                        current is None
                        or current["used_at"] is not None
                        or now >= datetime.fromisoformat(current["expires_at"])
                    ):
                        connection.rollback()
                        flash(
                            "QR-код истёк или уже использован.",
                            "error",
                        )
                        return redirect(url_for("dashboard"))
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO Doctor_Has_Patient
                            (doctor_id, patient_id)
                        VALUES (?, ?)
                        """,
                        (current["doctor_id"], session["user_id"]),
                    )
                    connection.execute(
                        "UPDATE Doctor_Link_Tokens SET used_at = ? "
                        "WHERE token_hash = ? AND used_at IS NULL",
                        (now.isoformat(), token_hash),
                    )
                    connection.commit()
                except sqlite3.Error:
                    connection.rollback()
                    app.logger.exception("Failed to link patient and doctor")
                    flash(
                        "Не удалось связать аккаунты. "
                        "Попробуйте ещё раз.",
                        "error",
                    )
                    return redirect(url_for("dashboard"))

                flash("Вы связаны с врачом.", "success")
                return redirect(url_for("dashboard"))

        return render_template(
            "connect_patient.html",
            doctor=doctor,
            token=token,
        )

    @app.get("/tracker")
    def tracker():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "patient":
            flash("Дневник состояния доступен только пациенту.", "error")
            return redirect(url_for("dashboard"))
        return render_template("tracker.html", user=session_user())

    @app.post("/tracker")
    def tracker_save():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "patient":
            flash("Дневник состояния доступен только пациенту.", "error")
            return redirect(url_for("dashboard"))

        def parse_scale(name: str) -> int | None:
            raw = request.form.get(name)
            if raw is None or raw == "":
                return None
            try:
                value = int(raw)
            except ValueError:
                return None
            return value if 0 <= value <= 10 else None

        mood = parse_scale("mood")
        anxiety = parse_scale("anxiety")
        energy = parse_scale("energy")
        description = (request.form.get("description") or "").strip() or None

        if mood is None or anxiety is None or energy is None:
            flash(
                "Отметьте настроение, тревогу и энергию (значение от 0 до 10).",
                "error",
            )
            return render_template(
                "tracker.html",
                user=session_user(),
                mood=mood,
                anxiety=anxiety,
                energy=energy,
                description=description or "",
            )

        repository.create_emotion(
            user_id=session["user_id"],
            anxiety=anxiety,
            mood=mood,
            energy=energy,
            description=description,
        )
        flash("Запись о состоянии сохранена.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/stats")
    def stats():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "patient":
            flash("Статистика доступна только пациенту.", "error")
            return redirect(url_for("dashboard"))

        ranges = {"week": 7, "2weeks": 14, "month": 30, "all": None}
        range_key = request.args.get("range", "week")
        if range_key not in ranges:
            range_key = "week"
        entries = emotion_series(session["user_id"], days=ranges[range_key])
        return render_template(
            "stats.html",
            user=session_user(),
            entries=entries,
            range_key=range_key,
        )

    @app.get("/notes")
    def notes():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "patient":
            flash("Заметки о состоянии доступны только пациенту.", "error")
            return redirect(url_for("dashboard"))

        # list_emotions already returns the newest entries first.
        records = repository.list_emotions(session["user_id"])
        notes_list = [
            {
                "date": entry.created_at.strftime("%d.%m.%Y"),
                "time": entry.created_at.strftime("%H:%M"),
                "mood": entry.mood,
                "anxiety": entry.anxiety,
                "energy": entry.energy,
                "text": entry.description,
            }
            for entry in records
            if entry.description and entry.description.strip()
        ]
        return render_template(
            "notes.html", user=session_user(), notes=notes_list
        )

    @app.get("/tests")
    def tests():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "patient":
            flash("Тесты доступны только пациенту.", "error")
            return redirect(url_for("dashboard"))
        with connect_database() as connection:
            ensure_test_assignments(connection, session["user_id"])
            pending = patient_pending_tests(connection, session["user_id"])
            critical_alerts = patient_critical_alerts(
                connection, session["user_id"]
            )
        return render_template(
            "tests.html",
            user=session_user(),
            tests=pending,
            critical_alerts=critical_alerts,
        )

    def _load_test(connection, test_id: int):
        test = connection.execute(
            "SELECT * FROM Tests WHERE id = ?", (test_id,)
        ).fetchone()
        if test is None:
            return None, []
        questions = connection.execute(
            """
            SELECT * FROM Test_Questions
            WHERE test_id = ?
            ORDER BY position, id
            """,
            (test_id,),
        ).fetchall()
        return test, questions

    @app.get("/tests/<int:test_id>")
    def take_test(test_id: int):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "patient":
            flash("Тесты доступны только пациенту.", "error")
            return redirect(url_for("dashboard"))
        with connect_database() as connection:
            test, questions = _load_test(connection, test_id)
        if test is None:
            flash("Тест не найден.", "error")
            return redirect(url_for("tests"))
        return render_template(
            "take_test.html",
            user=session_user(),
            test=test,
            questions=questions,
            action_url=url_for("submit_test", test_id=test["id"]),
            back_url=url_for("tests"),
            strict=True,
        )

    @app.post("/tests/<int:test_id>")
    def submit_test(test_id: int):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "patient":
            flash("Тесты доступны только пациенту.", "error")
            return redirect(url_for("dashboard"))

        with connect_database() as connection:
            test, questions = _load_test(connection, test_id)
            if test is None:
                flash("Тест не найден.", "error")
                return redirect(url_for("tests"))

            answers: dict[int, int] = {}
            critical_positive = False
            for question in questions:
                raw = request.form.get(f"q{question['id']}")
                try:
                    value = int(raw) if raw not in (None, "") else None
                except (TypeError, ValueError):
                    value = None
                if value is None or not (
                    question["min_value"] <= value <= question["max_value"]
                ):
                    flash("Ответьте на все вопросы теста.", "error")
                    return render_template(
                        "take_test.html",
                        user=session_user(),
                        test=test,
                        questions=questions,
                        action_url=url_for("submit_test", test_id=test["id"]),
                        back_url=url_for("tests"),
                        strict=True,
                    )
                answers[question["id"]] = value
                if question["is_critical"] and value > 0:
                    critical_positive = True

            total_score = sum(answers.values())
            cursor = connection.execute(
                """
                INSERT INTO Test_Results
                    (test_id, patient_id, created_at, total_score)
                VALUES (?, ?, ?, ?)
                """,
                (
                    test_id,
                    session["user_id"],
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    total_score,
                ),
            )
            result_id = cursor.lastrowid
            for question_id, value in answers.items():
                connection.execute(
                    """
                    INSERT INTO Test_Answers (result_id, question_id, value)
                    VALUES (?, ?, ?)
                    """,
                    (result_id, question_id, value),
                )

        # Architecture hook for the future: when a critical question is answered
        # positively (or the score crosses the test threshold), this is where
        # the patient's doctor would be notified that critical help may be
        # needed. For now we only surface the alert to the patient on their
        # dashboard via patient_critical_alerts().
        if critical_positive:
            app.logger.info(
                "Critical response recorded for patient %s on test %s",
                session["user_id"],
                test["code"],
            )

        flash("Тест пройден. Спасибо!", "success")
        return redirect(url_for("dashboard"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    def session_user():
        return {
            "id": session["user_id"],
            "name": session.get("name", ""),
            "surname": session.get("surname", ""),
            "email": session.get("email", ""),
            "role": session.get("role", "patient"),
        }

    def emotion_series(user_id: int, days: int | None = None):
        """Emotion entries for charts, oldest first, optionally limited to a
        trailing window of ``days``."""
        entries = list(reversed(repository.list_emotions(user_id)))
        if days is not None:
            cutoff = datetime.now().astimezone() - timedelta(days=days)

            def as_aware(moment: datetime) -> datetime:
                return moment if moment.tzinfo else moment.astimezone()

            entries = [e for e in entries if as_aware(e.created_at) >= cutoff]
        return [
            {
                "t": entry.created_at.isoformat(),
                "mood": entry.mood,
                "anxiety": entry.anxiety,
                "energy": entry.energy,
            }
            for entry in entries
        ]

    def _aware(moment: datetime) -> datetime:
        return moment if moment.tzinfo else moment.astimezone()

    def ensure_test_assignments(connection, patient_id: int) -> None:
        """Assign every catalogue test to the patient once. New tests added to
        the catalogue later are picked up here on the next page load."""
        connection.execute(
            """
            INSERT OR IGNORE INTO Patient_Has_Test (patient_id, test_id, assigned_at)
            SELECT ?, id, ? FROM Tests WHERE audience = 'patient'
            """,
            (patient_id, datetime.now().astimezone().isoformat(timespec="seconds")),
        )

    def patient_pending_tests(connection, patient_id: int) -> list[dict]:
        """Tests the patient currently owes. A test is due when it has never
        been taken (available from registration) or when at least one schedule
        interval has passed since the last time it was completed. It never
        accumulates: at most one pending pass per test."""
        rows = connection.execute(
            """
            SELECT t.id, t.title, t.schedule_days,
                (SELECT MAX(created_at) FROM Test_Results r
                 WHERE r.test_id = t.id AND r.patient_id = ?) AS last_at
            FROM Tests t
            JOIN Patient_Has_Test pht
                ON pht.test_id = t.id AND pht.patient_id = ?
            ORDER BY t.id
            """,
            (patient_id, patient_id),
        ).fetchall()
        now = datetime.now().astimezone()
        pending = []
        for row in rows:
            if row["last_at"] is None:
                due = True
            else:
                anchor = _aware(datetime.fromisoformat(row["last_at"]))
                due = now >= anchor + timedelta(days=row["schedule_days"])
            if due:
                pending.append({"id": row["id"], "title": row["title"]})
        return pending

    def patient_critical_alerts(connection, patient_id: int) -> list[str]:
        """Active critical alerts: the latest result of a test with a critical
        question answered above its minimum. Clears once the test is retaken
        with a non-positive answer."""
        tests = connection.execute(
            """
            SELECT DISTINCT t.id, t.critical_message
            FROM Tests t
            JOIN Test_Questions q ON q.test_id = t.id AND q.is_critical = 1
            WHERE t.critical_message IS NOT NULL AND t.audience = 'patient'
            """
        ).fetchall()
        alerts = []
        for test in tests:
            latest = connection.execute(
                """
                SELECT id FROM Test_Results
                WHERE patient_id = ? AND test_id = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (patient_id, test["id"]),
            ).fetchone()
            if latest is None:
                continue
            answer = connection.execute(
                """
                SELECT ta.value
                FROM Test_Answers ta
                JOIN Test_Questions q ON q.id = ta.question_id AND q.is_critical = 1
                WHERE ta.result_id = ?
                """,
                (latest["id"],),
            ).fetchone()
            if answer is not None and answer["value"] is not None and answer["value"] > 0:
                alerts.append(test["critical_message"])
        return alerts

    # ------------------------------------------------------------------ #
    #  Doctor cabinet: triage, patient profiles and clinician-run tests  #
    # ------------------------------------------------------------------ #

    CATEGORY_PRIORITY = {"red": 0, "yellow": 1, "white": 2, "green": 3}

    def _require_doctor():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "doctor":
            flash("Этот раздел доступен только врачу.", "error")
            return redirect(url_for("dashboard"))
        return None

    def _fmt_date(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).strftime("%d.%m.%Y")
        except ValueError:
            return value

    def _fmt_datetime(value):
        try:
            return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            return value

    def _full_name(row):
        return " ".join(
            part for part in (row["name"], row["surname"], row["second_name"]) if part
        )

    def _load_patient(connection, patient_id):
        return connection.execute(
            """
            SELECT u.id, u.name, u.surname, u.second_name, u.birth_date, u.email
            FROM Patients p JOIN Users u ON u.id = p.id
            WHERE u.id = ?
            """,
            (patient_id,),
        ).fetchone()

    def _recent_results(connection, patient_id, code, limit=2):
        return connection.execute(
            """
            SELECT r.id, r.total_score, r.created_at
            FROM Test_Results r JOIN Tests t ON t.id = r.test_id
            WHERE r.patient_id = ? AND t.code = ?
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT ?
            """,
            (patient_id, code, limit),
        ).fetchall()

    def _delta_cell(results):
        """Latest score plus direction/magnitude of change vs the previous one."""
        if not results:
            return {"value": None, "dir": None, "mag": None}
        last = results[0]["total_score"]
        if len(results) < 2:
            return {"value": last, "dir": None, "mag": None}
        diff = last - results[1]["total_score"]
        if diff > 0:
            return {"value": last, "dir": "up", "mag": diff}
        if diff < 0:
            return {"value": last, "dir": "down", "mag": -diff}
        return {"value": last, "dir": "flat", "mag": 0}

    def _phq9_q9_positive(connection, patient_id):
        latest = connection.execute(
            """
            SELECT r.id FROM Test_Results r JOIN Tests t ON t.id = r.test_id
            WHERE r.patient_id = ? AND t.code = 'PHQ_9'
            ORDER BY r.created_at DESC, r.id DESC LIMIT 1
            """,
            (patient_id,),
        ).fetchone()
        if latest is None:
            return False
        ans = connection.execute(
            """
            SELECT ta.value FROM Test_Answers ta
            JOIN Test_Questions q ON q.id = ta.question_id AND q.is_critical = 1
            WHERE ta.result_id = ?
            """,
            (latest["id"],),
        ).fetchone()
        return ans is not None and ans["value"] is not None and ans["value"] > 0

    def doctor_patient_card(connection, patient):
        """Triage card for one patient: category, signal and trend columns."""
        pid = patient["id"]
        phq = _recent_results(connection, pid, "PHQ_9")
        gad = _recent_results(connection, pid, "GAD-7")
        phq_cell = _delta_cell(phq)
        gad_cell = _delta_cell(gad)
        red_flag = _phq9_q9_positive(connection, pid)

        now = datetime.now().astimezone()
        last_any = connection.execute(
            "SELECT MAX(created_at) AS m FROM Emotions WHERE user_id = ?", (pid,)
        ).fetchone()["m"]
        days_since = None
        if last_any:
            days_since = (now - _aware(datetime.fromisoformat(last_any))).days
        cutoff7 = now - timedelta(days=7)
        avg_row = connection.execute(
            "SELECT AVG(mood) AS a FROM Emotions "
            "WHERE user_id = ? AND mood IS NOT NULL AND created_at >= ?",
            (pid, cutoff7.isoformat(timespec="seconds")),
        ).fetchone()
        diary_avg = round(avg_row["a"], 1) if avg_row["a"] is not None else None

        no_data = last_any is None or (days_since is not None and days_since > 7)
        phq_up = phq_cell["mag"] if phq_cell["dir"] == "up" else 0
        gad_up = gad_cell["mag"] if gad_cell["dir"] == "up" else 0
        worsening = phq_up >= 5 or gad_up >= 5

        if red_flag:
            category, signal = "red", "PHQ-9 п.9 > 0"
        elif worsening:
            category = "yellow"
            p = phq_up if phq_up >= 5 else 0
            g = gad_up if gad_up >= 5 else 0
            if p and g:
                signal = f"Ухудшение GAD-7 +{g} и PHQ-9 +{p}"
            elif p:
                signal = f"Ухудшение PHQ-9 +{p}"
            else:
                signal = f"Ухудшение GAD-7 +{g}"
        elif no_data:
            category = "white"
            signal = (
                f"Нет данных {days_since} дней" if days_since is not None
                else "Нет данных"
            )
        else:
            category = "green"
            phq_last, gad_last = phq_cell["value"], gad_cell["value"]
            if (
                phq_last is not None and phq_last < 5
                and gad_last is not None and gad_last < 5
            ):
                signal = "Ремиссия"
            else:
                signal = "Стабильно"

        return {
            "id": pid,
            "full_name": _full_name(patient),
            "birth_date": _fmt_date(patient["birth_date"]),
            "category": category,
            "signal": signal,
            "phq": phq_cell,
            "gad": gad_cell,
            "diary_avg": diary_avg,
        }

    def all_patient_cards(connection):
        patients = connection.execute(
            """
            SELECT u.id, u.name, u.surname, u.second_name, u.birth_date
            FROM Patients p JOIN Users u ON u.id = p.id
            """
        ).fetchall()
        cards = [doctor_patient_card(connection, p) for p in patients]
        cards.sort(key=lambda c: (CATEGORY_PRIORITY[c["category"]], c["full_name"]))
        counts = {"red": 0, "yellow": 0, "white": 0, "green": 0}
        for card in cards:
            counts[card["category"]] += 1
        return cards, counts

    @app.get("/doctor/patients")
    def doctor_patients():
        guard = _require_doctor()
        if guard is not None:
            return guard
        with connect_database() as connection:
            cards, counts = all_patient_cards(connection)
        return render_template(
            "patients.html", user=session_user(), cards=cards, counts=counts
        )

    @app.get("/doctor/patients/<int:patient_id>")
    def doctor_patient_profile(patient_id: int):
        guard = _require_doctor()
        if guard is not None:
            return guard
        with connect_database() as connection:
            patient = _load_patient(connection, patient_id)
        if patient is None:
            flash("Пациент не найден.", "error")
            return redirect(url_for("doctor_patients"))
        return render_template(
            "patient_profile.html",
            user=session_user(),
            patient_id=patient_id,
            full_name=_full_name(patient),
            birth_date=_fmt_date(patient["birth_date"]),
        )

    @app.get("/doctor/patients/<int:patient_id>/tests")
    def doctor_patient_tests(patient_id: int):
        guard = _require_doctor()
        if guard is not None:
            return guard
        with connect_database() as connection:
            patient = _load_patient(connection, patient_id)
            if patient is None:
                flash("Пациент не найден.", "error")
                return redirect(url_for("doctor_patients"))
            doctor_tests = connection.execute(
                "SELECT id, title FROM Tests WHERE audience = 'doctor' ORDER BY id"
            ).fetchall()
        return render_template(
            "doctor_tests.html",
            user=session_user(),
            patient_id=patient_id,
            full_name=_full_name(patient),
            tests=doctor_tests,
        )

    def _doctor_test_or_redirect(connection, patient_id, test_id):
        test, questions = _load_test(connection, test_id)
        if test is None or test["audience"] != "doctor":
            return None, None
        return test, questions

    @app.get("/doctor/patients/<int:patient_id>/tests/<int:test_id>")
    def doctor_take_test(patient_id: int, test_id: int):
        guard = _require_doctor()
        if guard is not None:
            return guard
        with connect_database() as connection:
            test, questions = _doctor_test_or_redirect(
                connection, patient_id, test_id
            )
        if test is None:
            flash("Тест не найден.", "error")
            return redirect(url_for("doctor_patient_tests", patient_id=patient_id))
        return render_template(
            "take_test.html",
            user=session_user(),
            test=test,
            questions=questions,
            action_url=url_for(
                "doctor_submit_test", patient_id=patient_id, test_id=test_id
            ),
            back_url=url_for("doctor_patient_tests", patient_id=patient_id),
            strict=(test["code"] != "C-SSRS"),
        )

    @app.post("/doctor/patients/<int:patient_id>/tests/<int:test_id>")
    def doctor_submit_test(patient_id: int, test_id: int):
        guard = _require_doctor()
        if guard is not None:
            return guard
        with connect_database() as connection:
            test, questions = _doctor_test_or_redirect(
                connection, patient_id, test_id
            )
            if test is None:
                flash("Тест не найден.", "error")
                return redirect(
                    url_for("doctor_patient_tests", patient_id=patient_id)
                )
            # Lenient validation: C-SSRS skips questions by branching, so we
            # record only the questions that were actually answered.
            answers: dict[int, int] = {}
            for question in questions:
                raw = request.form.get(f"q{question['id']}")
                if raw in (None, ""):
                    continue
                try:
                    value = int(raw)
                except ValueError:
                    continue
                if question["min_value"] <= value <= question["max_value"]:
                    answers[question["id"]] = value
            if not answers:
                flash("Ответьте хотя бы на один вопрос теста.", "error")
                return render_template(
                    "take_test.html",
                    user=session_user(),
                    test=test,
                    questions=questions,
                    action_url=url_for(
                        "doctor_submit_test",
                        patient_id=patient_id,
                        test_id=test_id,
                    ),
                    back_url=url_for(
                        "doctor_patient_tests", patient_id=patient_id
                    ),
                    strict=(test["code"] != "C-SSRS"),
                )
            total_score = sum(answers.values())
            cursor = connection.execute(
                """
                INSERT INTO Test_Results
                    (test_id, patient_id, created_at, total_score)
                VALUES (?, ?, ?, ?)
                """,
                (
                    test_id,
                    patient_id,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    total_score,
                ),
            )
            result_id = cursor.lastrowid
            for question_id, value in answers.items():
                connection.execute(
                    "INSERT INTO Test_Answers (result_id, question_id, value) "
                    "VALUES (?, ?, ?)",
                    (result_id, question_id, value),
                )
        flash("Результаты теста сохранены.", "success")
        return redirect(url_for("doctor_patient_stats", patient_id=patient_id))

    @app.get("/doctor/patients/<int:patient_id>/stats")
    def doctor_patient_stats(patient_id: int):
        guard = _require_doctor()
        if guard is not None:
            return guard
        ranges = {"week": 7, "2weeks": 14, "month": 30, "all": None}
        range_key = request.args.get("range", "all")
        if range_key not in ranges:
            range_key = "all"
        with connect_database() as connection:
            patient = _load_patient(connection, patient_id)
            if patient is None:
                flash("Пациент не найден.", "error")
                return redirect(url_for("doctor_patients"))
            doctor_tests = connection.execute(
                "SELECT id, code, title FROM Tests WHERE audience = 'doctor' "
                "ORDER BY id"
            ).fetchall()
            summaries = []
            for test in doctor_tests:
                results = connection.execute(
                    """
                    SELECT id, total_score, created_at FROM Test_Results
                    WHERE patient_id = ? AND test_id = ?
                    ORDER BY created_at DESC, id DESC
                    """,
                    (patient_id, test["id"]),
                ).fetchall()
                summaries.append(
                    {
                        "code": test["code"],
                        "last": results[0]["total_score"] if results else None,
                        "attempts": [
                            {
                                "id": row["id"],
                                "date": _fmt_datetime(row["created_at"]),
                                "score": row["total_score"],
                            }
                            for row in results
                        ],
                    }
                )
        entries = emotion_series(patient_id, days=ranges[range_key])
        return render_template(
            "doctor_stats.html",
            user=session_user(),
            patient_id=patient_id,
            full_name=_full_name(patient),
            summaries=summaries,
            entries=entries,
            range_key=range_key,
        )

    @app.get("/doctor/results/<int:result_id>")
    def doctor_result_detail(result_id: int):
        guard = _require_doctor()
        if guard is not None:
            return guard
        with connect_database() as connection:
            result = connection.execute(
                """
                SELECT r.id, r.created_at, r.total_score, r.patient_id,
                       t.id AS test_id, t.title, t.answer_kind
                FROM Test_Results r JOIN Tests t ON t.id = r.test_id
                WHERE r.id = ?
                """,
                (result_id,),
            ).fetchone()
            if result is None:
                flash("Результат не найден.", "error")
                return redirect(url_for("doctor_patients"))
            rows = connection.execute(
                """
                SELECT q.position, q.text, ta.value
                FROM Test_Questions q
                LEFT JOIN Test_Answers ta
                    ON ta.question_id = q.id AND ta.result_id = ?
                WHERE q.test_id = ?
                ORDER BY q.position, q.id
                """,
                (result_id, result["test_id"]),
            ).fetchall()
        return render_template(
            "result_detail.html",
            user=session_user(),
            result=result,
            date=_fmt_datetime(result["created_at"]),
            items=rows,
            patient_id=result["patient_id"],
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080,
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )

