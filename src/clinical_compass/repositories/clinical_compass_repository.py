from abc import ABC, abstractmethod
from datetime import date, datetime
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


class ClinicalCompassRepository(ABC):
    @abstractmethod
    def create_user(
        self,
        name: str,
        surname: str,
        password: str,
        second_name: str | None = None,
    ) -> User:
        raise NotImplementedError

    @abstractmethod
    def create_patient(self, user_id: int, info: str | None = None) -> Patient:
        raise NotImplementedError

    @abstractmethod
    def create_doctor(self, user_id: int, info: str | None = None) -> Doctor:
        raise NotImplementedError

    @abstractmethod
    def link_doctor_patient(self, doctor_id: int, patient_id: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_emotion(
        self,
        user_id: int,
        anxiety: int | None = None,
        mood: int | None = None,
        energy: int | None = None,
        description: str | None = None,
        created_at: datetime | None = None,
    ) -> EmotionEntry:
        raise NotImplementedError

    @abstractmethod
    def list_emotions(self, user_id: int) -> list[EmotionEntry]:
        raise NotImplementedError

    @abstractmethod
    def list_patient_emotions_for_doctor(
        self,
        doctor_id: int,
        patient_id: int,
    ) -> list[EmotionEntry]:
        raise NotImplementedError

    @abstractmethod
    def create_drug(self, name: str, prescription: str | None = None) -> Drug:
        raise NotImplementedError

    @abstractmethod
    def link_patient_drug(self, patient_id: int, drug_id: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_medication_prescription(
        self,
        patient_id: int,
        drug_id: int,
        dosage: str,
        start_date: date,
        end_date: date | None = None,
    ) -> MedicationPrescription:
        raise NotImplementedError

    @abstractmethod
    def set_medication_schedule(
        self,
        prescription_id: int,
        schedule_config: dict[str, Any],
    ) -> MedicationSchedule:
        raise NotImplementedError

    @abstractmethod
    def record_medication_event(
        self,
        prescription_id: int,
        taken_at: datetime | None = None,
    ) -> MedicationEvent:
        raise NotImplementedError

    @abstractmethod
    def list_medication_events(
        self,
        prescription_id: int,
    ) -> list[MedicationEvent]:
        raise NotImplementedError
