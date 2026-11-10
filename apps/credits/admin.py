from django.contrib import admin

from .models import BudgetBucket, BudgetReservation, CreditGrant, CreditReservation, CreditTransaction, Wallet


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("user", "total_credits", "reserved_credits", "available_credits")


@admin.register(CreditTransaction)
class CreditTransactionAdmin(admin.ModelAdmin):
    list_display = ("wallet", "kind", "signed_amount", "operation_key", "created_at")
    readonly_fields = [f.name for f in CreditTransaction._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(CreditGrant)
admin.site.register(CreditReservation)
admin.site.register(BudgetBucket)
admin.site.register(BudgetReservation)
