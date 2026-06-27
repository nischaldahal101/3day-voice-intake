"""End-to-end test of storage.py against the live R2 bucket.

Creates a small temp file, runs upload -> exists -> download -> compare ->
delete -> verify-gone, and prints PASS/FAIL for each step. Exits non-zero
if any check fails so CI / shell scripts can tell.

PREREQUISITE: R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and
R2_BUCKET_NAME must be set in voice-intake/.env.
"""

import os
import tempfile
import uuid

from storage import (
    StorageError,
    audio_exists,
    delete_audio,
    download_audio,
    upload_audio,
)


PASS = "✅ PASS"
FAIL = "❌ FAIL"


def _check(name, ok, detail=""):
    tag = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {tag}  {name}{suffix}")
    return bool(ok)


def main():
    # 1. Create a small temp file with known bytes so we can verify round-trip.
    payload = b"3day-app storage round-trip " + uuid.uuid4().bytes + b" END"
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(payload)
        src_path = f.name
    print(f"created test file at {src_path} ({len(payload)} bytes)\n")

    key = None
    download_path = None
    all_ok = True

    try:
        # 2. Upload
        try:
            key = upload_audio(src_path)
            all_ok &= _check(
                f"upload_audio returned a key",
                ok=isinstance(key, str) and key.startswith("consultations/"),
                detail=f"key={key!r}",
            )
        except StorageError as exc:
            all_ok &= _check("upload_audio", ok=False, detail=str(exc))
            return _finalize(all_ok)

        # 3. audio_exists -> True
        try:
            exists = audio_exists(key)
            all_ok &= _check(
                "audio_exists -> True after upload",
                ok=exists is True,
                detail=f"got {exists!r}" if exists is not True else "",
            )
        except StorageError as exc:
            all_ok &= _check("audio_exists (post-upload)", ok=False, detail=str(exc))

        # 4. Download to a fresh local path
        download_path = tempfile.mktemp(suffix=".bin")
        try:
            returned = download_audio(key, download_path)
            ok = returned == download_path and os.path.exists(download_path)
            all_ok &= _check(
                f"download_audio -> {download_path}",
                ok=ok,
                detail="returned path != local_path or file missing" if not ok else "",
            )
        except StorageError as exc:
            all_ok &= _check("download_audio", ok=False, detail=str(exc))

        # 5. Compare bytes — the actual round-trip integrity check
        if download_path and os.path.exists(download_path):
            with open(download_path, "rb") as f:
                got = f.read()
            match = got == payload
            all_ok &= _check(
                f"downloaded bytes match original ({len(payload)} bytes)",
                ok=match,
                detail=f"len(downloaded)={len(got)}, first-mismatch?" if not match else "",
            )
        else:
            all_ok &= _check(
                "downloaded bytes match original",
                ok=False,
                detail="no download to compare",
            )

        # 6. Delete
        try:
            delete_audio(key)
            all_ok &= _check(f"delete_audio({key})", ok=True)
        except StorageError as exc:
            all_ok &= _check("delete_audio", ok=False, detail=str(exc))

        # 7. audio_exists -> False after delete
        try:
            exists_after = audio_exists(key)
            all_ok &= _check(
                "audio_exists -> False after delete",
                ok=exists_after is False,
                detail=f"got {exists_after!r}" if exists_after is not False else "",
            )
        except StorageError as exc:
            all_ok &= _check("audio_exists (post-delete)", ok=False, detail=str(exc))

    finally:
        # Tidy up local temp files (the R2 object is already gone if step 6 succeeded;
        # if it didn't, the leftover key is intentional so you can inspect it).
        for p in (src_path, download_path):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    _finalize(all_ok)


def _finalize(all_ok):
    print()
    if all_ok:
        print("✅ ALL CHECKS PASSED — R2 read/write is working end-to-end.")
    else:
        print("❌ ONE OR MORE CHECKS FAILED — see output above.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
