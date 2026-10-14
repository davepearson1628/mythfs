#!/usr/bin/env python3
"""
mythfs_v36_recgroups_refresh.py

Read-only MythTV v36 filesystem using pyfuse3.

Features
--------
* MythTV Recording Groups as top-level folders.
* Deleted and LiveTV excluded by default.
* Jellyfin / Emby / Plex-friendly TV layout:
      Group/Series/Season 01/Series - S01E01 - Episode.ts
* Movie year: recordedprogram.airdate, then MythTV tmdb3.py fallback:
      Movies/Movie Title (2024)/Movie Title (2024).ts
* TMDB-derived years are cached persistently.
* Missing TMDB years are resolved in the background; mount startup does not wait.
* Automatic database refresh for additions, deletions, recgroup changes,
  title/episode metadata changes and movie-year changes.
* Stable inode numbers for unchanged virtual paths.
* Multiple MythTV Default Storage Group directories are discovered automatically.
* Optional storage_dirs entries can add/override locally mounted recording paths.
* Strictly read-only. The real MythTV recordings are never renamed,
  moved, deleted or modified.

Default backing recording directory:
    /media/myth/tv

Default refresh interval:
    300 seconds
"""

import argparse
import configparser
import errno
import json
import logging
import os
import re
import stat
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional

import pyfuse3
import trio
from MythTV import MythDB


LOG = logging.getLogger("mythfs")

DEFAULT_EXCLUDED_GROUPS = {"Deleted", "LiveTV"}
DEFAULT_MOVIE_GROUPS = {"Movies"}

BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_component(value, fallback="Untitled"):
    value = str(value or "").strip()
    value = BAD_CHARS.sub(" - ", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(" .")

    if not value or value in (".", ".."):
        return fallback

    return value


def valid_year(value):
    """Return a sensible four-digit year or 0."""
    if value is None:
        return 0

    if isinstance(value, (date, datetime)):
        year = int(value.year)
    else:
        text = str(value).strip()
        match = re.match(r"^(\d{4})", text)
        if not match:
            return 0
        year = int(match.group(1))

    if 1880 <= year <= 2200:
        return year

    return 0


def rec_title(rec):
    return clean_component(getattr(rec, "title", "") or "Untitled")


def rec_subtitle(rec):
    raw = getattr(rec, "subtitle", "") or ""
    return clean_component(raw, "") if raw else ""


def rec_ext(rec):
    basename = str(getattr(rec, "basename", "") or "")
    return Path(basename).suffix or ".ts"


def episode_filename(rec):
    title = rec_title(rec)
    subtitle = rec_subtitle(rec)
    season = int(getattr(rec, "season", 0) or 0)
    episode = int(getattr(rec, "episode", 0) or 0)

    name = f"{title} - S{season:02d}E{episode:02d}"
    if subtitle:
        name += f" - {subtitle}"

    return clean_component(name) + rec_ext(rec)


def normal_filename(rec):
    title = rec_title(rec)
    subtitle = rec_subtitle(rec)

    name = title
    if subtitle:
        name += f" - {subtitle}"

    return clean_component(name) + rec_ext(rec)


@dataclass
class DBMeta:
    recgroup: str = "Default"
    year: int = 0
    inetref: str = ""


@dataclass
class Node:
    inode: int
    name: bytes
    parent: Optional[int]
    is_dir: bool
    real_path: Optional[str] = None
    children: Dict[bytes, int] = field(default_factory=dict)


class MythFS(pyfuse3.Operations):
    enable_writeback_cache = False

    def __init__(
        self,
        storage_dirs,
        discover_storage_groups,
        excluded_groups,
        movie_groups,
        refresh_interval,
        tmdb_grabber,
        cache_file,
        db_connection=None,
        metadata_cache=None,
        offline_mode='disabled',
    ):
        super().__init__()
        self.db_connection = db_connection
        self.metadata_cache = metadata_cache
        self.offline_mode = offline_mode

        self.configured_storage_dirs = self._normalise_storage_dirs(storage_dirs)
        self.discover_storage_groups = bool(discover_storage_groups)
        self.active_storage_dirs = list(self.configured_storage_dirs)
        self.excluded_groups = {
            str(name).casefold() for name in excluded_groups
        }
        self.movie_groups = {
            str(name).casefold() for name in movie_groups
        }
        self.refresh_interval = max(0, int(refresh_interval))
        self.tmdb_grabber = os.path.abspath(tmdb_grabber)
        self.cache_file = os.path.abspath(os.path.expanduser(cache_file))
        self.movie_year_cache = self.load_movie_year_cache()
        self.tmdb_fail_until = {}
        self.tmdb_pending = set()
        self.tmdb_wakeup = trio.Event()

        self.next_inode = pyfuse3.ROOT_INODE + 1

        # Stable for the lifetime of the mount.
        self.path_to_inode: Dict[str, int] = {"/": pyfuse3.ROOT_INODE}

        self.nodes: Dict[int, Node] = {}
        self.nodes[pyfuse3.ROOT_INODE] = Node(
            inode=pyfuse3.ROOT_INODE,
            name=b"",
            parent=None,
            is_dir=True,
        )

        self.visible_paths = {"/"}
        self.initialized = False

        self.refresh(initial=True)

    def alloc_inode_for_path(self, virtual_path):
        inode = self.path_to_inode.get(virtual_path)

        if inode is not None:
            return inode

        inode = self.next_inode
        self.next_inode += 1
        self.path_to_inode[virtual_path] = inode
        return inode

    def node(self, inode):
        try:
            return self.nodes[inode]
        except KeyError:
            raise pyfuse3.FUSEError(errno.ENOENT)

    @staticmethod
    def recorded_columns(db):
        columns = set()

        with db as cur:
            cur.execute("SHOW COLUMNS FROM recorded")
            for row in cur:
                columns.add(str(row[0]))

        return columns

    @staticmethod
    def _normalise_storage_dirs(paths):
        result = []
        seen = set()

        for path in paths or []:
            path = os.path.abspath(os.path.expanduser(str(path).strip()))
            if not path or path in seen:
                continue
            seen.add(path)
            result.append(path)

        return result

    @classmethod
    def load_storage_groups(cls, db):
        """
        Return MythTV Default Storage Group directories as:

            ({hostname: [dir, ...]}, [all_dirs...])

        Storage paths are only candidates. MythFS never assumes that a path
        belonging to another backend is locally accessible; resolve_recording()
        verifies the actual file exists before using it.
        """
        by_host = {}
        all_dirs = []

        with db as cur:
            cur.execute("""
                SELECT hostname, dirname
                FROM storagegroup
                WHERE groupname = 'Default'
                ORDER BY hostname, id
            """)

            for hostname, dirname in cur:
                if not dirname:
                    continue

                path = os.path.abspath(os.path.expanduser(str(dirname)))
                host = str(hostname or "")

                by_host.setdefault(host, [])
                if path not in by_host[host]:
                    by_host[host].append(path)

                if path not in all_dirs:
                    all_dirs.append(path)

        return by_host, all_dirs

    def resolve_recording(self, basename, hostname, by_host, all_dirs):
        """
        Resolve a recording to a locally accessible backing file.

        Search order:
          1. Explicit storage_dirs from /etc/mythfs.conf
          2. Default Storage Group directories for recorded.hostname
          3. All other discovered Default Storage Group directories

        This works for multiple local disks as well as shared storage mounted
        at the same or different paths on the machine running MythFS.
        """
        candidates = []

        def add(paths):
            for path in paths:
                if path not in candidates:
                    candidates.append(path)

        add(self.configured_storage_dirs)
        add(by_host.get(str(hostname or ""), []))
        add(all_dirs)

        for directory in candidates:
            candidate = os.path.join(directory, basename)
            if os.path.isfile(candidate):
                return candidate, directory

        return None, None

    @classmethod
    def load_db_metadata(cls, db):
        """
        Return basename -> DBMeta.

        Recording Group comes from recorded.recgroup.

        Movie release year first comes from recordedprogram.airdate, joined
        to the exact recording by chanid + starttime.

        inetref is retained so Movies with no archived airdate can fall back
        to MythTV's own tmdb3.py metadata grabber.
        """
        result = {}

        with db as cur:
            cur.execute("""
                SELECT
                    r.basename,
                    r.recgroup,
                    r.inetref,
                    rp.airdate
                FROM recorded AS r
                LEFT JOIN recordedprogram AS rp
                  ON rp.chanid = r.chanid
                 AND rp.starttime = r.starttime
            """)

            for basename, recgroup, inetref, airdate in cur:
                if not basename:
                    continue

                result[str(basename)] = DBMeta(
                    recgroup=str(recgroup or "Default"),
                    year=valid_year(airdate),
                    inetref=str(inetref or ""),
                )

        return result

    def load_movie_year_cache(self):
        try:
            with open(self.cache_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except Exception:
            LOG.exception("Unable to read movie-year cache %s", self.cache_file)
            return {}

        result = {}
        if isinstance(data, dict):
            for key, value in data.items():
                year = valid_year(value)
                if year:
                    result[str(key)] = year

        LOG.info(
            "Loaded %d cached movie year(s) from %s",
            len(result),
            self.cache_file,
        )
        return result

    def save_movie_year_cache(self):
        directory = os.path.dirname(self.cache_file)
        if directory:
            os.makedirs(directory, exist_ok=True)

        tmp = self.cache_file + ".tmp"

        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                self.movie_year_cache,
                fh,
                indent=2,
                sort_keys=True,
            )
            fh.write("\n")

        os.replace(tmp, self.cache_file)

    @staticmethod
    def tmdb_id_from_inetref(inetref):
        match = re.fullmatch(r"(?:tmdb3\.py_)?(\d+)", str(inetref or "").strip())
        if not match:
            return None
        return match.group(1)

    def tmdb_year(self, inetref):
        """
        Return a cached TMDB year if available.

        If the year is not cached, queue the TMDB ID for background resolution
        and return 0 immediately. This keeps filesystem startup and refreshes
        non-blocking even when TMDB or the MythTV grabber is slow.
        """
        tmdb_id = self.tmdb_id_from_inetref(inetref)
        if not tmdb_id:
            return 0

        cached = valid_year(self.movie_year_cache.get(tmdb_id))
        if cached:
            return cached

        now = time.time()
        if now < self.tmdb_fail_until.get(tmdb_id, 0):
            return 0

        if tmdb_id not in self.tmdb_pending:
            self.tmdb_pending.add(tmdb_id)

            # Wake the background resolver. trio.Event cannot be reset, so
            # replace it after consumption in the resolver loop.
            try:
                self.tmdb_wakeup.set()
            except Exception:
                pass

        return 0

    def resolve_tmdb_year_sync(self, tmdb_id):
        """
        Blocking helper executed in a worker thread.

        Returns a valid year or 0. Successful lookups are cached persistently.
        Failed lookups are throttled for one hour.
        """
        now = time.time()

        cached = valid_year(self.movie_year_cache.get(tmdb_id))
        if cached:
            return cached

        if now < self.tmdb_fail_until.get(tmdb_id, 0):
            return 0

        if not os.path.isfile(self.tmdb_grabber):
            LOG.warning("TMDB grabber not found: %s", self.tmdb_grabber)
            self.tmdb_fail_until[tmdb_id] = now + 3600
            return 0

        try:
            proc = subprocess.run(
                [self.tmdb_grabber, "-D", tmdb_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired:
            LOG.warning(
                "TMDB lookup timed out after 15 seconds for %s",
                tmdb_id,
            )
            self.tmdb_fail_until[tmdb_id] = now + 3600
            return 0
        except Exception:
            LOG.exception("TMDB lookup failed for %s", tmdb_id)
            self.tmdb_fail_until[tmdb_id] = now + 3600
            return 0

        if proc.returncode != 0:
            LOG.warning(
                "TMDB lookup %s failed with exit code %d: %s",
                tmdb_id,
                proc.returncode,
                proc.stderr.strip(),
            )
            self.tmdb_fail_until[tmdb_id] = now + 3600
            return 0

        try:
            root = ET.fromstring(proc.stdout)
            year_text = root.findtext(".//item/year")
            year = valid_year(year_text)
        except Exception:
            LOG.exception("Unable to parse TMDB metadata for %s", tmdb_id)
            self.tmdb_fail_until[tmdb_id] = now + 3600
            return 0

        if not year:
            LOG.warning("TMDB metadata for %s contained no valid year", tmdb_id)
            self.tmdb_fail_until[tmdb_id] = now + 3600
            return 0

        self.movie_year_cache[tmdb_id] = year

        try:
            self.save_movie_year_cache()
        except Exception:
            LOG.exception("Unable to save movie-year cache %s", self.cache_file)

        LOG.info("TMDB movie year cached: %s -> %d", tmdb_id, year)
        return year

    async def tmdb_resolver_loop(self):
        """
        Resolve queued TMDB IDs in the background, one at a time.

        Each grabber invocation runs in a worker thread so pyfuse3 and the
        refresh loop stay responsive. When a new year is learned, rebuild the
        namespace immediately so the movie becomes Title (YEAR) without
        waiting for the next periodic refresh.
        """
        LOG.info("Background TMDB movie-year resolver enabled")

        while True:
            if not self.tmdb_pending:
                event = self.tmdb_wakeup
                await event.wait()

                # Replace the consumed Event for the next wakeup.
                if self.tmdb_wakeup is event:
                    self.tmdb_wakeup = trio.Event()

            while self.tmdb_pending:
                tmdb_id = sorted(self.tmdb_pending)[0]
                self.tmdb_pending.discard(tmdb_id)

                year = await trio.to_thread.run_sync(
                    self.resolve_tmdb_year_sync,
                    tmdb_id,
                )

                if year:
                    try:
                        added, removed = self.refresh(initial=False)
                        LOG.info(
                            "Applied TMDB year %s -> %d "
                            "(%d path(s) added, %d removed)",
                            tmdb_id,
                            year,
                            added,
                            removed,
                        )
                    except Exception:
                        LOG.exception(
                            "Unable to refresh after TMDB year lookup %s",
                            tmdb_id,
                        )

    def _build_desired_tree_online(self):
        db = (MythDB(**self.db_connection)
              if self.db_connection else MythDB())

        dbmeta = self.load_db_metadata(db)
        recordings = list(db.searchRecorded())

        if self.discover_storage_groups:
            storage_by_host, discovered_storage_dirs = self.load_storage_groups(db)
        else:
            storage_by_host, discovered_storage_dirs = {}, []

        active_dirs = []
        for directory in self.configured_storage_dirs + discovered_storage_dirs:
            if directory not in active_dirs:
                active_dirs.append(directory)
        self.active_storage_dirs = active_dirs

        dirs = {"/"}
        files = {}

        stats = {
            "database": len(recordings),
            "published": 0,
            "excluded": 0,
            "missing": 0,
            "groups": set(),
        }

        names_in_dir: Dict[str, set] = {}

        def add_dir(path):
            dirs.add(path)
            names_in_dir.setdefault(path, set())

        def unique_file(parent, filename):
            used = names_in_dir.setdefault(parent, set())
            filename = clean_component(filename)

            if filename.casefold() not in used:
                used.add(filename.casefold())
                return filename

            p = Path(filename)
            stem = p.stem
            suffix = p.suffix

            n = 2
            while True:
                candidate = f"{stem} ({n}){suffix}"
                if candidate.casefold() not in used:
                    used.add(candidate.casefold())
                    return candidate
                n += 1

        for rec in recordings:
            basename = str(getattr(rec, "basename", "") or "")
            if not basename:
                continue

            meta = dbmeta.get(basename, DBMeta())
            group = meta.recgroup or "Default"

            if group.casefold() in self.excluded_groups:
                stats["excluded"] += 1
                continue

            hostname = getattr(rec, "hostname", "") or ""
            real_path, resolved_dir = self.resolve_recording(
                basename,
                hostname,
                storage_by_host,
                discovered_storage_dirs,
            )

            if real_path is None:
                stats["missing"] += 1
                LOG.debug(
                    "Recording not locally accessible: %s "
                    "(hostname=%s)",
                    basename,
                    hostname,
                )
                continue

            if resolved_dir and resolved_dir not in self.active_storage_dirs:
                self.active_storage_dirs.append(resolved_dir)

            group = clean_component(group, "Default")
            stats["groups"].add(group)

            group_path = "/" + group
            add_dir(group_path)

            title = rec_title(rec)
            season = int(getattr(rec, "season", 0) or 0)
            episode = int(getattr(rec, "episode", 0) or 0)

            if group.casefold() in self.movie_groups:
                movie_title = title

                movie_year = meta.year
                if not movie_year and meta.inetref:
                    movie_year = self.tmdb_year(meta.inetref)

                if movie_year:
                    movie_title = f"{title} ({movie_year})"

                movie_dir = group_path + "/" + movie_title
                add_dir(movie_dir)

                filename = unique_file(
                    movie_dir,
                    movie_title + rec_ext(rec),
                )

                files[movie_dir + "/" + filename] = real_path

            else:
                title_path = group_path + "/" + title
                add_dir(title_path)

                if season or episode:
                    season_path = title_path + f"/Season {season:02d}"
                    add_dir(season_path)

                    filename = unique_file(
                        season_path,
                        episode_filename(rec),
                    )

                    files[season_path + "/" + filename] = real_path

                else:
                    filename = unique_file(
                        title_path,
                        normal_filename(rec),
                    )

                    files[title_path + "/" + filename] = real_path

            stats["published"] += 1

        return dirs, files, stats


    def _save_offline_catalogue(self, dirs, files, stats):
        """Atomically save a verified, nonempty online catalogue."""
        if not self.metadata_cache or not files:
            return

        import tempfile

        directory = os.path.dirname(self.metadata_cache)
        os.makedirs(directory, exist_ok=True)

        data = {
            "version": 1,
            "saved_at": datetime.now().astimezone().isoformat(),
            "directories": sorted(dirs),
            "files": files,
        }

        fd, tmp = tempfile.mkstemp(
            prefix=".recordings-",
            suffix=".json",
            dir=directory,
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, sort_keys=True)
                fh.write(chr(10))
                fh.flush()
                os.fsync(fh.fileno())

            os.chmod(tmp, 0o600)
            os.replace(tmp, self.metadata_cache)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _load_offline_catalogue(self):
        """Load cached virtual paths, retaining only accessible recordings."""
        if not self.metadata_cache:
            raise RuntimeError("No offline catalogue configured")

        with open(self.metadata_cache, encoding="utf-8") as fh:
            data = json.load(fh)

        if not isinstance(data, dict) or data.get("version") != 1:
            raise RuntimeError("Invalid offline catalogue version")

        stored = data.get("files")
        if not isinstance(stored, dict) or not stored:
            raise RuntimeError("Offline catalogue empty or invalid")

        files = {}
        dirs = {"/"}
        permitted = [
            os.path.realpath(path)
            for path in self.configured_storage_dirs
        ]

        for virtual, real in stored.items():
            if not isinstance(virtual, str) or not isinstance(real, str):
                continue
            if not virtual.startswith("/") or "/../" in virtual:
                continue
            if virtual.endswith("/..") or virtual.endswith("/."):
                continue

            resolved = os.path.realpath(real)

            if not any(
                os.path.commonpath([resolved, root]) == root
                for root in permitted
            ):
                continue

            if not os.path.isfile(resolved):
                continue

            files[virtual] = real
            parent = str(Path(virtual).parent)
            while parent != "/":
                dirs.add(parent)
                parent = str(Path(parent).parent)

        if not files:
            raise RuntimeError(
                "Offline catalogue has no accessible recordings"
            )

        stats = {
            "database": len(stored),
            "published": len(files),
            "excluded": 0,
            "missing": len(stored) - len(files),
            "groups": {
                path.split("/")[1]
                for path in files
                if len(path.split("/")) > 2
            },
        }

        LOG.warning(
            "Using offline catalogue: %d recordings; saved %s",
            len(files),
            data.get("saved_at", "unknown"),
        )

        return dirs, files, stats

    def build_desired_tree(self):
        if self.offline_mode == "cache":
            return self._load_offline_catalogue()

        try:
            dirs, files, stats = self._build_desired_tree_online()
        except Exception:
            if self.offline_mode != "auto":
                raise

            LOG.warning(
                "MythTV database unavailable; using offline catalogue",
                exc_info=True,
            )
            return self._load_offline_catalogue()

        if self.offline_mode == "auto":
            try:
                self._save_offline_catalogue(dirs, files, stats)
            except Exception:
                LOG.exception(
                    "Unable to save offline catalogue"
                )

        return dirs, files, stats

    @staticmethod
    def parent_and_name(path):
        if path == "/":
            return None, b""

        parent = str(Path(path).parent)
        if parent == ".":
            parent = "/"

        return parent, os.fsencode(Path(path).name)

    def install_tree(self, dirs, files):
        old_visible = set(self.visible_paths)
        new_visible = set(dirs) | set(files.keys())

        for path in sorted(dirs, key=lambda p: (p.count("/"), p.casefold())):
            self.alloc_inode_for_path(path)

        for path in sorted(files):
            self.alloc_inode_for_path(path)

        fresh_nodes = {}

        fresh_nodes[pyfuse3.ROOT_INODE] = Node(
            inode=pyfuse3.ROOT_INODE,
            name=b"",
            parent=None,
            is_dir=True,
        )

        for path in sorted(
            (p for p in dirs if p != "/"),
            key=lambda p: (p.count("/"), p.casefold()),
        ):
            inode = self.path_to_inode[path]
            parent_path, name = self.parent_and_name(path)
            parent_inode = self.path_to_inode[parent_path]

            fresh_nodes[inode] = Node(
                inode=inode,
                name=name,
                parent=parent_inode,
                is_dir=True,
            )

        for path, real_path in files.items():
            inode = self.path_to_inode[path]
            parent_path, name = self.parent_and_name(path)
            parent_inode = self.path_to_inode[parent_path]

            fresh_nodes[inode] = Node(
                inode=inode,
                name=name,
                parent=parent_inode,
                is_dir=False,
                real_path=real_path,
            )

        for path in new_visible:
            if path == "/":
                continue

            inode = self.path_to_inode[path]
            node = fresh_nodes[inode]
            parent = fresh_nodes.get(node.parent)

            if parent is not None:
                parent.children[node.name] = inode

        # Keep stale nodes around for old kernel inode references, but they
        # are no longer linked from any live directory.
        for inode, old_node in self.nodes.items():
            fresh_nodes.setdefault(inode, old_node)

        self.nodes = fresh_nodes
        self.visible_paths = new_visible

        return old_visible, new_visible

    def invalidate_changes(self, old_visible, new_visible):
        if not self.initialized:
            return

        changed = old_visible.symmetric_difference(new_visible)

        for path in changed:
            if path == "/":
                continue

            parent_path, name = self.parent_and_name(path)
            parent_inode = self.path_to_inode.get(parent_path)

            if parent_inode is None:
                continue

            deleted_inode = 0
            if path in old_visible and path not in new_visible:
                deleted_inode = self.path_to_inode.get(path, 0)

            pyfuse3.invalidate_entry_async(
                parent_inode,
                name,
                deleted=deleted_inode,
                ignore_enoent=True,
            )

        try:
            pyfuse3.invalidate_inode(
                pyfuse3.ROOT_INODE,
                attr_only=True,
            )
        except OSError as exc:
            if exc.errno != errno.ENOSYS:
                LOG.debug("invalidate_inode(root): %s", exc)

    def refresh(self, initial=False):
        dirs, files, stats = self.build_desired_tree()
        old_visible, new_visible = self.install_tree(dirs, files)

        added = len(new_visible - old_visible)
        removed = len(old_visible - new_visible)

        LOG.info(
            "Published %d recordings; %d excluded; %d missing; "
            "%d paths added; %d paths removed",
            stats["published"],
            stats["excluded"],
            stats["missing"],
            added,
            removed,
        )

        LOG.info(
            "Recording Groups: %s",
            ", ".join(sorted(stats["groups"], key=str.casefold))
            if stats["groups"]
            else "(none)",
        )

        LOG.info(
            "Locally considered recording directories: %s",
            ", ".join(self.active_storage_dirs)
            if self.active_storage_dirs
            else "(none)",
        )

        if not initial:
            self.invalidate_changes(old_visible, new_visible)

        return added, removed

    async def refresh_loop(self):
        if self.refresh_interval <= 0:
            return

        LOG.info(
            "Automatic refresh enabled every %d seconds",
            self.refresh_interval,
        )

        while True:
            await trio.sleep(self.refresh_interval)

            try:
                added, removed = self.refresh(initial=False)

                if added or removed:
                    LOG.info(
                        "Automatic refresh changed namespace: "
                        "%d path(s) added, %d removed",
                        added,
                        removed,
                    )

            except Exception:
                LOG.exception("Automatic MythTV refresh failed")

    async def getattr(self, inode, ctx=None):
        node = self.node(inode)
        attr = pyfuse3.EntryAttributes()

        if node.is_dir:
            try:
                st = os.stat(self._metadata_stat_path())
            except OSError as exc:
                raise pyfuse3.FUSEError(exc.errno or errno.EIO)

            attr.st_ino = inode
            attr.st_mode = stat.S_IFDIR | 0o555
            attr.st_nlink = 2
            attr.st_uid = os.getuid()
            attr.st_gid = os.getgid()
            attr.st_rdev = 0
            attr.st_size = 0
            attr.st_atime_ns = st.st_atime_ns
            attr.st_mtime_ns = st.st_mtime_ns
            attr.st_ctime_ns = st.st_ctime_ns
            attr.st_blksize = getattr(st, "st_blksize", 4096)
            attr.st_blocks = 0

        else:
            try:
                st = os.stat(node.real_path)
            except OSError as exc:
                raise pyfuse3.FUSEError(exc.errno or errno.EIO)

            attr.st_ino = inode
            attr.st_mode = stat.S_IFREG | 0o444
            attr.st_nlink = 1
            attr.st_uid = os.getuid()
            attr.st_gid = os.getgid()
            attr.st_rdev = st.st_rdev
            attr.st_size = st.st_size
            attr.st_atime_ns = st.st_atime_ns
            attr.st_mtime_ns = st.st_mtime_ns
            attr.st_ctime_ns = st.st_ctime_ns
            attr.st_blksize = getattr(st, "st_blksize", 4096)
            attr.st_blocks = getattr(
                st,
                "st_blocks",
                (st.st_size + 511) // 512,
            )

        attr.generation = 0
        attr.entry_timeout = 1.0
        attr.attr_timeout = 1.0

        return attr

    async def lookup(self, parent_inode, name, ctx=None):
        parent = self.node(parent_inode)

        if not parent.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)

        name = bytes(name)
        inode = parent.children.get(name)

        if inode is None:
            wanted = os.fsdecode(name).casefold()

            for child_name, child_inode in parent.children.items():
                if os.fsdecode(child_name).casefold() == wanted:
                    inode = child_inode
                    break

        if inode is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        return await self.getattr(inode, ctx)

    async def opendir(self, inode, ctx):
        node = self.node(inode)

        if not node.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)

        return inode

    async def readdir(self, fh, start_id, token):
        node = self.node(fh)

        if not node.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)

        entries = sorted(
            node.children.items(),
            key=lambda item: os.fsdecode(item[0]).casefold(),
        )

        index = int(start_id)

        while index < len(entries):
            name, inode = entries[index]
            attr = await self.getattr(inode)

            next_id = index + 1

            if not pyfuse3.readdir_reply(
                token,
                name,
                attr,
                next_id,
            ):
                return

            index += 1

    async def releasedir(self, fh):
        return

    async def open(self, inode, flags, ctx):
        node = self.node(inode)

        if node.is_dir:
            raise pyfuse3.FUSEError(errno.EISDIR)

        if (flags & os.O_ACCMODE) != os.O_RDONLY:
            raise pyfuse3.FUSEError(errno.EROFS)

        safe_flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            safe_flags |= os.O_CLOEXEC

        try:
            fd = os.open(node.real_path, safe_flags)
        except OSError as exc:
            raise pyfuse3.FUSEError(exc.errno or errno.EIO)

        return pyfuse3.FileInfo(fh=fd)

    async def read(self, fh, off, size):
        try:
            return os.pread(fh, size, off)
        except OSError as exc:
            raise pyfuse3.FUSEError(exc.errno or errno.EIO)

    async def release(self, fh):
        try:
            os.close(fh)
        except OSError:
            pass

    async def access(self, inode, mode, ctx):
        node = self.node(inode)

        if mode & os.W_OK:
            raise pyfuse3.FUSEError(errno.EROFS)

        if node.is_dir:
            return True

        if not os.access(node.real_path, os.R_OK):
            raise pyfuse3.FUSEError(errno.EACCES)

        return True

    def _metadata_stat_path(self):
        for path in self.active_storage_dirs:
            if os.path.isdir(path):
                return path

        for path in self.configured_storage_dirs:
            if os.path.isdir(path):
                return path

        return "/"

    async def statfs(self, ctx):
        """
        Report aggregate free/total space across distinct locally accessible
        recording filesystems. Multiple Storage Group directories on the same
        underlying filesystem are counted only once.
        """
        paths = [
            path
            for path in self.active_storage_dirs
            if os.path.isdir(path)
        ]

        if not paths:
            paths = [self._metadata_stat_path()]

        seen_devices = set()
        stats = []

        for path in paths:
            try:
                dev = os.stat(path).st_dev
                if dev in seen_devices:
                    continue
                seen_devices.add(dev)
                stats.append(os.statvfs(path))
            except OSError:
                continue

        if not stats:
            raise pyfuse3.FUSEError(errno.EIO)

        # Use the first filesystem's block size as the reporting unit and
        # convert all other filesystems into that unit before aggregation.
        base = stats[0]
        unit = base.f_frsize or base.f_bsize or 4096

        def blocks_in_unit(st, value):
            size = st.f_frsize or st.f_bsize or unit
            return int((value * size) // unit)

        data = pyfuse3.StatvfsData()
        data.f_bsize = base.f_bsize
        data.f_frsize = unit
        data.f_blocks = sum(blocks_in_unit(st, st.f_blocks) for st in stats)
        data.f_bfree = sum(blocks_in_unit(st, st.f_bfree) for st in stats)
        data.f_bavail = sum(blocks_in_unit(st, st.f_bavail) for st in stats)
        data.f_files = sum(st.f_files for st in stats)
        data.f_ffree = sum(st.f_ffree for st in stats)
        data.f_favail = sum(getattr(st, "f_favail", st.f_ffree) for st in stats)
        data.f_namemax = min(st.f_namemax for st in stats)

        return data

    async def unlink(self, parent_inode, name, ctx):
        raise pyfuse3.FUSEError(errno.EROFS)

    async def mkdir(self, parent_inode, name, mode, ctx):
        raise pyfuse3.FUSEError(errno.EROFS)

    async def rmdir(self, parent_inode, name, ctx):
        raise pyfuse3.FUSEError(errno.EROFS)

    async def rename(
        self,
        parent_inode_old,
        name_old,
        parent_inode_new,
        name_new,
        flags,
        ctx,
    ):
        raise pyfuse3.FUSEError(errno.EROFS)

    async def setattr(self, inode, attr, fields, fh, ctx):
        raise pyfuse3.FUSEError(errno.EROFS)

    async def create(self, parent_inode, name, mode, flags, ctx):
        raise pyfuse3.FUSEError(errno.EROFS)

    async def write(self, fh, off, buf):
        raise pyfuse3.FUSEError(errno.EROFS)



DEFAULT_CONFIG_FILE = "/etc/mythfs.conf"


def load_config(path):
    cfg = configparser.ConfigParser()

    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg.read_file(fh)

    return cfg


def cfg_get(section, name, fallback):
    if section is None:
        return fallback
    return section.get(name, fallback=fallback)


def cfg_getint(section, name, fallback):
    if section is None:
        return fallback
    return section.getint(name, fallback=fallback)


def cfg_getbool(section, name, fallback):
    if section is None:
        return fallback
    return section.getboolean(name, fallback=fallback)


def cfg_list(section, name, fallback):
    if section is None or not section.get(name, fallback="").strip():
        return list(fallback)

    return [
        item.strip()
        for item in section.get(name).split(",")
        if item.strip()
    ]


def parse_args():
    # Pre-parse --config so defaults can come from that file.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help="Configuration file (default: /etc/mythfs.conf)",
    )
    pre_args, _ = pre.parse_known_args()

    config = load_config(pre_args.config)
    section = config["mythfs"] if config.has_section("mythfs") else None

    parser = argparse.ArgumentParser(
        parents=[pre],
        description=(
            "Mount MythTV recordings read-only with Recording Group folders, "
            "media-server naming, movie-year enrichment and automatic refresh."
        ),
    )

    parser.add_argument(
        "mountpoint",
        nargs="?",
        default=cfg_get(section, "mountpoint", "/media/mythfs"),
        help="Mount point (default: /media/mythfs)",
    )

    parser.add_argument(
        "--storage-dir",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "Add a locally accessible recording directory; may be repeated. "
            "Normally MythFS discovers Default Storage Group directories "
            "from MythTV automatically."
        ),
    )

    parser.add_argument(
        "--no-storage-discovery",
        action="store_true",
        default=not cfg_getbool(section, "discover_storage_groups", True),
        help="Do not discover Default Storage Group directories from MythTV",
    )

    parser.add_argument(
        "--refresh",
        type=int,
        default=cfg_getint(section, "refresh", 300),
        metavar="SECONDS",
        help="Automatic refresh interval; 0 disables (default: 300)",
    )

    parser.add_argument(
        "--include-all-groups",
        action=argparse.BooleanOptionalAction,
        default=cfg_getbool(section, "include_all_groups", False),
        help="Include Deleted and LiveTV groups",
    )

    parser.add_argument(
        "--exclude-group",
        action="append",
        default=None,
        metavar="NAME",
        help="Exclude a Recording Group; may be repeated",
    )

    parser.add_argument(
        "--movie-group",
        action="append",
        default=None,
        metavar="NAME",
        help="Recording Group using Movie Title (YEAR) naming; may be repeated",
    )

    parser.add_argument(
        "--tmdb-grabber",
        default=cfg_get(
            section,
            "tmdb_grabber",
            "/usr/share/mythtv/metadata/Movie/tmdb3.py",
        ),
        help="MythTV TMDB metadata grabber",
    )

    parser.add_argument(
        "--cache-file",
        default=cfg_get(
            section,
            "cache_file",
            "/var/cache/mythfs/movie-years.json",
        ),
        help="Persistent TMDB movie-year cache",
    )

    parser.add_argument(
        "--allow-other",
        action=argparse.BooleanOptionalAction,
        default=cfg_getbool(section, "allow_other", True),
        help="Permit other users/services to access the FUSE mount",
    )

    parser.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        default=cfg_getbool(section, "debug", False),
    )

    parser.add_argument(
        "--debug-fuse",
        action=argparse.BooleanOptionalAction,
        default=cfg_getbool(section, "debug_fuse", False),
    )

    args = parser.parse_args()

    # Storage directories: CLI --storage-dir overrides config. The plural
    # storage_dirs option is preferred; legacy storage_dir remains supported.
    if args.storage_dir is None:
        configured = cfg_list(section, "storage_dirs", [])
        if not configured:
            legacy = cfg_get(section, "storage_dir", "").strip()
            configured = [legacy] if legacy else []
        args.storage_dirs = configured
    else:
        args.storage_dirs = args.storage_dir

    # Config lists apply unless explicit CLI --exclude-group/--movie-group
    # arguments were supplied.
    if args.exclude_group is None:
        args.exclude_group = cfg_list(
            section,
            "exclude_groups",
            DEFAULT_EXCLUDED_GROUPS,
        )

    if args.movie_group is None:
        args.movie_group = cfg_list(
            section,
            "movie_groups",
            DEFAULT_MOVIE_GROUPS,
        )


    # Optional explicit MythTV database connection.
    # With no [mythtv] section, preserve the original
    # MythDB() automatic configuration behaviour.
    mythtv = config["mythtv"] if config.has_section("mythtv") else None

    args.db_connection = None

    if mythtv is not None:
        mapping = {
            "database_host": "DBHostName",
            "database_port": "DBPort",
            "database_name": "DBName",
            "database_user": "DBUserName",
            "database_password": "DBPassword",
        }

        required = (
            "database_host",
            "database_name",
            "database_user",
            "database_password",
        )

        missing = [
            key for key in required
            if not mythtv.get(key, fallback="").strip()
        ]

        if missing:
            parser.error(
                "Incomplete [mythtv] configuration: "
                + ", ".join(missing)
            )

        args.db_connection = {}

        for config_key, mythtv_key in mapping.items():
            value = mythtv.get(config_key, fallback="").strip()
            if not value:
                continue
            if config_key == "database_port":
                try:
                    value = int(value)
                except ValueError:
                    parser.error("database_port must be an integer")
                if not 1 <= value <= 65535:
                    parser.error("database_port is out of range")
            args.db_connection[mythtv_key] = value

    args.metadata_cache = cfg_get(
        section, "metadata_cache", ""
    ).strip()

    args.offline_mode = cfg_get(
        section, "offline_mode", "disabled"
    ).strip().lower()

    if args.offline_mode not in ("disabled", "auto", "cache"):
        parser.error(
            "offline_mode must be disabled, auto or cache"
        )

    if args.offline_mode != "disabled" and not args.metadata_cache:
        parser.error(
            "metadata_cache required when offline mode is enabled"
        )

    return args


