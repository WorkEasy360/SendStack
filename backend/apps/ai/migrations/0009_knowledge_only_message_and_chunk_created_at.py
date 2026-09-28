from django.db import migrations, models


class Migration(migrations.Migration):
    """
    The two columns knowledge-base-only answering needs.

    `knowledge_only_message` is the exact sentence a strict agent refuses with;
    `KnowledgeChunk.created_at` is nullable rather than backfilled with "now",
    because stamping every pre-existing chunk with the moment of the migration
    would assert something false about when that text entered the knowledge
    base -- and this column exists precisely to answer "how old is the passage
    the customer was quoted?".
    """

    dependencies = [
        ("ai", "0008_aiagentconfig_allow_send_tools_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiagentconfig",
            name="knowledge_only_message",
            field=models.CharField(
                default="I couldn't find this information in the Knowledge Base.",
                help_text="Sent when knowledge-base-only answering is on and nothing relevant was found.",
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name="knowledgechunk",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
    ]
