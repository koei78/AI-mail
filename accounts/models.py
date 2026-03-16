from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """カスタムUserモデル（将来の拡張に対応）"""
    email = models.EmailField(unique=True)

    class Meta:
        verbose_name = 'ユーザー'
        verbose_name_plural = 'ユーザー'

    def __str__(self):
        return self.email or self.username
