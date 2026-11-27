from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.errors import AppError
from app.ingestion.blobs import LocalBlobStore, assert_owned_key, new_upload_key

ME = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")
GOOD = "00000000-0000-4000-8000-000000000009"


def test_owned_key_accepts_own() -> None:
    key = new_upload_key(ME)
    assert key.startswith(f"uploads/{ME}/") and key.endswith(".pdf")
    assert_owned_key(key, ME)


@pytest.mark.parametrize(
    "key",
    [
        f"uploads/{OTHER}/{GOOD}.pdf",
        f"uploads/{ME}/../x.pdf",
        f"uploads/{ME}/../{OTHER}/{GOOD}.pdf",
        "/etc/passwd",
        f"uploads/{ME}/{GOOD}.txt",
        f"uploads/{ME}/.pdf",
        f"uploads/{ME}/{GOOD}.pdf\n",
        f"uploads/{ME}/{GOOD}.pdf/extra",
        "",
    ],
)
def test_owned_key_rejects(key: str) -> None:
    with pytest.raises(AppError) as exc:
        assert_owned_key(key, ME)
    assert exc.value.code == "upload_not_found"
    assert exc.value.status == 404


async def test_local_store_roundtrip_and_missing(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    key = new_upload_key(ME)
    await store.put(key, b"%PDF-1")
    assert await store.get(key) == b"%PDF-1"
    await store.put(key, b"%PDF-2")
    assert await store.get(key) == b"%PDF-2"
    assert [p.name for p in (tmp_path / "uploads" / str(ME)).iterdir()] == [key.rsplit("/", 1)[1]]
    await store.delete(key)
    await store.delete(key)  # idempotent
    with pytest.raises(AppError) as exc:
        await store.get(key)
    assert exc.value.code == "upload_not_found"
    with pytest.raises(AppError):
        await store.get(new_upload_key(uuid4()))


@pytest.mark.parametrize("key", ["../escape.pdf", f"uploads/{ME}/../../../escape.pdf", "/abs.pdf"])
async def test_local_store_refuses_escape(tmp_path: Path, key: str) -> None:
    store = LocalBlobStore(tmp_path / "root")
    for call in (store.put(key, b"x"), store.get(key), store.delete(key)):
        with pytest.raises(AppError) as exc:
            await call
        assert exc.value.code == "upload_not_found"
    assert not (tmp_path / "escape.pdf").exists()
