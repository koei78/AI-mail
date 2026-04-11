"""
既存の Friend レコードをすべて削除し、
user FK を account FK に置き換える。
友達をアカウント単位で管理するための変更。
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('mailer', '0008_friend'),
    ]

    operations = [
        # 既存データはアカウントと紐づけられないため削除
        migrations.RunSQL('DELETE FROM mailer_friend;', migrations.RunSQL.noop),

        # unique_together を一旦クリア
        migrations.AlterUniqueTogether(
            name='friend',
            unique_together=set(),
        ),

        # user FK を削除
        migrations.RemoveField(
            model_name='friend',
            name='user',
        ),

        # account FK を追加
        migrations.AddField(
            model_name='friend',
            name='account',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='friends',
                to='mailer.mailaccount',
                verbose_name='メールアカウント',
            ),
        ),

        # unique_together を account + email_address に設定
        migrations.AlterUniqueTogether(
            name='friend',
            unique_together={('account', 'email_address')},
        ),
    ]
