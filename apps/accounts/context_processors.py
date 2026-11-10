def account_summary(request):
    if not request.user.is_authenticated:
        return {}
    from apps.credits.models import Wallet

    wallet = Wallet.objects.filter(user=request.user).first()
    return {"wallet": wallet}
