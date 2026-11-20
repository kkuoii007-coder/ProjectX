# Enables the pgvector extension required by sources.Embedding.
#
# Since 20 Nov 2026 the extension is created at the start of 0001_initial,
# before the first "vector" column. This migration is kept unchanged for
# databases that already recorded it; on a clean database it is a no-op.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("sources", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql="CREATE EXTENSION IF NOT EXISTS vector", reverse_sql=migrations.RunSQL.noop),
    ]
