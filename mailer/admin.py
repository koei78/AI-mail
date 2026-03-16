from django.contrib import admin

from .models import Email, MailAccount, MailFolder


@admin.register(MailAccount)
class MailAccountAdmin(admin.ModelAdmin):
    list_display = ['email_address', 'user', 'imap_host', 'is_active', 'last_synced_at']
    list_filter = ['is_active']
    search_fields = ['email_address', 'user__username']
    # ⚠️ パスワードフィールドは管理画面に表示しない
    exclude = ['password_encrypted']


@admin.register(MailFolder)
class MailFolderAdmin(admin.ModelAdmin):
    list_display = ['name', 'account', 'folder_type', 'unread_count']
    list_filter = ['folder_type']


@admin.register(Email)
class EmailAdmin(admin.ModelAdmin):
    list_display = ['subject', 'from_address', 'is_read', 'is_starred', 'received_at']
    list_filter = ['is_read', 'is_starred', 'has_attachments']
    search_fields = ['subject', 'from_address']
    date_hierarchy = 'received_at'
