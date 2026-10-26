"""Integration checks against an isolated SQLite database.

Run from the project root:
    PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
"""

import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from flask import template_rendered


NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW.astimezone().replace(tzinfo=None)
        return NOW.astimezone(tz)


class ChartMountParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.entries = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "test-score-chart" in attributes.get("class", "").split():
            self.entries.append(json.loads(attributes["data-entries"]))


class TestStatisticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Importing web.app creates its default app. Redirect that database
        # before the import so tests never initialize the developer's database.
        cls.bootstrap_directory = tempfile.TemporaryDirectory()
        with patch.dict(
            os.environ,
            {
                "SECRET_KEY": "statistics-test-secret",
                "CLINICAL_COMPASS_DATABASE": str(
                    Path(cls.bootstrap_directory.name) / "bootstrap.sqlite3"
                ),
            },
        ):
            cls.web_app = importlib.import_module("web.app")

    @classmethod
    def tearDownClass(cls):
        cls.bootstrap_directory.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "test.sqlite3"
        database_patch = patch.object(self.web_app, "DATABASE_PATH", self.database_path)
        database_patch.start()
        self.addCleanup(database_patch.stop)
        clock_patch = patch.object(self.web_app, "datetime", FrozenDateTime)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        with patch.dict(os.environ, {"SECRET_KEY": "statistics-test-secret"}):
            self.app = self.web_app.create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = self.app.test_client()
        with self.connect() as connection:
            connection.executemany(
                "INSERT INTO Users (id, name, surname, email, password) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "Анна", "Пациент", "patient@example.test", "unused"),
                    (2, "Борис", "Пациент", "other@example.test", "unused"),
                    (3, "Вера", "Врач", "doctor@example.test", "unused"),
                    (4, "Глеб", "Врач", "other-doctor@example.test", "unused"),
                    (5, "Дарья", "Врач", "new-doctor@example.test", "unused"),
                ],
            )
            connection.executemany("INSERT INTO Patients (id) VALUES (?)", [(1,), (2,)])
            connection.executemany("INSERT INTO Doctors (id) VALUES (?)", [(3,), (4,), (5,)])
            connection.executemany(
                "INSERT INTO Doctor_Has_Patient (doctor_id, patient_id) VALUES (?, ?)",
                [(3, 1), (4, 2)],
            )
            self.test_ids = {
                row["code"]: row["id"] for row in connection.execute("SELECT id, code FROM Tests")
            }
        self.sign_in()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def sign_in(self, role="patient", user_id=1):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(user_id=user_id, role=role, name="Анна", surname="Пациент")

    def add_result(self, score, *, code="PHQ_9", moment=NOW, patient_id=1):
        timestamp = moment.isoformat() if isinstance(moment, datetime) else moment
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO Test_Results (test_id, patient_id, created_at, total_score) "
                "VALUES (?, ?, ?, ?)",
                (self.test_ids[code], patient_id, timestamp, score),
            )
            return cursor.lastrowid

    def render(self, path):
        rendered = []

        def record(sender, template, context, **extra):
            rendered.append((template.name, context))

        template_rendered.connect(record, self.app)
        try:
            response = self.client.get(path)
        finally:
            template_rendered.disconnect(record, self.app)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(rendered, path)
        return response, rendered[-1][1]

    def stats(self, query="all", *, doctor=False):
        if doctor:
            self.sign_in("doctor", 3)
            path = "/doctor/patients/1/stats"
        else:
            self.sign_in()
            path = "/stats"
        if query is not None:
            path += "?range=" + query
        return self.render(path)[1]

    def by_code(self, context):
        return {item["code"]: item for item in context["test_statistics"]}

    def test_empty_catalogue_has_individual_scales_and_no_clinician_tests(self):
        for doctor in (False, True):
            with self.subTest(doctor=doctor):
                statistics = self.by_code(self.stats(doctor=doctor))
                self.assertEqual(set(statistics), {"PHQ_9", "GAD-7"})
                self.assertEqual(statistics["PHQ_9"]["max_score"], 27)
                self.assertEqual(statistics["GAD-7"]["max_score"], 21)
                for item in statistics.values():
                    self.assertEqual(item["min_score"], 0)
                    self.assertEqual(item["count"], 0)
                    self.assertIsNone(item["last"])
                    self.assertIsNone(item["delta"])
                    self.assertEqual(item["entries"], [])
                    self.assertEqual(item["attempts"], [])

    def test_single_zero_score_is_a_completed_attempt(self):
        result_id = self.add_result(0)
        context = self.stats()
        phq = self.by_code(context)["PHQ_9"]
        self.assertEqual(phq["count"], 1)
        self.assertEqual(phq["last"]["id"], result_id)
        self.assertEqual(phq["last"]["score"], 0)
        self.assertIsNone(phq["delta"])
        self.assertEqual([entry["score"] for entry in phq["entries"]], [0])

    def test_history_and_delta_keep_repeated_attempts_at_the_same_time(self):
        oldest = self.add_result(12, moment=NOW - timedelta(days=2))
        middle = self.add_result(8)
        latest = self.add_result(0)
        phq = self.by_code(self.stats())["PHQ_9"]
        self.assertEqual(phq["count"], 3)
        self.assertEqual([item["id"] for item in phq["attempts"]], [latest, middle, oldest])
        self.assertEqual([item["score"] for item in phq["entries"]], [12, 8, 0])
        self.assertEqual(phq["last"]["id"], latest)
        self.assertEqual(phq["delta"], -8)

    def test_all_ranges_filter_history_chart_and_summary_for_both_roles(self):
        for days, score in ((40, 20), (20, 15), (10, 10), (4, 5)):
            self.add_result(score, moment=NOW - timedelta(days=days))
        for range_key, expected in (
            ("week", [5]), ("2weeks", [10, 5]), ("month", [15, 10, 5]), ("all", [20, 15, 10, 5])
        ):
            patient_context = self.stats(range_key)
            doctor_context = self.stats(range_key, doctor=True)
            with self.subTest(range=range_key):
                self.assertEqual(patient_context["test_statistics"], doctor_context["test_statistics"])
                phq = self.by_code(patient_context)["PHQ_9"]
                self.assertEqual(phq["count"], len(expected))
                self.assertEqual([entry["score"] for entry in phq["entries"]], expected)
                self.assertEqual([entry["score"] for entry in phq["attempts"]], expected[::-1])
                self.assertEqual(phq["last"]["score"], 5)
                self.assertEqual(phq["delta"], None if len(expected) == 1 else -5)

    def test_cutoff_is_inclusive_and_compares_instants_across_timezones(self):
        cutoff = NOW - timedelta(days=7)
        self.add_result(17, moment=cutoff - timedelta(seconds=1))
        included = self.add_result(9, moment=cutoff.astimezone(timezone(timedelta(hours=-8))))
        phq = self.by_code(self.stats("week"))["PHQ_9"]
        self.assertEqual(phq["count"], 1)
        self.assertEqual(phq["last"]["id"], included)
        self.assertIsNone(phq["delta"])

    def test_chronology_uses_real_time_and_accepts_legacy_naive_timestamps(self):
        # Lexicographic ISO sorting would incorrectly put 12:00+03 after 10:00Z.
        first = self.add_result(10, moment="2026-10-26T12:00:00+03:00")
        second = self.add_result(7, moment="2026-10-26T10:00:00+00:00")
        naive_local = (NOW - timedelta(minutes=30)).astimezone().replace(tzinfo=None)
        last = self.add_result(3, moment=naive_local)
        phq = self.by_code(self.stats())["PHQ_9"]
        self.assertEqual([item["id"] for item in phq["attempts"]], [last, second, first])
        self.assertEqual([item["score"] for item in phq["entries"]], [10, 7, 3])
        self.assertEqual(phq["delta"], -4)

    def test_statistic_and_both_result_details_use_the_same_local_datetime(self):
        source = "2026-10-26T00:15:00+10:00"
        expected = datetime.fromisoformat(source).astimezone()
        result_id = self.add_result(5, moment=source)
        last = self.by_code(self.stats())["PHQ_9"]["last"]
        self.assertEqual(last["t"], expected.isoformat())
        self.assertEqual(last["date"], expected.strftime("%d.%m.%Y %H:%M"))
        _, patient_detail = self.render(f"/tests/results/{result_id}")
        self.sign_in("doctor", 3)
        _, doctor_detail = self.render(f"/doctor/results/{result_id}")
        self.assertEqual(last["date"], patient_detail["date"])
        self.assertEqual(last["date"], doctor_detail["date"])

    def test_range_defaults_and_invalid_values(self):
        self.add_result(10, moment=NOW - timedelta(days=20))
        for query in (None, "invalid"):
            with self.subTest(query=query):
                patient_context = self.stats(query)
                doctor_context = self.stats(query, doctor=True)
                self.assertEqual(patient_context["range_key"], "week")
                self.assertEqual(doctor_context["range_key"], "all")
                self.assertEqual(self.by_code(patient_context)["PHQ_9"]["count"], 0)
                self.assertEqual(self.by_code(doctor_context)["PHQ_9"]["count"], 1)

    def test_patient_isolation_and_statistics_do_not_require_assignments(self):
        own_id = self.add_result(6)
        self.add_result(27, patient_id=2)
        self.add_result(4, code="HAM-A")
        context = self.stats()
        self.assertEqual(self.by_code(context)["PHQ_9"]["last"]["id"], own_id)
        self.assertEqual(sum(item["count"] for item in context["test_statistics"]), 1)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM Patient_Has_Test").fetchone()[0], 0)

    def test_new_patient_test_uses_catalogue_question_limits(self):
        with self.connect() as connection:
            test_id = connection.execute(
                "INSERT INTO Tests (code, title, description, schedule_days, audience) "
                "VALUES ('NEW', 'Новый тест', '', 14, 'patient')"
            ).lastrowid
            connection.executemany(
                "INSERT INTO Test_Questions (test_id, position, text, min_value, max_value) "
                "VALUES (?, ?, ?, ?, ?)",
                [(test_id, 1, "Первый", 1, 4), (test_id, 2, "Второй", 2, 5)],
            )
        item = self.by_code(self.stats())["NEW"]
        self.assertEqual(item["min_score"], 3)
        self.assertEqual(item["max_score"], 9)
        self.assertEqual(item["count"], 0)

    def test_patient_result_detail_shows_own_answers_and_keeps_range(self):
        result_id = self.add_result(0)
        with self.connect() as connection:
            question_id = connection.execute(
                "SELECT id FROM Test_Questions WHERE test_id = ? ORDER BY position LIMIT 1",
                (self.test_ids["PHQ_9"],),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO Test_Answers (result_id, question_id, value) VALUES (?, ?, 0)",
                (result_id, question_id),
            )
        response, context = self.render(f"/tests/results/{result_id}?range=month")
        self.assertEqual(context["result"]["id"], result_id)
        self.assertEqual(context["items"][0]["value"], 0)
        self.assertEqual(len(context["items"]), 9)
        self.assertEqual(context["back_url"], "/stats?range=month")
        self.assertIn('/stats?range=month', response.get_data(as_text=True))
        _, fallback_context = self.render(f"/tests/results/{result_id}?range=invalid")
        self.assertEqual(fallback_context["back_url"], "/stats?range=week")

    def test_patient_cannot_read_other_patients_or_clinician_results(self):
        other_id = self.add_result(23, patient_id=2)
        clinician_id = self.add_result(42, code="HAM-A")
        for result_id in (other_id, clinician_id, 99999):
            with self.subTest(result_id=result_id):
                response = self.client.get(f"/tests/results/{result_id}")
                self.assertIn(response.status_code, (302, 403, 404))
                self.assertNotIn('class="answers-list"', response.get_data(as_text=True))

    def test_stats_and_result_routes_require_the_correct_role(self):
        result_id = self.add_result(8)
        with self.client.session_transaction() as session:
            session.clear()
        for path in ("/stats", f"/tests/results/{result_id}", "/doctor/patients/1/stats"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])
        self.sign_in("doctor", 3)
        self.assertEqual(self.client.get("/stats").status_code, 302)
        self.assertEqual(self.client.get(f"/tests/results/{result_id}").status_code, 302)
        self.sign_in()
        self.assertEqual(self.client.get("/doctor/patients/1/stats").status_code, 302)
        self.assertEqual(self.client.get(f"/doctor/results/{result_id}").status_code, 302)

    def test_doctor_can_open_patient_test_answers_and_return_to_selected_period(self):
        result_id = self.add_result(8)
        self.sign_in("doctor", 3)
        _, context = self.render(f"/doctor/results/{result_id}?range=2weeks")
        self.assertEqual(context["result"]["patient_id"], 1)
        self.assertEqual(context["back_url"], "/doctor/patients/1/stats?range=2weeks")

    def test_incomplete_submission_does_not_become_a_completed_test(self):
        test_id = self.test_ids["PHQ_9"]
        response = self.client.post(f"/tests/{test_id}", data={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.by_code(self.stats())["PHQ_9"]["count"], 0)
        with self.connect() as connection:
            answers = {
                f"q{row['id']}": "0"
                for row in connection.execute("SELECT id FROM Test_Questions WHERE test_id = ?", (test_id,))
            }
        response = self.client.post(f"/tests/{test_id}", data=answers)
        self.assertEqual(response.status_code, 302)
        item = self.by_code(self.stats())["PHQ_9"]
        self.assertEqual(item["count"], 1)
        self.assertEqual(item["last"]["score"], 0)

    def test_dashboard_links_to_statistics_without_mood_entries(self):
        self.add_result(4)
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/stats"', response.get_data(as_text=True))

    def test_patient_dashboard_renders_own_test_graphs_beyond_the_mood_window(self):
        self.add_result(18, moment=NOW - timedelta(days=45))
        own_result = self.add_result(9, moment=NOW - timedelta(days=31))
        self.add_result(27, patient_id=2)
        self.add_result(21, code="GAD-7", patient_id=2)
        response, context = self.render("/dashboard")
        self.assertEqual(context["entries"], [])
        statistics = self.by_code(context)
        self.assertEqual(statistics["PHQ_9"]["count"], 2)
        self.assertEqual(statistics["PHQ_9"]["last"]["id"], own_result)
        self.assertEqual(statistics["GAD-7"]["count"], 0)
        markup = response.get_data(as_text=True)
        parser = ChartMountParser()
        parser.feed(markup)
        self.assertEqual(
            [[entry["score"] for entry in entries] for entries in parser.entries if entries],
            [[18, 9]],
        )
        self.assertIn("MedCharts.initTestCharts()", markup)

    def test_doctor_dashboard_and_patient_list_include_only_linked_patients(self):
        for doctor_id, expected_ids in ((3, [1]), (4, [2]), (5, [])):
            with self.subTest(doctor_id=doctor_id):
                self.sign_in("doctor", doctor_id)
                _, dashboard = self.render("/dashboard")
                _, patients = self.render("/doctor/patients")
                self.assertEqual([row["id"] for row in dashboard["patients"]], expected_ids)
                self.assertEqual([row["id"] for row in patients["cards"]], expected_ids)
                expected_counts = {"red": 0, "yellow": 0, "white": len(expected_ids), "green": 0}
                self.assertEqual(dashboard["counts"], expected_counts)
                self.assertEqual(patients["counts"], expected_counts)

    def test_doctor_cannot_read_unlinked_or_missing_patient_routes(self):
        results = {
            1: [self.add_result(6), self.add_result(12, code="HAM-A")],
            2: [self.add_result(20, patient_id=2), self.add_result(34, code="HAM-A", patient_id=2)],
        }
        test_id = self.test_ids["HAM-A"]
        for doctor_id, patient_id in ((3, 2), (4, 1), (5, 1), (3, 99999)):
            self.sign_in("doctor", doctor_id)
            paths = [
                f"/doctor/patients/{patient_id}",
                f"/doctor/patients/{patient_id}/stats",
                f"/doctor/patients/{patient_id}/tests",
                f"/doctor/patients/{patient_id}/tests/{test_id}",
            ]
            paths.extend(f"/doctor/results/{result_id}" for result_id in results.get(patient_id, [99999]))
            for path in paths:
                with self.subTest(doctor_id=doctor_id, path=path):
                    self.assertIn(self.client.get(path).status_code, (302, 403, 404))

    def clinician_answers(self):
        with self.connect() as connection:
            return {
                f"q{row['id']}": "1"
                for row in connection.execute(
                    "SELECT id FROM Test_Questions WHERE test_id = ?", (self.test_ids["HAM-A"],)
                )
            }

    def result_counts(self):
        with self.connect() as connection:
            return (
                connection.execute("SELECT COUNT(*) FROM Test_Results").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM Test_Answers").fetchone()[0],
            )

    def test_doctor_cannot_submit_tests_for_unlinked_or_missing_patients(self):
        answers = self.clinician_answers()
        test_id = self.test_ids["HAM-A"]
        original_counts = self.result_counts()
        for doctor_id, patient_id in ((3, 2), (4, 1), (5, 1), (3, 99999)):
            with self.subTest(doctor_id=doctor_id, patient_id=patient_id):
                self.sign_in("doctor", doctor_id)
                response = self.client.post(
                    f"/doctor/patients/{patient_id}/tests/{test_id}", data=answers
                )
                self.assertIn(response.status_code, (302, 403, 404))
                self.assertEqual(self.result_counts(), original_counts)

    def test_linked_doctor_can_view_patient_and_complete_a_clinician_test(self):
        self.sign_in("doctor", 3)
        test_id = self.test_ids["HAM-A"]
        for path in (
            "/doctor/patients/1", "/doctor/patients/1/stats", "/doctor/patients/1/tests",
            f"/doctor/patients/1/tests/{test_id}",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        answers = self.clinician_answers()
        response = self.client.post(f"/doctor/patients/1/tests/{test_id}", data=answers)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/doctor/patients/1/stats")
        self.assertEqual(self.result_counts(), (1, len(answers)))
        with self.connect() as connection:
            result = connection.execute("SELECT * FROM Test_Results").fetchone()
        self.assertEqual(result["patient_id"], 1)
        self.assertEqual(result["total_score"], len(answers))
        self.assertEqual(self.client.get(f"/doctor/results/{result['id']}").status_code, 200)

    def test_multiple_linked_doctors_can_access_the_same_patient(self):
        with self.connect() as connection:
            connection.execute("INSERT INTO Doctor_Has_Patient VALUES (4, 1)")
        result_id = self.add_result(6)
        for doctor_id, expected_ids in ((3, {1}), (4, {1, 2})):
            with self.subTest(doctor_id=doctor_id):
                self.sign_in("doctor", doctor_id)
                _, context = self.render("/doctor/patients")
                self.assertEqual({card["id"] for card in context["cards"]}, expected_ids)
                self.assertEqual(sum(context["counts"].values()), len(expected_ids))
                self.assertEqual(self.client.get("/doctor/patients/1/stats").status_code, 200)
                self.assertEqual(self.client.get(f"/doctor/results/{result_id}").status_code, 200)

    def test_removing_doctor_patient_link_revokes_read_and_write_access(self):
        result_id = self.add_result(6)
        self.sign_in("doctor", 3)
        self.assertEqual(self.client.get("/doctor/patients/1/stats").status_code, 200)
        with self.connect() as connection:
            connection.execute("DELETE FROM Doctor_Has_Patient WHERE doctor_id = 3 AND patient_id = 1")
        _, dashboard = self.render("/dashboard")
        _, listing = self.render("/doctor/patients")
        self.assertEqual(dashboard["patients"], [])
        self.assertEqual(listing["cards"], [])
        self.assertEqual(sum(dashboard["counts"].values()), 0)
        self.assertEqual(sum(listing["counts"].values()), 0)
        test_path = f"/doctor/patients/1/tests/{self.test_ids['HAM-A']}"
        for path in (
            "/doctor/patients/1", "/doctor/patients/1/stats", "/doctor/patients/1/tests",
            test_path, f"/doctor/results/{result_id}",
        ):
            with self.subTest(path=path):
                self.assertIn(self.client.get(path).status_code, (302, 403, 404))
        before = self.result_counts()
        response = self.client.post(test_path, data=self.clinician_answers())
        self.assertIn(response.status_code, (302, 403, 404))
        self.assertEqual(self.result_counts(), before)

    def test_registering_a_doctor_starts_with_an_empty_patient_list(self):
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.post(
            "/register",
            data={
                "email": "registered-doctor@example.test",
                "password": "test-password-123",
                "firstname": "Новый",
                "lastname": "Врач",
                "is_doctor": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard")
        _, dashboard = self.render("/dashboard")
        _, patients = self.render("/doctor/patients")
        self.assertEqual(dashboard["user"]["role"], "doctor")
        self.assertEqual(dashboard["patients"], [])
        self.assertEqual(patients["cards"], [])
        self.assertEqual(sum(dashboard["counts"].values()), 0)
        self.assertEqual(sum(patients["counts"].values()), 0)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM Patients").fetchone()[0], 2)
            links = connection.execute(
                "SELECT COUNT(*) FROM Doctor_Has_Patient WHERE doctor_id = ?",
                (dashboard["user"]["id"],),
            ).fetchone()[0]
        self.assertEqual(links, 0)


if __name__ == "__main__":
    unittest.main()
