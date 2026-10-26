import hashlib
import io
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
            entries = emotion_series(session["user_id"], days=14)
            return render_template(
                "patient.html",
                user=session_user(),
                doctors=doctors,
                entries=entries,
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

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )

