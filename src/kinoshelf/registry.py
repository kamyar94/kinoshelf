"""A small read-only client for an image registry (ghcr.io, a local registry:2): resolve a tag, read the image's
labels, and fetch the image as an archive that `docker load` reads. Every manifest and blob is checked against
its sha256 digest while it is read; nothing unchecked is kept.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json")
ACCEPT = ", ".join(INDEX_TYPES + MANIFEST_TYPES)
CANDIDATE_TAG = "kinoshelf:candidate"  # the name `docker load` gives the fetched image; the host step renames it


class RegistryError(Exception):
    """A short, fixed explanation; never carries a token or a proxy address."""


@dataclass
class Image:
    """What a tag points at, checked."""

    digest: str  # what the tag resolved to (an index or a manifest)
    manifest_digest: str  # the linux/amd64 image manifest
    config_digest: str
    version: str = ""
    revision: str = ""
    created: str = ""
    size: int = 0  # compressed bytes to fetch
    layers_sha: str = ""  # sha256 of the config's layer list (diff_ids, one per line): what `docker load` keeps
    manifest: dict = field(default_factory=dict, repr=False)


def split_image(image: str) -> tuple[str, str]:
    """'ghcr.io/owner/name' -> ('ghcr.io', 'owner/name'). A first part without a dot or colon is Docker Hub."""
    image = image.strip().split("@")[0]
    first, _, rest = image.partition("/")
    if not rest or ("." not in first and ":" not in first and first != "localhost"):
        return "registry-1.docker.io", image if "/" in image else f"library/{image}"
    return first, rest


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _check(data: bytes, digest: str, what: str) -> None:
    if not digest.startswith("sha256:") or _sha256(data) != digest:
        raise RegistryError(f"The {what} does not match its digest")


class Registry:
    def __init__(self, image: str, client_for: Callable[[str], httpx.Client], insecure: bool = False,
                 platform: tuple[str, str] = ("linux", "amd64")):
        self.host, self.repo = split_image(image)
        self.scheme = "http" if insecure else "https"
        self.client_for = client_for
        self.platform = platform
        self._token: str | None = None

    def _url(self, path: str) -> str:
        return f"{self.scheme}://{self.host}/v2/{self.repo}/{path}"

    def _headers(self, accept: str | None = None) -> dict:
        h = {"Accept": accept} if accept else {}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _login(self, challenge: str) -> None:
        """Anonymous pull token from the realm a 401 names (ghcr.io and Docker Hub work this way)."""
        if not challenge.lower().startswith("bearer"):
            raise RegistryError("The registry wants a sign-in this updater does not do")
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop("realm", "")
        if not realm.startswith(("https://", "http://")):
            raise RegistryError("The registry gave no sign-in address")
        try:
            r = self.client_for(realm).get(realm, params=params)
        except httpx.HTTPError as e:
            raise RegistryError(f"Could not reach the registry's sign-in ({type(e).__name__})") from None
        if r.status_code != 200:
            raise RegistryError(f"The registry refused an anonymous pull (HTTP {r.status_code}); is the image public?")
        body = r.json()
        self._token = body.get("token") or body.get("access_token")
        if not self._token:
            raise RegistryError("The registry gave no pull token")

    def _get(self, path: str, accept: str | None = None) -> httpx.Response:
        url = self._url(path)
        for attempt in (1, 2):
            try:
                r = self.client_for(url).get(url, headers=self._headers(accept))
            except httpx.HTTPError as e:
                raise RegistryError(f"Could not reach {self.host} ({type(e).__name__})") from None
            if r.status_code == 401 and attempt == 1:
                self._login(r.headers.get("www-authenticate", ""))
                continue
            if r.status_code == 404:
                raise RegistryError(f"{self.repo} has no {path.rsplit('/', 1)[-1]} on {self.host}")
            if r.status_code >= 400:
                raise RegistryError(f"{self.host} answered HTTP {r.status_code}")
            return r
        raise RegistryError(f"{self.host} refused the pull token")

    def _manifest(self, ref: str) -> tuple[dict, str, str]:
        r = self._get(f"manifests/{ref}", ACCEPT)
        body = r.content
        digest = ref if ref.startswith("sha256:") else (r.headers.get("docker-content-digest") or _sha256(body))
        _check(body, digest, "manifest")
        doc = json.loads(body)
        return doc, digest, doc.get("mediaType") or r.headers.get("content-type", "").split(";")[0]

    def resolve(self, tag: str) -> Image:
        """The image a tag points at now, with its labels. Pins everything by digest."""
        doc, digest, kind = self._manifest(tag)
        man, man_digest = doc, digest
        if kind in INDEX_TYPES or "manifests" in doc:
            os_, arch = self.platform
            pick = [m for m in doc.get("manifests", []) if (m.get("platform") or {}).get("os") == os_
                    and (m.get("platform") or {}).get("architecture") == arch]
            if not pick:
                raise RegistryError(f"No {os_}/{arch} image under this tag")
            man, man_digest, _ = self._manifest(pick[0]["digest"])
        cfg_desc = man.get("config") or {}
        cfg_digest = cfg_desc.get("digest", "")
        cfg_raw = self._get(f"blobs/{cfg_digest}").content
        _check(cfg_raw, cfg_digest, "image settings")
        cfg = json.loads(cfg_raw)
        labels = (cfg.get("config") or {}).get("Labels") or {}
        return Image(digest=digest, manifest_digest=man_digest, config_digest=cfg_digest,
                     version=labels.get("org.opencontainers.image.version", ""),
                     revision=labels.get("org.opencontainers.image.revision", ""),
                     created=cfg.get("created", ""),
                     size=sum(int(x.get("size", 0)) for x in man.get("layers", [])),
                     layers_sha=hashlib.sha256("".join(f"{x}\n" for x in (cfg.get("rootfs") or {}).get(
                         "diff_ids", [])).encode()).hexdigest(),
                     manifest={"manifest": man, "config_raw": cfg_raw.decode("utf-8")})

    def fetch(self, img: Image, dest: Path, progress: Callable[[int], None] | None = None) -> str:
        """Write a `docker load` archive of the image to dest; returns the archive's sha256 (hex).

        Layers stream from the registry straight into the archive while their digests are computed; a layer
        that does not match removes the whole file.
        """
        man = img.manifest["manifest"]
        cfg_raw = img.manifest["config_raw"].encode("utf-8")
        cfg_name = img.config_digest.split(":", 1)[1] + ".json"
        layers = man.get("layers", [])
        names = [d["digest"].split(":", 1)[1] + (".tar.gz" if "gzip" in d.get("mediaType", "gzip") else ".tar")
                 for d in layers]
        part = dest.with_name(dest.name + ".part")
        done = 0
        try:
            with open(part, "wb") as raw:
                with tarfile.open(fileobj=raw, mode="w|", format=tarfile.USTAR_FORMAT) as tar:
                    _add(tar, cfg_name, cfg_raw)
                    for d, name in zip(layers, names):
                        size = int(d["size"])
                        url = self._url(f"blobs/{d['digest']}")
                        try:
                            with self.client_for(url).stream("GET", url, headers=self._headers(),
                                                             timeout=httpx.Timeout(30.0, read=120.0)) as r:
                                if r.status_code >= 400:
                                    raise RegistryError(f"{self.host} answered HTTP {r.status_code} for a layer")
                                reader = _HashingReader(r.iter_bytes(1 << 20))
                                info = tarfile.TarInfo(name)
                                info.size, info.mtime, info.mode = size, int(time.time()), 0o644
                                tar.addfile(info, reader)
                                if reader.read(1) or reader.count != size:
                                    raise RegistryError("A layer is not the size the manifest says")
                        except httpx.HTTPError as e:
                            raise RegistryError(f"The download from {self.host} broke off ({type(e).__name__})") \
                                from None
                        if "sha256:" + reader.hash.hexdigest() != d["digest"]:
                            raise RegistryError("A layer does not match its digest")
                        done += size
                        if progress:
                            progress(done)
                    manifest = [{"Config": cfg_name, "RepoTags": [CANDIDATE_TAG], "Layers": names}]
                    _add(tar, "manifest.json", json.dumps(manifest).encode("utf-8"))
            h = hashlib.sha256()
            with open(part, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            part.replace(dest)
            return h.hexdigest()
        except BaseException:
            part.unlink(missing_ok=True)
            raise


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
    tar.addfile(info, io.BytesIO(data))


class _HashingReader:
    """A file-like view of an HTTP body that hashes what passes through and counts it."""

    def __init__(self, chunks):
        self._it = iter(chunks)
        self._buf = b""
        self.hash = hashlib.sha256()
        self.count = 0

    def read(self, n: int = -1) -> bytes:
        while n < 0 or len(self._buf) < n:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        out, self._buf = (self._buf, b"") if n < 0 else (self._buf[:n], self._buf[n:])
        self.hash.update(out)
        self.count += len(out)
        return out
