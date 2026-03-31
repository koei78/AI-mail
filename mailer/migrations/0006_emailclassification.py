from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('mailer', '0005_mailaccount_oauth2'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmailClassification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uid', models.IntegerField(verbose_name='IMAP UID')),
                ('message_id', models.CharField(blank=True, max_length=512, verbose_name='Message-ID')),
                ('subject', models.CharField(blank=True, max_length=500, verbose_name='件名')),
                ('sender', models.CharField(blank=True, max_length=255, verbose_name='送信者')),
                ('summary', models.TextField(blank=True, verbose_name='AI要約')),
                ('category', models.CharField(
                    choices=[('A', 'A: 最優先'), ('B', 'B: 重要'), ('C', 'C: 低優先')],
                    max_length=1,
                    verbose_name='分類カテゴリ',
                )),
                ('classified_at', models.DateTimeField(auto_now_add=True, verbose_name='分類日時')),
                ('account', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='classifications',
                    to='mailer.mailaccount',
                    verbose_name='メールアカウント',
                )),
                ('folder', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='classifications',
                    to='mailer.mailfolder',
                    verbose_name='フォルダ',
                )),
            ],
            options={
                'verbose_name': 'メール分類',
                'verbose_name_plural': 'メール分類',
                'ordering': ['category', '-classified_at'],
                'unique_together': {('account', 'folder', 'uid')},
            },
        ),
    ]
