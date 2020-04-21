import os
import io
import json
import re
import sys
import tarfile
import typing
import urllib.parse


class _Context:
    def __init__(self, base):
        try:
            self.uri = os.environ[f"CONDUCTO_{base}_DATA_PATH"]
        except KeyError:
            raise RuntimeError(
                f"co.{base.lower()}_data is enabled for use in pipeline nodes.  Perhaps you intended to use co.Exec with a Python function that calls co.{base.lower()}_data."
            )
        if self.uri.startswith("s3://"):
            import boto3
            from conducto.api import Auth

            self.is_s3 = True

            auth = Auth()
            token = os.environ["CONDUCTO_DATA_TOKEN"]
            token = auth.get_refreshed_token(token)
            creds = auth.get_credentials(token)

            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretKey"],
                aws_session_token=creds["SessionToken"],
            )
            self.s3 = session.resource("s3")
            m = re.search("^s3://(.*?)/(.*)", self.uri)
            self.bucket, self.key_root = m.group(1, 2)
        else:
            self.uri = os.path.expanduser(self.uri)
            self.is_s3 = False

    def get_s3_key(self, name):
        return _safe_join(self.key_root, name)

    def get_s3_obj(self, name):
        return self.s3.Object(self.bucket, self.get_s3_key(name))

    def get_path(self, name):
        return _safe_join(self.uri, name)


