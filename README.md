# MythFS for MythTV v36

MythFS is a read-only FUSE filesystem for MythTV recordings. It presents the
recordings stored by MythTV with human-readable names and a folder layout that
works well with Jellyfin, Emby and Plex, without renaming or modifying the
actual MythTV recording files.

This implementation targets MythTV v36, Python 3 and FUSE3/pyfuse3.

## Features

- Read-only: MythTV remains the owner of the real recording files.
- MythTV Recording Groups become top-level folders.
- `Deleted` and `LiveTV` are excluded by default.
- TV recordings use media-server-friendly naming:
  `Series/Season 01/Series - S01E01 - Episode.ts`.
- The `Movies` Recording Group uses:
  `Movie Title (YEAR)/Movie Title (YEAR).ts`.
- Movie year comes first from `recordedprogram.airdate`.
- If that year is absent and the recording has a `tmdb3.py_<id>` inetref,
  MythFS uses MythTV's own `tmdb3.py` metadata grabber in the background.
- TMDB-derived years are cached persistently.
- Automatic refresh adds/removes recordings without remounting.
- Multiple MythTV `Default` Storage Group directories are discovered
  automatically.
- Optional local recording paths can be supplied when another backend's
  storage is mounted at a different path.
- FUSE `allow_other` support allows the virtual filesystem to be exported
  through Samba and NFS.

## Example layout

```text
/media/mythfs/
├── Default/
├── Documentaries/
├── Glastonbury/
├── Movies/
│   ├── A Quiet Place (2018)/
│   │   └── A Quiet Place (2018).ts
│   └── Blade II (2002)/
│       └── Blade II (2002).ts
├── Music/
└── The Fast Show/
    └── The Fast Show/
        └── Season 01/
            └── The Fast Show - S01E01 - Episode Title.ts
```

## Requirements

On Ubuntu:

```bash
sudo apt install fuse3 python3-pyfuse3
```

MythTV's Python bindings must also be installed and usable:

```bash
python3 - <<'PY'
import MythTV
print(MythTV.__file__)
print(getattr(MythTV, "__version__", "unknown"))
PY
```

MythFS has been developed and tested with MythTV v36.

## Installation

Install the program:

```bash
sudo install -m 755 mythfs.py /usr/local/bin/mythfs.py
```

Create the production mount and cache directories:

```bash
sudo mkdir -p /media/mythfs /var/cache/mythfs
```

Choose the account that will run MythFS and make those directories writable by
that account. For example, if MythFS runs as the `mythtv` user:

```bash
sudo chown mythtv:mythtv /media/mythfs /var/cache/mythfs
```

Install the example configuration:

```bash
sudo install -m 644 mythfs.conf.example /etc/mythfs.conf
```

Edit `/etc/mythfs.conf` as required.

If `allow_other = yes`, ensure `/etc/fuse.conf` contains:

```text
user_allow_other
```

For example:

```bash
grep -q '^user_allow_other' /etc/fuse.conf \
  || echo user_allow_other | sudo tee -a /etc/fuse.conf
```

## Configuration

The default configuration is:

```ini
[mythfs]
mountpoint = /media/mythfs
discover_storage_groups = yes

refresh = 300

exclude_groups = Deleted, LiveTV
movie_groups = Movies
include_all_groups = no

tmdb_grabber = /usr/share/mythtv/metadata/Movie/tmdb3.py
cache_file = /var/cache/mythfs/movie-years.json

allow_other = yes
debug = no
debug_fuse = no
```

### Multiple recording storage locations

MythFS queries MythTV's `storagegroup` table for every directory in the
`Default` Storage Group.

For each recording it searches in this order:

1. Explicit `storage_dirs` configured in `/etc/mythfs.conf`.
2. `Default` Storage Group directories for the backend named by
   `recorded.hostname`.
3. All other discovered `Default` Storage Group directories.

A candidate path is only accepted when the actual recording file exists on the
machine running MythFS. MythFS therefore does not assume that identical paths
on two MythTV backends refer to the same storage.

