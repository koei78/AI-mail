from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('mailer', '0002_mailaccount_ssl_verify'),
    ]

    operations = [
        migrations.CreateModel(
            name='Label',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=50, verbose_name='ラベル名')),
                ('color', models.CharField(default='#0078d4', max_length=7, verbose_name='カラー')),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='labels',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='オーナー',
                )),
            ],
            options={
                'verbose_name': 'ラベル',
                'verbose_name_plural': 'ラベル',
                'unique_together': {('user', 'name')},
            },
        ),
        migrations.AddField(
            model_name='email',
            name='labels',
            field=models.ManyToManyField(
                blank=True,
                related_name='emails',
                to='mailer.label',
                verbose_name='ラベル',
            ),
        ),
    ]
