"""Bot conversation sub-package."""

# v5.4 installs a higher-priority UX layer around the proven profile engine.
# Importing it here means existing runtime registration code remains unchanged.
from app.bot import profile_experience_v54 as _profile_experience_v54  # noqa: F401,E402