If another backend's recordings are mounted locally under a different path,
add the local paths:

```ini
storage_dirs = /mnt/backend1-recordings, /mnt/backend2-recordings
```

The older singular form remains accepted:

```ini
storage_dir = /media/myth/tv
```

Storage Group discovery can be disabled if required:

```ini
discover_storage_groups = no
storage_dirs = /mnt/recordings-a, /mnt/recordings-b
```

Remote-only recordings that are not locally mounted are not streamed through
the MythTV backend protocol; they are simply omitted until their backing file
becomes locally accessible.

## Running manually

```bash
/usr/local/bin/mythfs.py --config /etc/mythfs.conf
```

Useful overrides include:

```bash
/usr/local/bin/mythfs.py --help
/usr/local/bin/mythfs.py --refresh 300
/usr/local/bin/mythfs.py --storage-dir /mnt/extra-recordings
/usr/local/bin/mythfs.py --debug
```

## systemd

Edit `mythfs.service` and set `User=` and `Group=` to the account that should
run MythFS.

Install and enable it:

```bash
sudo install -m 644 mythfs.service /etc/systemd/system/mythfs.service
sudo systemctl daemon-reload
sudo systemctl enable --now mythfs.service
```

Check:

```bash
systemctl status mythfs.service --no-pager
mount | grep ' /media/mythfs '
```

## Samba

With `allow_other = yes`, Samba can export the FUSE filesystem directly.

Example:

```ini
[MythFS]
    comment = MythTV Recordings
    path = /media/mythfs
    browseable = yes
    read only = yes
    guest ok = no
    follow symlinks = yes
    wide links = no
```

After adding it to `/etc/samba/smb.conf`:

```bash
sudo testparm
sudo systemctl reload smbd
```

## NFS

Example `/etc/exports` entry:

```text
/media/mythfs 192.168.1.0/24(ro,sync,no_subtree_check,fsid=30)
```

Apply it with:

```bash
sudo exportfs -ra
```

### Boot ordering with NFS

Do not make `nfs-server.service` depend directly on `mythfs.service` when the
underlying MythTV recording storage is itself involved in NFS/systemd mount
ordering; that can create an ordering cycle.

Instead, use the supplied `mythfs-nfs-export.service.example` as a starting
point. It explicitly removes any export associated with an old FUSE instance,
exports the newly mounted MythFS filesystem, and unexports MythFS before the
FUSE filesystem is stopped.

This is important because restarting MythFS creates a new FUSE filesystem
instance. A simple `exportfs -r` may leave NFS exporting the empty directory
underneath `/media/mythfs` rather than the new FUSE mount.

Existing NFS clients can still receive stale file handles after a MythFS
restart because their file handles belong to the previous FUSE instance. In
that case, unmount and remount the MythFS NFS share on the client. Normal
300-second MythFS refreshes do not recreate the FUSE filesystem and therefore
do not cause this issue.

The example contains a LAN-specific client address. Edit it before installing.

## Movie metadata cache

MythFS stores TMDB-derived years in:

```text
/var/cache/mythfs/movie-years.json
```

The cache is independent of MythTV's own `~/.mythtv/cache/pytmdb3.cache`.

If MythTV's TMDB cache becomes corrupt, stop MythFS before removing that
MythTV-owned cache so no metadata grabber process is using it.

## Refresh behaviour

The default refresh interval is 300 seconds (5 minutes). New recordings appear
and deleted recordings disappear automatically.

Normal MythFS refreshes update the existing FUSE filesystem in place and do
not require SMB or NFS clients to reconnect.

Set:

```ini
refresh = 0
```

to disable automatic refresh.

Movie-year lookups are performed in the background so a slow TMDB request does
not block mounting or periodic refreshes.

## Read-only behaviour

MythFS rejects write, create, delete, rename and truncate operations with
`EROFS`. Samba and NFS exports should also be configured read-only.

## Troubleshooting

### `allow_other` fails

Ensure `/etc/fuse.conf` contains:

