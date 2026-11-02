import pytest

from app.core import db
from app.core.auth import hash_password
from app.core.config import Settings

PASSWORD = "correct horse"


@pytest.fixture(scope="session")
def password_hash():
    return hash_password(PASSWORD)


@pytest.fixture
def settings(tmp_path, password_hash):
    return Settings(
        _env_file=None, data_dir=tmp_path, secret_key="x" * 32, password_hash=password_hash
    )


@pytest.fixture
def conn(settings):
    """A migrated core DB."""
    c = db.connect(settings.db_path)
    db.migrate(c, {"core": db.MIGRATIONS_DIR}, settings.backup_dir)
    yield c
    c.close()
