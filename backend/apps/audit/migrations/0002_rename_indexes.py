from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [
        migrations.RenameIndex(
            model_name="auditevent",
            new_name="audit_audit_tenant__eab6aa_idx",
            old_name="audit_event_tenant_created_idx",
        ),
        migrations.RenameIndex(
            model_name="auditevent",
            new_name="audit_audit_actor_i_fb4253_idx",
            old_name="audit_event_actor_created_idx",
        ),
        migrations.RenameIndex(
            model_name="auditevent",
            new_name="audit_audit_action_9e2202_idx",
            old_name="audit_event_action_created_idx",
        ),
    ]
