import ssl

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend

from mailer.imap_client import _ProxySMTP_SSL


class ProxySMTPEmailBackend(EmailBackend):
    def open(self):
        if self.connection:
            return False
        proxy_host = getattr(settings, 'SMTP_PROXY_HOST', '')
        if not proxy_host:
            return super().open()
        proxy_port = int(getattr(settings, 'SMTP_PROXY_PORT', 1080))
        proxy_user = getattr(settings, 'SMTP_PROXY_USER', None) or None
        proxy_pass = getattr(settings, 'SMTP_PROXY_PASS', None) or None
        try:
            ssl_context = ssl.create_default_context()
            self.connection = _ProxySMTP_SSL(
                self.host, self.port,
                proxy_host, proxy_port,
                proxy_user=proxy_user, proxy_pass=proxy_pass,
                context=ssl_context,
                timeout=self.timeout or 30,
            )
            if self.username and self.password:
                self.connection.login(self.username, self.password)
            return True
        except Exception:
            if not self.fail_silently:
                raise
            return False
