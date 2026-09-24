"""Local-only feasibility evidence, not a claim of an immutable adapter."""

import os
import sqlite3

import pytest


def seeded(path):
    from qdrant_client import QdrantClient, models
    client = QdrantClient(path=str(path))
    client.create_collection('spike', vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
    client.upsert('spike', points=[models.PointStruct(id=1, vector=[1., 0.], payload={'version': 'original'})])
    client.close()


@pytest.mark.skipif(os.geteuid() == 0, reason='root bypasses file permission evidence')
def test_local_loading_needs_writable_lock_even_when_data_already_exists(tmp_path):
    from qdrant_client import QdrantClient
    location = tmp_path / 'index'
    seeded(location)
    lock = location / '.lock'
    original_mode = lock.stat().st_mode & 0o777
    lock.chmod(0o400)
    try:
        with pytest.raises(PermissionError):
            QdrantClient(path=str(location))
    finally:
        lock.chmod(original_mode)


def test_second_persistent_local_handle_is_rejected(tmp_path):
    from qdrant_client import QdrantClient
    location = tmp_path / 'index'
    seeded(location)
    first = QdrantClient(path=str(location))
    try:
        with pytest.raises(RuntimeError, match='already accessed'):
            QdrantClient(path=str(location))
        assert first.retrieve('spike', ids=[1])[0].payload['version'] == 'original'
    finally:
        first.close()


def test_failed_persistence_does_not_make_local_memory_immutable(tmp_path, monkeypatch):
    from qdrant_client import QdrantClient, models
    location = tmp_path / 'index'
    seeded(location)
    client = QdrantClient(path=str(location))
    try:
        storage = client._client.collections['spike'].storage
        def denied(*args, **kwargs):
            raise sqlite3.OperationalError('synthetic readonly storage')
        monkeypatch.setattr(storage, 'persist', denied)
        with pytest.raises(sqlite3.OperationalError):
            client.upsert('spike', points=[models.PointStruct(id=1, vector=[0., 1.], payload={'version': 'mutated'})])
        assert client.retrieve('spike', ids=[1])[0].payload['version'] == 'mutated'
    finally:
        client.close()
    reopened = QdrantClient(path=str(location))
    try:
        assert reopened.retrieve('spike', ids=[1])[0].payload['version'] == 'original'
    finally:
        reopened.close()
