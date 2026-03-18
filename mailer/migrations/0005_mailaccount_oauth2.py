from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mailer', '0004_emaillabel_delete_email'),
    ]

    operations = [
        migrations.AddField(
            model_name='mailaccount',
            name='auth_type',
            field=models.CharField(default='password', max_length=20, verbose_name='認証方式'),
        ),
        migrations.AddField(
            model_name='mailaccount',
            name='oauth2_refresh_token_encrypted',
            field=models.TextField(blank=True, verbose_name='OAuth2リフレッシュトークン（暗号化済み）'),
        ),
    ]
