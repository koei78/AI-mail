from django.contrib.auth import login
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django import forms
from django.shortcuts import redirect, render

from .models import User


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True, label='メールアドレス')

    class Meta:
        model = User
        fields = ('username', 'email', 'password1', 'password2')


def _render_auth(request, login_form=None, register_form=None):
    return render(request, 'registration/auth.html', {
        'login_form':    login_form    or AuthenticationForm(request),
        'register_form': register_form or RegisterForm(),
        'next': request.POST.get('next', request.GET.get('next', '/mail/')),
    })


def _redirect_if_authenticated(request):
    if request.user.is_authenticated:
        return redirect('/mail/')
    return None


def auth_view(request):
    """ログイン・新規登録を1ページに統合したビュー（GETのみ）"""
    redirect_response = _redirect_if_authenticated(request)
    if redirect_response:
        return redirect_response
    return _render_auth(request)


def login_view(request):
    """ログイン処理"""
    redirect_response = _redirect_if_authenticated(request)
    if redirect_response:
        return redirect_response

    next_url = request.POST.get('next', request.GET.get('next', '/mail/'))
    if request.method != 'POST':
        return redirect(f'/accounts/auth/?next={next_url}')

    form = AuthenticationForm(request, data=request.POST)
    if form.is_valid():
        login(request, form.get_user())
        return redirect(next_url)
    return _render_auth(request, login_form=form)


def register_view(request):
    """新規登録処理"""
    redirect_response = _redirect_if_authenticated(request)
    if redirect_response:
        return redirect_response

    if request.method != 'POST':
        return redirect('/accounts/auth/?tab=register')

    form = RegisterForm(request.POST)
    if form.is_valid():
        user = form.save()
        login(request, user)
        return redirect('/mail/')
    return _render_auth(request, register_form=form)
