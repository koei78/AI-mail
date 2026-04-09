from django.contrib import admin
from django.shortcuts import render
from django.urls import path, include
def home_view(request):
    """トップページ — ログイン状態に関わらず表示"""
    return render(request, 'home.html')


def privacy_view(request):
    return render(request, 'privacy.html')


def terms_view(request):
    return render(request, 'terms.html')


def demo_view(request):
    """デモページ — ログイン不要で体験できる"""
    return render(request, 'demo.html')


urlpatterns = [
    path('',          home_view,              name='home'),
    path('privacy/',  privacy_view,           name='privacy'),
    path('terms/',    terms_view,             name='terms'),
    path('demo/',     demo_view,              name='demo'),
    path('admin/',    admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('mail/',     include('mailer.urls')),
]