class _Data:
    @staticmethod
    def _ctx():
        raise NotImplementedError()

    @classmethod
    def get(cls, name, file):
        """
        Get object at `name`, store it to `file`.
        """
        ctx = cls._ctx()
        if ctx.is_s3:
            return ctx.get_s3_obj(name).download_file(file)
        else:
            import shutil

            shutil.copy(ctx.get_path(name), file)

    @classmethod
    def gets(cls, name, *, byte_range: typing.List[int] = None) -> bytes:
        """
        Return object at `name`. Optionally restrict to the given `byte_range`.
        """
        ctx = cls._ctx()
        if ctx.is_s3:
            kwargs = {}
            if byte_range:
                begin, end = byte_range
                kwargs["Range"] = f"bytes {begin}-{end}"
            return ctx.get_s3_obj(name).get(**kwargs)["Body"].read()
        else:
            with open(ctx.get_path(name), "rb") as f:
                if byte_range:
                    begin, end = byte_range
                    f.seek(begin)
                    return f.read(end - begin)
                else:
                    return f.read()

    @classmethod
    def put(cls, name, file):
        """
        Store object in `file` to `name`.
        """
        ctx = cls._ctx()
        if ctx.is_s3:
            ctx.get_s3_obj(name).upload_file(file)
        else:
            # Make sure to write the obj atomically. Write to a temp file then move it
            # into the final location. If anything goes wrong delete the temp file.
            import tempfile, shutil

            path = ctx.get_path(name)
            dirpath = os.path.dirname(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmppath = tempfile.mkstemp(dir=dirpath)
            try:
                shutil.copy(file, tmppath)
            except Exception:
                os.remove(tmppath)
                raise
            else:
                shutil.move(tmppath, path)

    @classmethod
    def puts(cls, name, obj: bytes):
        if not isinstance(obj, bytes):
            raise ValueError(f"Expected 'obj' of type 'bytes', but got {type(bytes)}")
        ctx = cls._ctx()
        if ctx.is_s3:
            ctx.get_s3_obj(name).put(Body=obj)
        else:
            # Make sure to write the obj atomically. Write to a temp file then move it
            # into the final location. If anything goes wrong delete the temp file.
            import tempfile, shutil

            path = ctx.get_path(name)
            dirpath = os.path.dirname(path)
            os.makedirs(dirpath, exist_ok=True)
            fd, tmppath = tempfile.mkstemp(dir=dirpath)
            try:
                with open(fd, "wb") as f:
                    f.write(obj)
            except Exception:
                os.remove(tmppath)
                raise
            else:
                shutil.move(tmppath, path)

    @classmethod
    def delete(cls, name):
        """
        Delete object at `name`.
        """
        ctx = cls._ctx()
        if ctx.is_s3:
            return ctx.get_s3_obj(name).delete()
        else:
            os.remove(ctx.get_path(name))

    @classmethod
    def list(cls, prefix):
        """
        Return names of objects that start with `prefix`.
        """
        # TODO: make this more like listdir or more like glob. Right now pattern matching is inconsistent between local and cloud.
        ctx = cls._ctx()
        if ctx.is_s3:
            bkt = ctx.s3.Bucket(ctx.bucket)
            return [obj.key for obj in bkt.objects.filter(Prefix=prefix)]
        else:
            path = ctx.get_path(prefix)
            try:
                names = os.listdir(path)
            except OSError:
                return []
            return [_safe_join(prefix, name) for name in sorted(names)]

    @classmethod
    def exists(cls, name):
        """
        Test if there is an object at `name`.
        """
        ctx = cls._ctx()
        if ctx.is_s3:
            import botocore.exceptions

            try:
                ctx.s3.head_object(Bucket=ctx.bucket, Key=ctx.get_s3_obj(name))
            except botocore.exceptions.ClientError:
                return False
            else:
                return True
        else:
            return os.path.exists(ctx.get_path(name))

    @classmethod
    def size(cls, name):
        ctx = cls._ctx()
        if ctx.is_s3:
            result = ctx.s3.head_object(Bucket=ctx.bucket, Key=ctx.get_s3_obj(name))
            return result["ContentLength"]
        else:
            return os.stat(ctx.get_path(name)).st_size

    @classmethod
    def clear_cache(cls, name, checksum=None):
        """
        Clear cache at `name` with `checksum`, clears all `name` cache if no `checksum`.
        """
        data_path = f"conducto-cache/{name}"
        if checksum is None:
            for file in cls.list(data_path):
                cls.delete(file)
        else:
            cls.delete(f"{data_path}/{checksum}.tar.gz")

    @classmethod
    def cache_exists(cls, name, checksum):
        """
        Test if there is a cache at `name` with `checksum`.
        """
        data_path = f"conducto-cache/{name}/{checksum}.tar.gz"
        return cls.exists(data_path)

    @classmethod
    def save_cache(cls, name, checksum, save_dir):
        """
        Save `save_dir` to cache at `name` with `checksum`.
        """
        data_path = f"conducto-cache/{name}/{checksum}.tar.gz"
        tario = io.BytesIO()
        with tarfile.TarFile(fileobj=tario, mode="w") as cmdtar:
            cmdtar.add(save_dir, arcname=os.path.basename(os.path.normpath(save_dir)))
        cls.puts(data_path, tario.getvalue())

    @classmethod
    def restore_cache(cls, name, checksum, restore_dir):
        """
        Restore cache at `name` with `checksum` to `restore_dir`.
        """
        data_path = f"conducto-cache/{name}/{checksum}.tar.gz"
        if not cls.cache_exists(name, checksum):
            raise FileNotFoundError("Cache not found")
        byte_array = cls.gets(data_path)
        file_like = io.BytesIO(byte_array)
        tar = tarfile.open(fileobj=file_like)
        tar.extractall(path=restore_dir)

    @classmethod
    def url(cls, name):
        """
        Get url for object at `name`.
        """
        # Convert CamelCase to snake_case
        # https://stackoverflow.com/questions/1175208/elegant-python-function-to-convert-camelcase-to-snake-case
        data_type = re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()

        pipeline_id = os.environ["CONDUCTO_PIPELINE_ID"]
        conducto_url = os.environ["CONDUCTO_AUTO_URL"]
        qname = urllib.parse.quote(name)
        return f"{conducto_url}/pgw/data/{pipeline_id}/{data_type}/{qname}"

    @classmethod
    def _main(cls):
        import conducto as co

        variables = {
            "delete": cls.delete,
            "exists": cls.exists,
            "get": cls.get,
            "gets": cls._gets_from_command_line,
            "list": cls.list,
            "put": cls.put,
            "puts": cls._puts_from_command_line,
            "url": cls.url,
            "cache-exists": cls.cache_exists,
            "clear-cache": cls.clear_cache,
            "save-cache": cls.save_cache,
            "restore-cache": cls.restore_cache,
        }
        co.main(variables=variables, printer=cls._print)

    @classmethod
    def _gets_from_command_line(cls, name, *, byte_range: typing.List[int] = None):
        """
        Read object stored at `name` and write it to stdout. Use `byte_range=start,end`
        to optionally specify a [start, end) range within the object to read.
        """
        obj = cls.gets(name, byte_range=byte_range)
        sys.stdout.buffer.write(obj)

    @classmethod
    def _puts_from_command_line(cls, name):
        """
        Read object from stdin and store it to `name`.
        """
        obj = sys.stdin.read().encode()
        return cls.puts(name, obj)

    @classmethod
    def _print(cls, val):
        if val is None:
            return
        if isinstance(val, bytes):
            val = val.decode()
        print(json.dumps(val))


class temp_data(_Data):
    @staticmethod
    def _ctx():
        return _Context(base="TEMP")


class perm_data(_Data):
    @staticmethod
    def _ctx():
        return _Context(base="PERM")


def _safe_join(*parts):
    parts = list(parts)
    parts[1:] = [p.lstrip(os.path.sep) for p in parts[1:]]
    return os.path.join(*parts)