async def run_filesystem(fs):
    async with trio.open_nursery() as nursery:
        nursery.start_soon(fs.tmdb_resolver_loop)

        if fs.refresh_interval > 0:
            nursery.start_soon(fs.refresh_loop)

        try:
            await pyfuse3.main()
        finally:
            nursery.cancel_scope.cancel()


def main():
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    mountpoint = os.path.abspath(os.path.expanduser(args.mountpoint))
    if not os.path.isdir(mountpoint):
        raise SystemExit(f"Mount point does not exist: {mountpoint}")

    excluded_groups = (
        set()
        if args.include_all_groups
        else set(args.exclude_group)
    )

    movie_groups = set(args.movie_group)

    LOG.info(
        "Excluded Recording Groups: %s",
        ", ".join(sorted(excluded_groups, key=str.casefold))
        if excluded_groups
        else "(none)",
    )

    LOG.info(
        "Movie Recording Groups: %s",
        ", ".join(sorted(movie_groups, key=str.casefold))
        if movie_groups
        else "(none)",
    )

    fs = MythFS(
        storage_dirs=args.storage_dirs,
        discover_storage_groups=not args.no_storage_discovery,
        excluded_groups=excluded_groups,
        movie_groups=movie_groups,
        refresh_interval=args.refresh,
        tmdb_grabber=args.tmdb_grabber,
        cache_file=args.cache_file,
        db_connection=args.db_connection,
        metadata_cache=args.metadata_cache,
        offline_mode=args.offline_mode,
    )

    fuse_options = set(pyfuse3.default_options)
    fuse_options.add("fsname=mythfs")
    fuse_options.add("ro")

    if args.allow_other:
        fuse_options.add("allow_other")

    if args.debug_fuse:
        fuse_options.add("debug")

    LOG.info("Mounting %s", mountpoint)
    LOG.info(
        "Configured recording directories: %s",
        ", ".join(args.storage_dirs) if args.storage_dirs else "(none)",
    )
    LOG.info(
        "MythTV Storage Group discovery: %s",
        "enabled" if not args.no_storage_discovery else "disabled",
    )
    LOG.info(
        "FUSE allow_other: %s",
        "enabled" if args.allow_other else "disabled",
    )

    pyfuse3.init(fs, mountpoint, fuse_options)
    fs.initialized = True

    try:
        trio.run(run_filesystem, fs)
    except BaseException:
        pyfuse3.close(unmount=False)
        raise
    else:
        pyfuse3.close()


if __name__ == "__main__":
    main()
