"""
mailer/models.py
メールアカウント・フォルダ・ラベルのモデル定義

メール本文はIMAPサーバーに保存されているため、DBには保存しない。
DBに保存するのは:
  - MailAccount  : 接続情報（パスワードはFernetで暗号化）
  - MailFolder   : フォルダ一覧（表示用。アカウント追加時に同期）
  - Label        : ユーザー定義ラベル
  - EmailLabel   : メール(message_id)とラベルの紐付けのみ
"""
from django.conf import settings
from django.db import models

# ⚠️ セキュリティ: パスワードはFernetで暗号化してDBに保存する
from cryptography.fernet import Fernet


def _get_fernet():
    """設定からFernetインスタンスを生成する"""
    key = settings.MAIL_ENCRYPTION_KEY
    if not key:
        raise ValueError(
            "MAIL_ENCRYPTION_KEY が設定されていません。"
            ".envに追記してください。"
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


class MailAccount(models.Model):
    """独自ドメインのメールアカウント情報"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='mail_accounts',
        verbose_name='オーナー',
    )
    email_address = models.CharField(max_length=255, verbose_name='メールアドレス')
    display_name = models.CharField(max_length=100, blank=True, verbose_name='表示名')

    # IMAP設定
    imap_host = models.CharField(max_length=255, verbose_name='IMAPホスト')
    imap_port = models.IntegerField(default=993, verbose_name='IMAPポート')

    # SMTP設定
    smtp_host = models.CharField(max_length=255, verbose_name='SMTPホスト')
    smtp_port = models.IntegerField(default=465, verbose_name='SMTPポート')

    # 認証情報
    username = models.CharField(
        max_length=255,
        verbose_name='ログインID',
        help_text='通常はメールアドレスと同じ。異なる場合のみ変更。',
    )
    # ⚠️ パスワードはFernetで暗号化して保存。平文では絶対に保存しないこと
    password_encrypted = models.TextField(verbose_name='パスワード（暗号化済み）')

    use_ssl = models.BooleanField(default=True, verbose_name='SSL使用')
    # ⚠️ さくら/Xserverなど共有ホスティングはSSL証明書がサーバー名で発行されるため
    # カスタムドメインで接続するとホスト名不一致になる。その場合Falseに設定する。
    ssl_verify = models.BooleanField(default=True, verbose_name='SSL証明書検証')
    last_synced_at = models.DateTimeField(null=True, blank=True, verbose_name='最終同期日時')
    is_active = models.BooleanField(default=True, verbose_name='有効')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日時')

    class Meta:
        verbose_name = 'メールアカウント'
        verbose_name_plural = 'メールアカウント'

    def __str__(self):
        return f'{self.email_address} ({self.user})'

    def set_password(self, raw_password: str):
        """平文パスワードをFernetで暗号化してpassword_encryptedに保存"""
        f = _get_fernet()
        self.password_encrypted = f.encrypt(raw_password.encode()).decode()

    def get_password(self) -> str:
        """暗号化されたパスワードを復号して返す"""
        f = _get_fernet()
        return f.decrypt(self.password_encrypted.encode()).decode()


class MailFolder(models.Model):
    """メールフォルダ（受信トレイ・送信済み・ゴミ箱など）"""

    FOLDER_TYPE_CHOICES = [
        ('inbox', '受信トレイ'),
        ('sent', '送信済み'),
        ('draft', '下書き'),
        ('trash', 'ゴミ箱'),
        ('spam', 'スパム'),
        ('custom', 'カスタム'),
    ]

    account = models.ForeignKey(
        MailAccount,
        on_delete=models.CASCADE,
        related_name='folders',
        verbose_name='メールアカウント',
    )
    name = models.CharField(max_length=100, verbose_name='フォルダ名')
    folder_type = models.CharField(
        max_length=20,
        choices=FOLDER_TYPE_CHOICES,
        default='custom',
        verbose_name='フォルダ種別',
    )
    # IMAPサーバー上の実際のフォルダ名（例: INBOX, Sent, Trash）
    remote_name = models.CharField(max_length=255, verbose_name='IMAPフォルダ名')
    unread_count = models.IntegerField(default=0, verbose_name='未読数')

    class Meta:
        verbose_name = 'メールフォルダ'
        verbose_name_plural = 'メールフォルダ'

    def __str__(self):
        return f'{self.account.email_address} / {self.name}'


class Label(models.Model):
    """メールラベル（タグ）"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='labels',
        verbose_name='オーナー',
    )
    name = models.CharField(max_length=50, verbose_name='ラベル名')
    color = models.CharField(max_length=7, default='#0078d4', verbose_name='カラー')

    class Meta:
        verbose_name = 'ラベル'
        verbose_name_plural = 'ラベル'
        unique_together = [['user', 'name']]

    def __str__(self):
        return self.name


class EmailLabel(models.Model):
    """メール(message_id)とラベルの紐付け（IMAPにはラベル概念がないためDBで管理）"""

    account = models.ForeignKey(
        MailAccount,
        on_delete=models.CASCADE,
        related_name='email_labels',
        verbose_name='メールアカウント',
    )
    # RFC 2822 Message-ID をキーとして使用（IMAP UID はフォルダ移動で変わるため）
    message_id = models.CharField(max_length=512, verbose_name='Message-ID')
    label = models.ForeignKey(
        Label,
        on_delete=models.CASCADE,
        related_name='email_labels',
        verbose_name='ラベル',
    )

    class Meta:
        verbose_name = 'メールラベル紐付け'
        verbose_name_plural = 'メールラベル紐付け'
        unique_together = [['account', 'message_id', 'label']]
