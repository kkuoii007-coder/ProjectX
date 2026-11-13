import re

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError


class RegisterForm(forms.Form):
    login = forms.CharField(label="Логин", min_length=4, max_length=32)
    password = forms.CharField(label="Пароль", min_length=12, max_length=128, widget=forms.PasswordInput, strip=False)
    password_confirm = forms.CharField(label="Повторите пароль", max_length=128, widget=forms.PasswordInput, strip=False)
    no_recovery_ack = forms.BooleanField(label="Я понимаю, что восстановления доступа нет")
    invite_code = forms.CharField(label="Код приглашения", required=False, max_length=64)

    def __init__(self, *args, invite_required=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.invite_required = invite_required
        self.fields["login"].widget.attrs.update({"autocomplete": "username", "autocapitalize": "none"})
        self.fields["password"].widget.attrs["autocomplete"] = "new-password"
        self.fields["password_confirm"].widget.attrs["autocomplete"] = "new-password"

    def clean_invite_code(self):
        code = (self.cleaned_data.get("invite_code") or "").strip()
        from django.conf import settings

        if settings.REGISTRATION_MODE == "invite":
            if not code:
                raise ValidationError("Введите код приглашения")
        return code

    def clean_login(self):
        login = self.cleaned_data["login"].lower()
        if not re.fullmatch(r"[a-z0-9_]{4,32}", login):
            raise ValidationError("Только строчные латинские буквы, цифры и подчёркивание")
        if get_user_model().objects.filter(login_normalized=login).exists():
            raise ValidationError("Этот логин уже занят")
        return login

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") != cleaned.get("password_confirm"):
            self.add_error("password_confirm", "Пароли не совпадают")
        elif cleaned.get("password"):
            password_validation.validate_password(cleaned["password"])
        return cleaned


class WordForm(forms.Form):
    term = forms.CharField(label="Слово или термин", max_length=100)
    translation_ru = forms.CharField(label="Перевод", max_length=200)
    context_en = forms.CharField(label="Контекст", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    publication_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
