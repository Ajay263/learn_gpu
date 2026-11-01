"""Milestone 1: infrastructure behaves as designed (buckets, least privilege, SFTP → MinIO)."""

import io
import uuid

import paramiko
import psycopg
import pytest
from botocore.exceptions import ClientError

pytestmark = pytest.mark.integration

BUCKETS = {"landing", "bronze", "derived", "manifests", "evidence"}


def _denied(fn) -> bool:
    try:
        fn()
    except ClientError as e:
        return e.response["Error"]["Code"] in {"AccessDenied", "403"}
    return False


def test_buckets_exist_and_bronze_is_versioned(env, s3_factory):
    root = s3_factory(env["MINIO_ROOT_USER"], env["MINIO_ROOT_PASSWORD"])
    names = {b["Name"] for b in root.list_buckets()["Buckets"]}
    assert BUCKETS <= names
    assert root.get_bucket_versioning(Bucket="bronze")["Status"] == "Enabled"


def test_sftpgo_identity_is_confined_to_landing_uploads(env, s3_factory):
    s3 = s3_factory(env["MINIO_SFTPGO_ACCESS_KEY"], env["MINIO_SFTPGO_SECRET_KEY"])
    assert _denied(lambda: s3.list_objects_v2(Bucket="bronze"))
    assert _denied(lambda: s3.put_object(Bucket="landing", Key="rejected/x", Body=b"x"))


def test_nifi_identity_cannot_delete_originals(env, s3_factory):
    nifi = s3_factory(env["MINIO_NIFI_ACCESS_KEY"], env["MINIO_NIFI_SECRET_KEY"])
    key = f"test/{uuid.uuid4()}.bin"
    nifi.put_object(Bucket="bronze", Key=key, Body=b"original")
    assert _denied(lambda: nifi.delete_object(Bucket="bronze", Key=key))
    assert _denied(lambda: nifi.put_object(Bucket="derived", Key=key, Body=b"x"))
    root = s3_factory(env["MINIO_ROOT_USER"], env["MINIO_ROOT_PASSWORD"])
    for v in root.list_object_versions(Bucket="bronze", Prefix=key).get("Versions", []):
        root.delete_object(Bucket="bronze", Key=key, VersionId=v["VersionId"])


def test_app_identity_reads_bronze_but_cannot_write_it(env, s3_factory):
    app = s3_factory(env["MINIO_APP_ACCESS_KEY"], env["MINIO_APP_SECRET_KEY"])
    app.list_objects_v2(Bucket="bronze", MaxKeys=1)
    assert _denied(lambda: app.put_object(Bucket="bronze", Key="x", Body=b"x"))
    assert _denied(lambda: app.list_objects_v2(Bucket="landing"))
    key = f"test/{uuid.uuid4()}.txt"
    app.put_object(Bucket="derived", Key=key, Body=b"ok")
    app.delete_object(Bucket="derived", Key=key)


def _sftp(env) -> paramiko.SFTPClient:
    transport = paramiko.Transport(("127.0.0.1", 2022))
    transport.connect(username=env["SFTPGO_UPLOADER_USER"], password=env["SFTPGO_UPLOADER_PASSWORD"])
    return paramiko.SFTPClient.from_transport(transport)


def test_sftp_upload_lands_in_minio(env, s3_factory):
    name = f"smoke-{uuid.uuid4().hex[:8]}.mp4"
    sftp = _sftp(env)
    try:
        sftp.putfo(io.BytesIO(b"\x00" * 1024), name)
    finally:
        sftp.close()
    root = s3_factory(env["MINIO_ROOT_USER"], env["MINIO_ROOT_PASSWORD"])
    key = f"uploads/{env['SFTPGO_UPLOADER_USER']}/{name}"
    assert root.head_object(Bucket="landing", Key=key)["ContentLength"] == 1024
    root.delete_object(Bucket="landing", Key=key)


def test_sftp_rejects_disallowed_extension(env):
    sftp = _sftp(env)
    try:
        with pytest.raises(OSError):
            sftp.putfo(io.BytesIO(b"MZ"), "payload.exe")
    finally:
        sftp.close()


def test_postgres_databases_and_pgvector(env):
    dsn = f"postgresql://pixeltable:{env['PIXELTABLE_DB_PASSWORD']}@127.0.0.1:5432/pixeltable"
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT extname FROM pg_extension WHERE extname='vector'").fetchone()
    for db, user, pw in (("keycloak", "keycloak", "KEYCLOAK_DB_PASSWORD"), ("newsintel", "newsintel", "APP_DB_PASSWORD")):
        with psycopg.connect(f"postgresql://{user}:{env[pw]}@127.0.0.1:5432/{db}") as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