```text
user_allow_other
```

### Recording is missing from MythFS

Check:

- it is present in MythTV's `recorded` table;
- its Recording Group is not excluded;
- at least one candidate Storage Group/configured directory contains the
  recording basename locally;
- the MythFS service account can read the real file.

Enable `debug = yes` to log inaccessible recording basenames and backend
hostnames.

### FUSE mount is busy during shutdown

Stop/unexport services using `/media/mythfs` first. The supplied NFS helper
service removes the NFS export before MythFS stops and recreates it against the
new FUSE instance after MythFS starts. The systemd unit also uses a lazy FUSE
unmount as a fallback.

If an NFS client reports `Stale file handle` after MythFS has been restarted,
unmount and remount the share on that client.

## Uninstall

```bash
sudo systemctl disable --now mythfs.service
sudo fusermount3 -uz /media/mythfs
sudo rm -f /etc/systemd/system/mythfs.service
sudo rm -f /usr/local/bin/mythfs.py
sudo systemctl daemon-reload
```

Remove `/etc/mythfs.conf`, the cache directory and SMB/NFS configuration only
if they are no longer required.


## Deployment modes and offline operation

MythFS supports two deployment methods:

- **Standalone Linux:** run `mythfs.py` directly, optionally as a systemd
  service. Docker is not required.
- **Docker:** run the same Python application inside a Linux container,
  with its dependencies and FUSE mount managed through Docker Compose.

The core MythFS program is the same in both configurations. The Docker
`entrypoint.sh` is used only by the container and is not required when
running MythFS as a standalone Linux application.

### Standalone Linux compatibility

Existing installations using `/etc/mythfs.conf` continue to work.

The `[mythtv]` database section and offline recording catalogue are
optional. Without explicit database settings, MythFS uses the normal
MythTV Python bindings and their configured database connection.

The default offline mode is `disabled`, retaining database-based operation.

Run directly with:

```bash
python3 mythfs.py --config /etc/mythfs.conf
```

Or use the existing `mythfs.service` example described earlier.

On the standalone host, the recording directories and FUSE mountpoint
must be accessible locally. The MythTV Python bindings, `pyfuse3`,
`trio`, FUSE3 and relevant metadata grabber dependencies must be installed.

### Connecting to a remote MythTV database

An optional `[mythtv]` section allows MythFS to connect to a database
on another server instead of relying on locally configured MythTV
database credentials.

Example:

```ini
[mythtv]
database_host = 192.168.1.10
database_port = 3306
database_name = mythconverg
database_user = mythtv
database_password = REPLACE_WITH_DATABASE_PASSWORD
```

Omit this section when the normal MythTV Python bindings already know
how to connect.

Store real credentials in a private configuration file, not in Git.
Restrict access to that configuration file and make sure the database
server permits connections from the MythFS machine.

### Persistent recording catalogue

MythFS can save the last successfully generated virtual filesystem
catalogue as a JSON file. This allows it to start when the MythTV
database is temporarily unavailable.

Configure the following options in `[mythfs]`:

```ini
metadata_cache = /var/cache/mythfs/recordings.json
offline_mode = auto
```

Available modes:

| Mode | Behaviour |
| --- | --- |
| `disabled` | Use the MythTV database; do not use the offline catalogue. |
| `auto` | Use the database normally; fall back to the saved catalogue if the database cannot be queried. |
| `cache` | Load the saved catalogue without querying the database. Useful for offline testing. |

With `auto`, a successful online refresh updates the recording
catalogue. Cached virtual paths refer to the original recording files,
which **must still exist and be readable locally**.

The catalogue contains filenames and local backing-file paths, not
recording video content or a copy of the MythTV database.

A valid, nonempty catalogue must first be generated while the database
is available. If the database is unavailable and no usable catalogue
exists, MythFS cannot reconstruct the filesystem.

The offline catalogue is persisted separately from the TMDB movie-year
cache:

```text
/var/cache/mythfs/recordings.json
/var/cache/mythfs/movie-years.json
```

