import os
import sqlite3
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFProtect

from clinical_compass import SQLiteRepository


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(
    os.environ.get(
        "CLINICAL_COMPASS_DATABASE",
        BASE_DIR / "data" / "clinical_compass.sqlite3",
    )
)


class WebSQLiteRepository(SQLiteRepository):
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

    repository = WebSQLiteRepository(DATABASE_PATH)
    app.extensions["clinical_compass_repository"] = repository

    @app.get("/")
    def index():
        if "user_id" not in session:
            return redirect(url_for("login"))
        return redirect(url_for("dashboard"))

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
                flash("Пароль должен содержать не менее 8 символов.", "error")
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

            session.clear()
            session["user_id"] = user.id
            session["email"] = user.email
            session["name"] = user.name
            session["surname"] = user.surname
            session["role"] = "doctor" if is_doctor else "patient"
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
                flash("Неверная почта или пароль.", "error")
                return render_template("login.html")

            role = repository.get_user_role(user.id)
            if role is None:
                flash(
                    "Для аккаунта не настроен тип пользователя. Обратитесь к администратору.",
                    "error",
                )
                return render_template("login.html")

            session.clear()
            session["user_id"] = user.id
            session["email"] = user.email
            session["name"] = user.name
            session["surname"] = user.surname
            session["role"] = role
            return redirect(url_for("dashboard"))

        return render_template("login.html")

    @app.get("/dashboard")
    def dashboard():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") == "doctor":
            return render_template("doctor.html", user=session_user())
        if session.get("role") == "patient":
            return render_template("patient.html", user=session_user())
        session.clear()
        flash("Не удалось определить тип аккаунта. Войдите снова.", "error")
        return redirect(url_for("login"))

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

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
