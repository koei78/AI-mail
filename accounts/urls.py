from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = 'accounts'

urlpatterns = [
    path('auth/',     views.auth_view,      name='auth'),
    path('login/',    views.login_view,     name='login'),
    path('logout/',   auth_views.LogoutView.as_view(), name='logout'),
    path('register/', views.register_view,  name='register'),
]
