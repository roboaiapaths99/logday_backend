"""Real HTTP routes with an in-memory MongoDB adapter; never connect to production."""
import asyncio
import os
from pathlib import Path
import re
import sys
import types

import mongomock
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update(APP_ENV="testing", JWT_SECRET="security-tests-only-not-for-deployment-123456789",
                  ACCESS_TOKEN_EXPIRE_MINUTES="60", ALLOWED_ORIGINS="https://admin.example.test")
mongo = mongomock.MongoClient(tz_aware=True)
memory_db = mongo["security_tests"]


class AsyncCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def sort(self, *args, **kwargs):
        self.cursor = self.cursor.sort(*args, **kwargs)
        return self

    def limit(self, value):
        self.cursor = self.cursor.limit(value)
        return self

    def skip(self, value):
        self.cursor = self.cursor.skip(value)
        return self

    async def to_list(self, length=None):
        items = list(self.cursor)
        return items if length is None else items[:length]

    def __aiter__(self):
        self.iterator = iter(self.cursor)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration


class AsyncCollection:
    def __init__(self, collection):
        self.collection = collection

    def find(self, *args, **kwargs):
        return AsyncCursor(self.collection.find(*args, **kwargs))

    def aggregate(self, *args, **kwargs):
        return AsyncCursor(self.collection.aggregate(*args, **kwargs))

    def __getattr__(self, name):
        async def operation(*args, **kwargs):
            return getattr(self.collection, name)(*args, **kwargs)
        return operation


database = types.ModuleType("database")
for name in re.findall(r"^(\w+_collection) =", (ROOT / "database.py").read_text(encoding="utf-8"), re.M):
    setattr(database, name, AsyncCollection(memory_db[name]))


async def ensure_indexes():
    memory_db.employees_collection.create_index("email", unique=True)


database.ensure_employee_indexes = ensure_indexes
database.db = memory_db
database.client = mongo
sys.modules["database"] = database

# Biometric inference and outbound Sheets integration are not exercised by auth tests.
face = types.ModuleType("face_utils")
face.get_face_embedding = lambda image: [0.1, 0.2, 0.3]
face.verify_face = lambda *args, **kwargs: (True, 0.01)
face.compare_faces = lambda *args, **kwargs: True
face.is_dummy_embedding = lambda value: value is None
sys.modules["face_utils"] = face
sheets = types.ModuleType("sheets_sync")


async def no_external_sync(*args, **kwargs):
    return None


sheets.sync_to_google_sheets = no_external_sync
sheets.sync_visit_to_google_sheets = no_external_sync
sys.modules["sheets_sync"] = sheets


@pytest.fixture(autouse=True)
def reset_data():
    for name in memory_db.list_collection_names():
        memory_db[name].delete_many({})
    asyncio.run(ensure_indexes())
    import auth
    auth._FAILED_LOGINS.clear()


@pytest.fixture
def db():
    return memory_db


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app
    # No lifespan: scheduled jobs and production connections must never start in unit tests.
    return TestClient(app, raise_server_exceptions=True)
