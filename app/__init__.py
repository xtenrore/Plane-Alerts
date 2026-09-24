"""Aircraft Alert Telegram Bot — Core application package."""

# Plane? v3.8 applies one outbound rendering layer to every Telegram message,
# caption and inline keyboard. The retired external agent worker intentionally uses a minimal
# dependency set and imports the same `app` package, so skip this Telegram-only
# bootstrap only when the optional telegram package itself is absent.
try:
    from app.ui_symbols import install_telegram_monochrome
except ModuleNotFoundError as exc:
    if exc.name != "telegram":
        raise
else:
    install_telegram_monochrome()
