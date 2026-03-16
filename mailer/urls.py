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

    # =============================
    # フォルダAPI
    # =============================
    path('api/folders/', views.api_folders, name='api_folders'),

    # =============================
    # メールAPI
    # =============================
    path('api/emails/', views.api_emails, name='api_emails'),
    path('api/emails/<int:email_id>/', views.api_email_detail, name='api_email_detail'),
    path('api/emails/<int:email_id>/read/', views.api_email_read, name='api_email_read'),
    path('api/emails/<int:email_id>/star/', views.api_email_star, name='api_email_star'),
    path('api/emails/<int:email_id>/move/', views.api_email_move, name='api_email_move'),

    # =============================
    # ラベルAPI
    # =============================
    path('api/labels/', views.api_labels, name='api_labels'),
    path('api/labels/<int:label_id>/', views.api_label_detail, name='api_label_detail'),
    path('api/emails/<int:email_id>/labels/<int:label_id>/', views.api_email_label, name='api_email_label'),

    # =============================
    # 送信・返信・転送API
    # =============================
    path('api/send/', views.api_send, name='api_send'),
    path('api/reply/<int:email_id>/', views.api_reply, name='api_reply'),
    path('api/forward/<int:email_id>/', views.api_forward, name='api_forward'),
    path('api/emails/<int:email_id>/ai-reply/', views.api_ai_reply, name='api_ai_reply'),
]
