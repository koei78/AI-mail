from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('mailer', '0007_classifyschedule'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Friend',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email_address', models.EmailField(verbose_name='友達のメールアドレス')),
                ('display_name', models.CharField(blank=True, max_length=200, verbose_name='表示名')),
                ('last_email_at', models.DateTimeField(blank=True, null=True, verbose_name='最終メール日時')),
                ('last_email_subject', models.CharField(blank=True, max_length=300, verbose_name='最終メール件名')),
                ('added_at', models.DateTimeField(auto_now_add=True, verbose_name='追加日時')),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='friends',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='オーナー',
                )),
            ],
            options={
                'verbose_name': '友達',
                'verbose_name_plural': '友達',
                'unique_together': {('user', 'email_address')},
            },
        ),
    ]
