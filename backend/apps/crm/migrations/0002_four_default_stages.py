"""
Fold pipelines that still carry the old eight seeded stages down to the four
the board ships with now: New, Contacted, Qualified, Won.

Two rules keep this from destroying anyone's work:

* Only an *untouched* pipeline is folded -- one whose stage names are still
  exactly the old defaults. A tenant who renamed, added or removed anything
  has an opinion about their board, and it is not this migration's to override.
* No deal is silently reclassified. Deals in a disappearing stage fold
  backwards to the nearest surviving one (never forwards, which would claim
  progress that never happened), and "Lost" is kept wherever it still holds
  deals, because folding a lost deal into an open stage would resurrect it.
"""

from django.db import migrations

OLD_DEFAULTS = {"New", "Contacted", "Interested", "Qualified", "Proposal", "Negotiation", "Won", "Lost"}

#: Where the deals of a removed stage go. Backwards, never forwards.
FOLD_INTO = {"Interested": "Contacted", "Proposal": "Qualified", "Negotiation": "Qualified"}

#: The stages that survive, in board order, with their new probabilities.
KEEP = (("New", 0, 10), ("Contacted", 1, 30), ("Qualified", 2, 60), ("Won", 3, 100))


def fold_to_four(apps, schema_editor):
    Pipeline = apps.get_model("crm", "Pipeline")
    Deal = apps.get_model("crm", "Deal")

    for pipeline in Pipeline.objects.prefetch_related("stages"):
        stages = {stage.name: stage for stage in pipeline.stages.all()}
        if set(stages) != OLD_DEFAULTS:
            continue

        for name, target in FOLD_INTO.items():
            # OPEN -> OPEN, so `status` and `closed_at` stay correct without
            # going through Deal.save() (which the historical model lacks).
            Deal.objects.filter(stage=stages[name]).update(stage=stages[target])
            stages[name].delete()

        lost = stages["Lost"]
        if Deal.objects.filter(stage=lost).exists():
            lost.order = len(KEEP)
            lost.save(update_fields=["order"])
        else:
            lost.delete()

        for name, order, probability in KEEP:
            stage = stages[name]
            stage.order = order
            stage.probability = probability
            stage.save(update_fields=["order", "probability"])


class Migration(migrations.Migration):

    dependencies = [("crm", "0001_initial")]

    operations = [migrations.RunPython(fold_to_four, migrations.RunPython.noop)]
