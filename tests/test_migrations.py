"""Migration order on a clean database (20 Nov 2026).

sources.0001_initial creates a column of type "vector". The pgvector
extension used to be enabled only in sources.0002, so `migrate` and the
tests failed on a new machine with: type "vector" does not exist. The
extension is now created at the start of 0001. A database that already
applied both migrations is not affected: applied migrations do not run again.
"""

from django.db import connection
from django.db.migrations import CreateModel, RunSQL
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase
from pgvector.django import VectorField


def _enables_vector(operation):
    return isinstance(operation, RunSQL) and "create extension" in str(operation.sql).lower() and "vector" in str(operation.sql).lower()


def _uses_vector(operation):
    fields = getattr(operation, "fields", None) or []
    if isinstance(operation, CreateModel):
        return any(isinstance(field, VectorField) for _, field in fields)
    return isinstance(getattr(operation, "field", None), VectorField)


class VectorExtensionOrderTests(SimpleTestCase):
    def test_extension_is_enabled_before_the_first_vector_column(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        plan = loader.graph.forwards_plan(max(key for key in loader.graph.nodes if key[0] == "sources"))
        enabled = False
        vector_columns = 0
        for key in plan:
            for operation in loader.graph.nodes[key].operations:
                if _enables_vector(operation):
                    enabled = True
                if _uses_vector(operation):
                    vector_columns += 1
                    self.assertTrue(enabled, f"{key[0]}.{key[1]} uses the vector type before the extension is enabled")
        self.assertTrue(enabled)
        self.assertGreaterEqual(vector_columns, 1)  # the check above really ran

    def test_applied_migration_names_are_unchanged(self):
        # Servers recorded these names in django_migrations: renaming or
        # squashing them would make the server re-run or lose migrations.
        loader = MigrationLoader(None, ignore_no_migrations=True)
        names = sorted(name for app, name in loader.disk_migrations if app == "sources")
        self.assertEqual(names[:3], ["0001_initial", "0002_enable_vector", "0003_sourcechunk_search_vector_and_more"])
        self.assertEqual(loader.disk_migrations[("sources", "0002_enable_vector")].dependencies, [("sources", "0001_initial")])


class VectorExtensionPresentTests(TestCase):
    def test_test_database_has_the_extension_and_the_column(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            self.assertIsNotNone(cursor.fetchone())
            cursor.execute("SELECT udt_name FROM information_schema.columns WHERE table_name = 'sources_embedding' AND column_name = 'vector'")
            self.assertEqual(cursor.fetchone()[0], "vector")
