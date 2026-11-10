from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import InviteCode, User


@admin.register(User)
class ReaderUserAdmin(UserAdmin):
    ordering = ("login",)
    list_display = ("login", "public_alias", "is_active", "is_staff", "created_at")
    search_fields = ("login",)
    readonly_fields = ("login_normalized", "public_alias", "last_login", "created_at")
    fieldsets = (
        (None, {"fields": ("login", "password")}),
        ("Права", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Служебное", {"fields": ("login_normalized", "public_alias", "last_login", "created_at")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("login", "password1", "password2", "is_staff", "is_active")}),)


admin.site.register(InviteCode)
