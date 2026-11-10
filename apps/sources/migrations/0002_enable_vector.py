# Enables the pgvector extension required by sources.Embedding.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("sources", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql="CREATE EXTENSION IF NOT EXISTS vector", reverse_sql=migrations.RunSQL.noop),
    ]
