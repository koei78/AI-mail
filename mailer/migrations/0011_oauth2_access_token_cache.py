from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mailer', '0010_emailcache'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameIndex(
                    model_name='emailcache',
                    new_name='mailer_emai_folder__370098_idx',
                    old_name='mailer_emai_folder__recv_idx',
                ),
                migrations.AddField(
                    model_name='mailaccount',
                    name='oauth2_access_token',
                    field=models.TextField(blank=True, verbose_name='OAuth2アクセストークン'),
                ),
                migrations.AddField(
                    model_name='mailaccount',
                    name='oauth2_access_token_expires_at',
                    field=models.DateTimeField(blank=True, null=True, verbose_name='アクセストークン有効期限'),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
        AND indexname = 'mailer_emai_folder__recv_idx'
    ) THEN
        ALTER INDEX mailer_emai_folder__recv_idx RENAME TO mailer_emai_folder__370098_idx;
    END IF;
END $$;
ALTER TABLE mailer_mailaccount ADD COLUMN IF NOT EXISTS oauth2_access_token TEXT NOT NULL DEFAULT '';
ALTER TABLE mailer_mailaccount ADD COLUMN IF NOT EXISTS oauth2_access_token_expires_at TIMESTAMP WITH TIME ZONE;
                    """,
                    reverse_sql="""
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
        AND indexname = 'mailer_emai_folder__370098_idx'
    ) THEN
        ALTER INDEX mailer_emai_folder__370098_idx RENAME TO mailer_emai_folder__recv_idx;
    END IF;
END $$;
ALTER TABLE mailer_mailaccount DROP COLUMN IF EXISTS oauth2_access_token;
ALTER TABLE mailer_mailaccount DROP COLUMN IF EXISTS oauth2_access_token_expires_at;
                    """,
                ),
            ],
        ),
    ]
