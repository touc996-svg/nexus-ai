from app.core.secret_files import load_secret_files

load_secret_files()  # NAME_FILE=/run/secrets/... -> NAME, before any module reads its settings
