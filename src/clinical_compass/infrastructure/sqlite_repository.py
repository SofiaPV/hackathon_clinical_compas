import hashlib
import hmac
import re
import json
import secrets
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from clinical_compass.domain.models import (
    Doctor,
    Drug,
    EmotionEntry,
    MedicationEvent,
    MedicationPrescription,
    MedicationSchedule,
    Patient,
    User,
)
from clinical_compass.repositories.clinical_compass_repository import (
    ClinicalCompassRepository,
)


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS Users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    surname TEXT NOT NULL,
    second_name TEXT,
    email TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS Doctors (
    id INTEGER PRIMARY KEY,
    info TEXT,
    FOREIGN KEY (id) REFERENCES Users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS Patients (
    id INTEGER PRIMARY KEY,
    info TEXT,
    FOREIGN KEY (id) REFERENCES Users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS Doctor_Has_Patient (
    doctor_id INTEGER NOT NULL,
    patient_id INTEGER NOT NULL,
    PRIMARY KEY (doctor_id, patient_id),
    FOREIGN KEY (doctor_id) REFERENCES Doctors(id) ON DELETE CASCADE,
    FOREIGN KEY (patient_id) REFERENCES Patients(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS Emotions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    anxiety INTEGER,
    mood INTEGER,
    energy INTEGER,
    description TEXT,
    FOREIGN KEY (user_id) REFERENCES Users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS Drugs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    prescription TEXT
);

CREATE TABLE IF NOT EXISTS Patient_has_Drugs (
    patient_id INTEGER NOT NULL,
    drug_id INTEGER NOT NULL,
    PRIMARY KEY (patient_id, drug_id),
    FOREIGN KEY (patient_id) REFERENCES Patients(id) ON DELETE CASCADE,
    FOREIGN KEY (drug_id) REFERENCES Drugs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS Medication_Prescriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    drug_id INTEGER NOT NULL,
    dosage TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    FOREIGN KEY (user_id) REFERENCES Patients(id) ON DELETE CASCADE,
    FOREIGN KEY (drug_id) REFERENCES Drugs(id) ON DELETE RESTRICT,
    CHECK (end_date IS NULL OR end_date >= start_date)
);

CREATE TABLE IF NOT EXISTS Medication_Schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prescription_id INTEGER NOT NULL UNIQUE,
    schedule_config TEXT NOT NULL,
    FOREIGN KEY (prescription_id)
        REFERENCES Medication_Prescriptions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS Medication_Events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prescription_id INTEGER NOT NULL,
    taken_at TEXT NOT NULL,
    FOREIGN KEY (prescription_id)
        REFERENCES Medication_Prescriptions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_emotions_user_created
    ON Emotions(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_prescriptions_user
    ON Medication_Prescriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_events_prescription_taken
    ON Medication_Events(prescription_id, taken_at);
"""


class SQLiteRepository(ClinicalCompassRepository):
    def __init__(self, database_path: str | Path = "clinical_compass.sqlite3") -> None:
        self._database_path = str(database_path)
        if self._database_path != ":memory:":
            Path(self._database_path).expanduser().resolve().parent.mkdir(
                parents=True,
                exist_ok=True,
            )
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(Users)")
            }
            if "email" not in columns:
                # Existing accounts are retained; their email must be filled in
                # before they can use email-based authentication.
                connection.execute("ALTER TABLE Users ADD COLUMN email TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON Users(email)"
            )

    @staticmethod
    def _hash_password(password: str) -> str:
        if not password:
            raise ValueError("Пароль не должен быть пустым.")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            310_000,
        )
        return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"

    @staticmethod
    def _verify_password(password: str, password_hash: str) -> bool:
        """Compare a password with its stored PBKDF2 hash."""
        try:
            algorithm, salt_hex, digest_hex = password_hash.split("$", maxsplit=2)
            if algorithm != "pbkdf2_sha256":
                return False
            salt = bytes.fromhex(salt_hex)
            expected_digest = bytes.fromhex(digest_hex)
        except (ValueError, AttributeError):
            return False

        actual_digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            310_000,
        )
        return hmac.compare_digest(actual_digest, expected_digest)

    @staticmethod
    def _normalize_email(email: str) -> str:
        normalized = email.strip().casefold()
        email_pattern = r"[^@\s]+@[^@\s]+\.[^@\s]+"
        if not normalized or not re.fullmatch(email_pattern, normalized):
            raise ValueError("Укажите корректный email.")
        return normalized

    @staticmethod
    def _validate_scale(value: int | None, field_name: str) -> None:
        if value is not None and not 0 <= value <= 10:
            raise ValueError(f"{field_name} должен быть в диапазоне от 0 до 10.")

    @staticmethod
    def _datetime_to_text(value: datetime) -> str:
        return value.isoformat(timespec="seconds")

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            name=row["name"],
            surname=row["surname"],
            second_name=row["second_name"],
            email=row["email"],
        )

    @staticmethod
    def _row_to_emotion(row: sqlite3.Row) -> EmotionEntry:
        return EmotionEntry(
            id=row["id"],
            user_id=row["user_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            anxiety=row["anxiety"],
            mood=row["mood"],
            energy=row["energy"],
            description=row["description"],
        )

    @staticmethod
    def _row_to_drug(row: sqlite3.Row) -> Drug:
        return Drug(
            id=row["id"],
            name=row["name"],
            prescription=row["prescription"],
        )

    @staticmethod
    def _row_to_prescription(row: sqlite3.Row) -> MedicationPrescription:
        return MedicationPrescription(
            id=row["id"],
            user_id=row["user_id"],
            drug_id=row["drug_id"],
            dosage=row["dosage"],
            start_date=date.fromisoformat(row["start_date"]),
            end_date=(
                date.fromisoformat(row["end_date"])
                if row["end_date"] is not None
                else None
            ),
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> MedicationEvent:
        return MedicationEvent(
            id=row["id"],
            prescription_id=row["prescription_id"],
            taken_at=datetime.fromisoformat(row["taken_at"]),
        )

    def create_user(
        self,
        name: str,
        surname: str,
        password: str,
        second_name: str | None = None,
        email: str | None = None,
    ) -> User:
        if not name.strip() or not surname.strip():
            raise ValueError("Имя и фамилия обязательны.")
        if email is None:
            raise ValueError("Email обязателен для регистрации.")
        normalized_email = self._normalize_email(email)
        password_hash = self._hash_password(password)
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO Users (
                        name, surname, second_name, email, password
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        name.strip(),
                        surname.strip(),
                        second_name.strip() if second_name else None,
                        normalized_email,
                        password_hash,
                    ),
                )
            except sqlite3.IntegrityError as error:
                if "Users.email" in str(error) or "idx_users_email" in str(error):
                    raise ValueError(
                        "Пользователь с таким email уже существует."
                    ) from error
                raise
            row = connection.execute(
                "SELECT * FROM Users WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return self._row_to_user(row)

    def get_user_by_email(self, email: str) -> User | None:
        """Return a public user record by email, without exposing the password hash."""
        normalized_email = self._normalize_email(email)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM Users WHERE email = ?",
                (normalized_email,),
            ).fetchone()
            return self._row_to_user(row) if row is not None else None

    def authenticate_user(self, email: str, password: str) -> User | None:
        """Authenticate a user; return None for unknown email or wrong password."""
        if not password:
            return None
        normalized_email = self._normalize_email(email)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM Users WHERE email = ?",
                (normalized_email,),
            ).fetchone()
            if row is None or not self._verify_password(password, row["password"]):
                return None
            return self._row_to_user(row)

    def change_password(
        self,
        user_id: int,
        current_password: str,
        new_password: str,
    ) -> bool:
        """Change password only when the current password is valid."""
        if not new_password:
            raise ValueError("Новый пароль не должен быть пустым.")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT password FROM Users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None or not self._verify_password(
                current_password,
                row["password"],
            ):
                return False
            connection.execute(
                "UPDATE Users SET password = ? WHERE id = ?",
                (self._hash_password(new_password), user_id),
            )
            return True

    def update_user_email(self, user_id: int, email: str) -> User:
        """Update an account email, preserving case-insensitive uniqueness."""
        normalized_email = self._normalize_email(email)
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    "UPDATE Users SET email = ? WHERE id = ?",
                    (normalized_email, user_id),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    "Пользователь с таким email уже существует."
                ) from error
            if cursor.rowcount == 0:
                raise ValueError("Пользователь не найден.")
            row = connection.execute(
                "SELECT * FROM Users WHERE id = ?",
                (user_id,),
            ).fetchone()
            return self._row_to_user(row)

    def create_patient(self, user_id: int, info: str | None = None) -> Patient:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO Patients (id, info) VALUES (?, ?)",
                (user_id, info),
            )
        return Patient(id=user_id, info=info)

    def create_doctor(self, user_id: int, info: str | None = None) -> Doctor:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO Doctors (id, info) VALUES (?, ?)",
                (user_id, info),
            )
        return Doctor(id=user_id, info=info)

    def link_doctor_patient(self, doctor_id: int, patient_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO Doctor_Has_Patient (doctor_id, patient_id)
                VALUES (?, ?)
                """,
                (doctor_id, patient_id),
            )

    def create_emotion(
        self,
        user_id: int,
        anxiety: int | None = None,
        mood: int | None = None,
        energy: int | None = None,
        description: str | None = None,
        created_at: datetime | None = None,
    ) -> EmotionEntry:
        self._validate_scale(anxiety, "anxiety")
        self._validate_scale(mood, "mood")
        self._validate_scale(energy, "energy")
        timestamp = created_at or datetime.now().astimezone()
        timestamp_text = self._datetime_to_text(timestamp)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO Emotions
                    (user_id, created_at, anxiety, mood, energy, description)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    timestamp_text,
                    anxiety,
                    mood,
                    energy,
                    description,
                ),
            )
            row = connection.execute(
                "SELECT * FROM Emotions WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return self._row_to_emotion(row)

    def list_emotions(self, user_id: int) -> list[EmotionEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM Emotions
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
            return [self._row_to_emotion(row) for row in rows]

    def list_patient_emotions_for_doctor(
        self,
        doctor_id: int,
        patient_id: int,
    ) -> list[EmotionEntry]:
        with self._connect() as connection:
            link = connection.execute(
                """
                SELECT 1 FROM Doctor_Has_Patient
                WHERE doctor_id = ? AND patient_id = ?
                """,
                (doctor_id, patient_id),
            ).fetchone()
            if link is None:
                raise PermissionError("У врача нет доступа к данным этого пациента.")
            rows = connection.execute(
                """
                SELECT * FROM Emotions
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (patient_id,),
            ).fetchall()
            return [self._row_to_emotion(row) for row in rows]

    def create_drug(self, name: str, prescription: str | None = None) -> Drug:
        if not name.strip():
            raise ValueError("Название препарата обязательно.")
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO Drugs (name, prescription) VALUES (?, ?)",
                (name.strip(), prescription),
            )
            row = connection.execute(
                "SELECT * FROM Drugs WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return self._row_to_drug(row)

    def link_patient_drug(self, patient_id: int, drug_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO Patient_has_Drugs (patient_id, drug_id)
                VALUES (?, ?)
                """,
                (patient_id, drug_id),
            )

    def create_medication_prescription(
        self,
        patient_id: int,
        drug_id: int,
        dosage: str,
        start_date: date,
        end_date: date | None = None,
    ) -> MedicationPrescription:
        if not dosage.strip():
            raise ValueError("Дозировка обязательна.")
        if end_date is not None and end_date < start_date:
            raise ValueError("Дата окончания не может быть раньше даты начала.")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO Patient_has_Drugs (patient_id, drug_id)
                VALUES (?, ?)
                """,
                (patient_id, drug_id),
            )
            cursor = connection.execute(
                """
                INSERT INTO Medication_Prescriptions
                    (user_id, drug_id, dosage, start_date, end_date)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
                    drug_id,
                    dosage.strip(),
                    start_date.isoformat(),
                    end_date.isoformat() if end_date else None,
                ),
            )
            row = connection.execute(
                "SELECT * FROM Medication_Prescriptions WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return self._row_to_prescription(row)

    def set_medication_schedule(
        self,
        prescription_id: int,
        schedule_config: dict[str, Any],
    ) -> MedicationSchedule:
        if not isinstance(schedule_config, dict) or not schedule_config:
            raise ValueError("Расписание должно быть непустым JSON-совместимым объектом.")
        try:
            config_text = json.dumps(
                schedule_config,
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("Расписание должно содержать только JSON-совместимые данные.") from error
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO Medication_Schedules (prescription_id, schedule_config)
                VALUES (?, ?)
                ON CONFLICT(prescription_id)
                DO UPDATE SET schedule_config = excluded.schedule_config
                """,
                (prescription_id, config_text),
            )
            row = connection.execute(
                """
                SELECT * FROM Medication_Schedules
                WHERE prescription_id = ?
                """,
                (prescription_id,),
            ).fetchone()
            return MedicationSchedule(
                id=row["id"],
                prescription_id=row["prescription_id"],
                schedule_config=json.loads(row["schedule_config"]),
            )

    def record_medication_event(
        self,
        prescription_id: int,
        taken_at: datetime | None = None,
    ) -> MedicationEvent:
        timestamp = taken_at or datetime.now().astimezone()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO Medication_Events (prescription_id, taken_at)
                VALUES (?, ?)
                """,
                (prescription_id, self._datetime_to_text(timestamp)),
            )
            row = connection.execute(
                "SELECT * FROM Medication_Events WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return self._row_to_event(row)

    def list_medication_events(
        self,
        prescription_id: int,
    ) -> list[MedicationEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM Medication_Events
                WHERE prescription_id = ?
                ORDER BY taken_at DESC, id DESC
                """,
                (prescription_id,),
            ).fetchall()
            return [self._row_to_event(row) for row in rows]
