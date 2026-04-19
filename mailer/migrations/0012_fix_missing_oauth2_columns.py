from django.db import migrations


class Migration(migrations.Migration):
    """
    0011 が django_migrations に「適用済み」として記録されているが
    実際のカラムが存在しないケースへの安全策。
    IF NOT EXISTS を使い冪等に実行できる。
    """

    dependencies = [
        ('mailer', '0011_oauth2_access_token_cache'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
ALTER TABLE mailer_mailaccount
  ADD COLUMN IF NOT EXISTS oauth2_access_token TEXT NOT NULL DEFAULT '';
ALTER TABLE mailer_mailaccount
  ADD COLUMN IF NOT EXISTS oauth2_access_token_expires_at TIMESTAMP WITH TIME ZONE;
            """,
            reverse_sql="""
ALTER TABLE mailer_mailaccount DROP COLUMN IF EXISTS oauth2_access_token;
ALTER TABLE mailer_mailaccount DROP COLUMN IF EXISTS oauth2_access_token_expires_at;
            """,
        ),
    ]
