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
                    severity_bands TEXT
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
            # Migration for databases created before severity_bands existed.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(Tests)")}
            if "severity_bands" not in columns:
                connection.execute("ALTER TABLE Tests ADD COLUMN severity_bands TEXT")
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
                         answer_kind, critical_message, severity_bands)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            return render_template(
                "doctor.html", user=session_user(), patients=patients
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
            SELECT ?, id, ? FROM Tests
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
            WHERE t.critical_message IS NOT NULL
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

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )

