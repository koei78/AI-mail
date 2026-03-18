"""
mailer/urls.py
メールクライアントのURLルーティング
"""
from django.urls import path

from . import views

app_name = 'mailer'

urlpatterns = [
    # =============================
    # ページView
    # =============================
    path('', views.MailIndexView.as_view(), name='index'),
    path('setup/', views.MailSetupView.as_view(), name='setup'),
    path('setup/register/', views.setup_register, name='setup_register'),

    # =============================
    # 接続テスト
    # =============================
    path('api/test-connection/', views.api_test_connection, name='api_test_connection'),

    # =============================
    # アカウントAPI
    # =============================
    path('api/accounts/', views.api_accounts, name='api_accounts'),
    path('api/accounts/<int:account_id>/', views.api_account_detail, name='api_account_detail'),
    path('api/accounts/<int:account_id>/sync/', views.api_account_sync, name='api_account_sync'),
    path('api/folders/<int:folder_id>/sync/', views.api_folder_sync, name='api_folder_sync'),

    # =============================
    # フォルダAPI
    # =============================
    path('api/folders/', views.api_folders, name='api_folders'),

    # =============================
    # メールAPI（uid = IMAP UID、folder_id はクエリパラメータで渡す）
    # =============================
    path('api/emails/', views.api_emails, name='api_emails'),
    path('api/emails/<int:uid>/', views.api_email_detail, name='api_email_detail'),
    path('api/emails/<int:uid>/read/', views.api_email_read, name='api_email_read'),
    path('api/emails/<int:uid>/unread/', views.api_email_unread, name='api_email_unread'),
    path('api/emails/<int:uid>/star/', views.api_email_star, name='api_email_star'),
    path('api/emails/<int:uid>/move/', views.api_email_move, name='api_email_move'),

    # =============================
    # フォルダ操作API
    # =============================
    path('api/folders/<int:folder_id>/empty/', views.api_folder_empty, name='api_folder_empty'),

    # =============================
    # ラベルAPI
    # =============================
    path('api/labels/', views.api_labels, name='api_labels'),
    path('api/labels/<int:label_id>/', views.api_label_detail, name='api_label_detail'),
    path('api/emails/<int:uid>/labels/<int:label_id>/', views.api_email_label, name='api_email_label'),

    # =============================
    # 添付ファイルAPI・検索API
    # =============================
    path('api/emails/<int:uid>/attachments/<int:index>/', views.api_attachment, name='api_attachment'),
    path('api/search/', views.api_search, name='api_search'),

    # =============================
    # 送信・返信・転送API
    # =============================
    path('api/send/', views.api_send, name='api_send'),
    path('api/reply/<int:uid>/', views.api_reply, name='api_reply'),
    path('api/forward/<int:uid>/', views.api_forward, name='api_forward'),
    path('api/emails/<int:uid>/ai-reply/', views.api_ai_reply, name='api_ai_reply'),
    path('api/emails/<int:uid>/ai-chat/', views.api_ai_chat, name='api_ai_chat'),

    # =============================
    # Gmail OAuth2
    # =============================
    path('oauth/gmail/start/', views.gmail_oauth_start, name='gmail_oauth_start'),
    path('oauth/gmail/callback/', views.gmail_oauth_callback, name='gmail_oauth_callback'),
]
