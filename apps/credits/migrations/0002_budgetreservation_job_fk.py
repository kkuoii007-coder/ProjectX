from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("credits", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="budgetreservation",
            name="job",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="budget_reservations",
                to="ai.aijob",
            ),
        ),
    ]
