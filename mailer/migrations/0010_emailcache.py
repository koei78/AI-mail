from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('mailer', '0009_friend_per_account'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmailCache',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uid', models.IntegerField(verbose_name='IMAP UID')),
                ('message_id', models.CharField(blank=True, db_index=True, max_length=512, verbose_name='Message-ID')),
                ('subject', models.CharField(blank=True, max_length=500, verbose_name='件名')),
                ('from_address', models.CharField(blank=True, max_length=255, verbose_name='送信者')),
                ('to_addresses', models.JSONField(default=list, verbose_name='宛先')),
                ('received_at', models.DateTimeField(blank=True, null=True, verbose_name='受信日時')),
                ('is_read', models.BooleanField(default=False, verbose_name='既読')),
                ('is_starred', models.BooleanField(default=False, verbose_name='スター')),
                ('has_attachments', models.BooleanField(default=False, verbose_name='添付あり')),
                ('size', models.IntegerField(default=0, verbose_name='サイズ')),
                ('cached_at', models.DateTimeField(auto_now=True, verbose_name='キャッシュ日時')),
                ('account', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='email_caches',
                    to='mailer.mailaccount',
                    verbose_name='メールアカウント',
                )),
                ('folder', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='email_caches',
                    to='mailer.mailfolder',
                    verbose_name='フォルダ',
                )),
            ],
            options={
                'verbose_name': 'メールキャッシュ',
                'verbose_name_plural': 'メールキャッシュ',
                'indexes': [
                    models.Index(fields=['folder', '-received_at'], name='mailer_emai_folder__recv_idx'),
                ],
                'unique_together': {('folder', 'uid')},
            },
        ),
    ]
