from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model


class LoginBackend(ModelBackend):
    def authenticate(self, request, login=None, password=None, username=None, **kwargs):
        login = (login or username or "").lower()
        if not login or password is None:
            return None
        try:
            user = get_user_model().objects.get(login_normalized=login)
        except get_user_model().DoesNotExist:
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
