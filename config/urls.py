from django.contrib import admin
from django.shortcuts import render
from django.urls import path, include
from django.http import HttpResponse


def home_view(request):
    return render(request, 'home.html')


def privacy_view(request):
    return render(request, 'privacy.html')


def terms_view(request):
    return render(request, 'terms.html')


def demo_view(request):
    return render(request, 'demo.html')


def sitemap_view(request):
    return render(request, 'sitemap.xml', content_type='application/xml')


def robots_view(request):
    return render(request, 'robots.txt', content_type='text/plain')


urlpatterns = [
    path('',            home_view,    name='home'),
    path('privacy/',    privacy_view, name='privacy'),
    path('terms/',      terms_view,   name='terms'),
    path('demo/',       demo_view,    name='demo'),
    path('sitemap.xml', sitemap_view, name='sitemap'),
    path('robots.txt',  robots_view,  name='robots'),
    path('admin/',      admin.site.urls),
    path('accounts/',   include('accounts.urls')),
    path('mail/',       include('mailer.urls')),
]
