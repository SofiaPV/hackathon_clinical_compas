from datetime import date, datetime, time

from clinical_compass import SQLiteRepository


def main() -> None:
    repository = SQLiteRepository("data/clinical_compass.sqlite3")

    doctor_user = repository.create_user(
        name="Анна",
        surname="Смирнова",
        password="change-this-password",
        email="some@gmail.com"
    )
    doctor = repository.create_doctor(
        user_id=doctor_user.id,
        info="Врач-психиатр",
    )

    patient_user = repository.create_user(
        name="Иван",
        surname="Петров",
        password="another-change-this-password",
        email="example@gmail.com"
    )
    patient = repository.create_patient(
        user_id=patient_user.id,
        info="",
    )
    repository.link_doctor_patient(doctor.id, patient.id)

    entry = repository.create_emotion(
        user_id=patient.id,
        anxiety=6,
        mood=4,
        energy=3,
        description="Тревожность усилилась к вечеру.",
    )
    print("Запись дневника:", entry)

    drug = repository.create_drug(
        name="Пример препарата",
        prescription="Уточнить статус и инструкцию по официальным данным.",
    )
    prescription = repository.create_medication_prescription(
        patient_id=patient.id,
        drug_id=drug.id,
        dosage="Согласно назначению лечащего врача",
        start_date=date.today(),
    )
    repository.set_medication_schedule(
        prescription_id=prescription.id,
        schedule_config={
            "version": 1,
            "frequency": "daily",
            "times": ["09:00", "21:00"],
            "timezone": "Europe/Moscow",
        },
    )
    repository.record_medication_event(
        prescription_id=prescription.id,
        taken_at=datetime.combine(date.today(), time(9, 0)).astimezone(),
    )

    doctor_view = repository.list_patient_emotions_for_doctor(
        doctor_id=doctor.id,
        patient_id=patient.id,
    )
    print("Записи пациента, доступные врачу:", doctor_view)
    print(
        "События приема:",
        repository.list_medication_events(prescription.id),
    )


if __name__ == "__main__":
    main()
