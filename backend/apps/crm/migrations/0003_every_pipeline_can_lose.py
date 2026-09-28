"""
Give a losing stage to any pipeline that has none.

0002 trimmed the seeded ladder and dropped "Lost" wherever it was empty, which
left those boards with no way to close a sale that fell through: the deal sits
in an open stage forever, and the forecast counts money nobody is going to pay.
A stage is cheap and a wrong forecast is not, so every pipeline gets one back.

Kind, not name -- a tenant whose losing stage is called "Went elsewhere"
already has one and is left alone.
"""

from django.db import migrations


def give_every_pipeline_a_losing_stage(apps, schema_editor):
    Pipeline = apps.get_model("crm", "Pipeline")
    PipelineStage = apps.get_model("crm", "PipelineStage")

    for pipeline in Pipeline.objects.prefetch_related("stages"):
        stages = list(pipeline.stages.all())
        if any(stage.kind == "LOST" for stage in stages):
            continue
        # (pipeline, name) is unique, so a stage already called "Lost" -- an
        # open one, since this pipeline has no losing stage -- takes the name.
        name = "Lost" if not any(stage.name == "Lost" for stage in stages) else "Lost sale"
        PipelineStage.objects.create(
            pipeline=pipeline, name=name, kind="LOST", probability=0, color="#dc2626",
            order=max((stage.order for stage in stages), default=-1) + 1,
        )


class Migration(migrations.Migration):

    dependencies = [("crm", "0002_four_default_stages")]

    operations = [migrations.RunPython(give_every_pipeline_a_losing_stage, migrations.RunPython.noop)]
