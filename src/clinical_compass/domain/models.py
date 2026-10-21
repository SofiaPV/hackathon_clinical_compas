from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class User:
    id: int
    name: str
    surname: str
    second_name: str | None = None
    email: str | None = None


@dataclass(frozen=True, slots=True)
class Patient:
    id: int
    info: str | None = None


@dataclass(frozen=True, slots=True)
class Doctor:
    id: int
    info: str | None = None


@dataclass(frozen=True, slots=True)
class EmotionEntry:
    id: int
    user_id: int
    created_at: datetime
    anxiety: int | None
    mood: int | None
    energy: int | None
    description: str | None


@dataclass(frozen=True, slots=True)
class Drug:
    id: int
    name: str
    prescription: str | None = None


@dataclass(frozen=True, slots=True)
class MedicationPrescription:
    id: int
    user_id: int
    drug_id: int
    dosage: str
    start_date: date
    end_date: date | None


@dataclass(frozen=True, slots=True)
class MedicationSchedule:
    id: int
    prescription_id: int
    schedule_config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MedicationEvent:
    id: int
    prescription_id: int
    taken_at: datetime