Both files should be stored on persistent storage, especially in Docker.
Do not commit these files to Git.

**Limitations:** A saved catalogue represents the last successful online
snapshot. New recordings, changed recording metadata and deletions may
not be reflected until database connectivity returns. Files that are
no longer accessible are omitted from offline presentation.

### Docker deployment

The supplied `Dockerfile` builds a Linux image with MythFS, FUSE3,
MythTV Python bindings, and the MythTV metadata grabber.

For a Docker deployment where the host media application (such as
Jellyfin) reads the FUSE mount directly, bind-mount the **parent
directory**, not the FUSE mountpoint itself.

Example Docker Compose service:

```yaml
services:
  mythfs:
    build: .
    container_name: mythfs
    restart: unless-stopped
    cap_add:
      - SYS_ADMIN
    devices:
      - /dev/fuse:/dev/fuse:rwm
    security_opt:
      - apparmor:unconfined
    volumes:
      - type: bind
        source: /etc/mythfs.conf
        target: /etc/mythfs.conf
        read_only: true
      - type: bind
        source: /srv/mythtv-recordings
        target: /recordings
        read_only: true
      - type: bind
        source: /srv/mythfs-cache
        target: /var/cache/mythfs
      - type: bind
        source: /srv/jellyfin
        target: /fuse-parent
        bind:
          propagation: rshared
```

Example container configuration:

```ini
[mythfs]
mountpoint = /fuse-parent/mythfs
storage_dirs = /recordings
discover_storage_groups = no
refresh = 300
exclude_groups = Deleted, LiveTV
movie_groups = Movies
include_all_groups = no
tmdb_grabber = /usr/share/mythtv/metadata/Movie/tmdb3.py
cache_file = /var/cache/mythfs/movie-years.json
metadata_cache = /var/cache/mythfs/recordings.json
offline_mode = auto
allow_other = yes
```

Create `/srv/jellyfin/mythfs` on the host before starting the
container. It must be an ordinary directory, not another mounted
filesystem.

Build and start the service from the repository directory:

```bash
docker compose build
docker compose up -d
```

With the example mappings, the host filesystem appears under:

```text
/srv/jellyfin/mythfs
```

The parent bind mount requires compatible shared mount propagation on
the host, a usable `/dev/fuse`, and permission to mount FUSE inside
the container. Container runtime and security settings vary between
Linux hosts.

The sample `SYS_ADMIN`, device and AppArmor permissions should be
evaluated for the security requirements of the deployment.

### Docker FUSE restart recovery

An unexpected MythFS process crash may leave a disconnected FUSE mount
visible on the host.

Binding the FUSE mountpoint itself can prevent Docker from recreating
the container. Binding the parent directory avoids this particular
startup problem.

The Docker `entrypoint.sh` reads `mountpoint` from the MythFS
configuration and checks for a previous mount before accessing the
directory. If the mount is identified as the MythFS FUSE filesystem,
it attempts a normal unmount before launching `mythfs.py`.

It refuses to unmount an unexpected filesystem and does not use
forced or lazy unmounts.

Together with `restart: unless-stopped`, this supports recovery from
unexpected application process termination.

This recovery has been tested with Docker and a host-visible FUSE
mount. It does **not** guarantee that the host's storage volumes will
be mounted before Docker starts, or that dependent applications will
wait for MythFS following a complete host reboot.

Verify mount availability and application startup ordering separately
for the host operating system.

### Production checklist

Before pointing a media server at a Docker-hosted MythFS mount:

1. Verify the real recording directory is available.
2. Populate and validate the persistent recording catalogue.
3. Verify the FUSE filesystem can be read by the media server's user.
4. Confirm that no competing NFS or other mount occupies the path.
5. Confirm Docker's restart policy and the entrypoint recovery behaviour.
6. Verify boot ordering and recovery after a host restart.

Do not allow a media server to scan an empty directory while a FUSE
mount is unavailable; such scans can remove or invalidate library items.

## License

MIT. See `LICENSE`.
