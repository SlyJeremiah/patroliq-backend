"""
Database file storage: uploads and generated reports kept as rows in PostgreSQL.

For hosts without a persistent disk (Render's free web services start from a clean filesystem on every
deploy and restart) and without an object store configured. Files are small (photos are compressed on
the phone; reports are a few hundred KB) and are only ever streamed through authenticated, tenant-scoped
API views, so a plain ``bytea`` column is enough. Prefer R2 once volumes grow.
"""
from __future__ import annotations

import posixpath

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class DatabaseStorage(Storage):
    def _model(self):
        from .models import StoredFile

        return StoredFile

    def _open(self, name, mode="rb"):
        row = self._model().objects.filter(name=name).only("content").first()
        if row is None:
            raise FileNotFoundError(name)
        f = ContentFile(bytes(row.content))
        f.name = name
        return f

    def _save(self, name, content):
        if hasattr(content, "seek"):
            content.seek(0)
        data = content.read()
        if isinstance(data, str):
            data = data.encode()
        self._model().objects.create(name=name, content=data, size=len(data))
        return name

    def exists(self, name):
        return self._model().objects.filter(name=name).exists()

    def delete(self, name):
        self._model().objects.filter(name=name).delete()

    def size(self, name):
        row = self._model().objects.filter(name=name).only("size").first()
        if row is None:
            raise FileNotFoundError(name)
        return row.size

    def get_modified_time(self, name):
        row = self._model().objects.filter(name=name).only("created_at").first()
        if row is None:
            raise FileNotFoundError(name)
        return row.created_at

    get_created_time = get_modified_time

    def listdir(self, path):
        prefix = path.rstrip("/") + "/" if path else ""
        dirs, files = set(), []
        for name in self._model().objects.filter(name__startswith=prefix).values_list("name", flat=True):
            rest = name[len(prefix):]
            if "/" in rest:
                dirs.add(rest.split("/", 1)[0])
            else:
                files.append(rest)
        return sorted(dirs), sorted(files)

    def url(self, name):
        # Never public: files are served by the authenticated API views.
        raise NotImplementedError("Database-stored files are served through the API.")

    def get_available_name(self, name, max_length=None):
        name = super().get_available_name(name, max_length=max_length)
        return posixpath.normpath(name)
