# coding=utf-8
"""
OctoPrint System Image Backup

Creates a full, bootable image of the Raspberry Pi's SD card / boot drive with RonR's
image-utils (image-backup). OctoPrint is stopped during the backup, so the actual work is
done by a separate systemd service (octopi-image-backup.service) that the plugin starts via
a narrow sudoers rule. See helper/ for the root-side pieces and install.sh to set them up.
"""
from __future__ import absolute_import, unicode_literals

import getpass
import hmac
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions

SERVICE_UNIT = "octopi-image-backup.service"
HELPER_PATH = "/usr/local/sbin/octopi-image-backup"
UNIT_PATH = "/etc/systemd/system/" + SERVICE_UNIT
ENV_FILE = "/etc/default/octopi-image-backup"
STATE_DIR = "/var/lib/octopi-image-backup"
STATUS_FILE = os.path.join(STATE_DIR, "status")
LOG_FILE = os.path.join(STATE_DIR, "last.log")
DEST_INFO_FILE = os.path.join(STATE_DIR, "dest-info")
CRED_FILE = "/etc/octopi-image-backup/smb-credentials"
EXPECTED_HELPER_VERSION = 6

SUDO = "/usr/bin/sudo"
SYS_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SYSTEMCTL = shutil.which("systemctl", path=SYS_PATH) or "/usr/bin/systemctl"
IMAGE_BACKUP_CANDIDATES = (
    "/usr/local/sbin/image-backup",
    "/usr/local/bin/image-backup",
    "/usr/sbin/image-backup",
    "/usr/bin/image-backup",
)

AT_COMMAND = "IMAGEBACKUP"

BUSY_STATES = {
    "PRINTING",
    "PAUSING",
    "PAUSED",
    "RESUMING",
    "CANCELLING",
    "STARTING",
    "FINISHING",
    "TRANSFERING_FILE",
}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEST_RE = re.compile(r"^/(mnt|media)(/[A-Za-z0-9._+@-]+)+$")
NET_MNT = "/mnt/octopi-image-backup"
SMB_RE = re.compile(r"^//[A-Za-z0-9._-]+(/[A-Za-z0-9 ._$@+-]+)+$")
NFS_RE = re.compile(r"^[A-Za-z0-9._-]+:/[A-Za-z0-9._@+/-]*$")
SUBDIR_RE = re.compile(r"^[A-Za-z0-9._@+-]+(/[A-Za-z0-9._@+-]+)*$")
OPTIONS_RE = re.compile(r"^[A-Za-z0-9=._:-]+(,[A-Za-z0-9=._:-]+)*$")
FORBIDDEN_OPTIONS = {"credentials", "cred", "user", "username", "pass", "password", "password2", "guest"}
SMB_USER_RE = re.compile(r"^([A-Za-z0-9._-]{1,64}\\)?[A-Za-z0-9._@ -]{1,128}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9@._:-]+\.service$")
PROTECTED_RE = re.compile(
    r"^(ssh|sshd|dbus|systemd-.*|networking|NetworkManager|dhcpcd|wpa_supplicant|avahi-daemon"
    r"|rpcbind|nfs-.*|smbd|nmbd|autofs|getty@.*|serial-getty@.*|user@.*|octopi-image-backup)\.service$"
)
SUGGEST_RE = re.compile(
    r"(webcam|camera-streamer|mjpg|ustreamer|crowsnest|go2rtc|mediamtx|octoprint-.*cam)", re.I
)


class BackupError(Exception):
    def __init__(self, message, code=409):
        Exception.__init__(self, message)
        self.message = message
        self.code = code


def _read_kv(path):
    """Parse a KEY=VALUE file (the helper's status file or /etc/default file)."""
    result = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    value = value[1:-1]
                result[key.strip()] = value
    except OSError:
        return None
    return result


def _tail(path, max_bytes=16384):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
        if size > max_bytes:
            data = data.split("\n", 1)[-1]
        return data
    except OSError:
        return ""


def _to_int(value, default, lo, hi):
    try:
        value = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _normalize_share(dest_type, share):
    share = str(share or "").strip()
    if dest_type == "smb":
        # accept //server/share, \\server\share and smb://server/share
        share = share.replace("\\", "/")
        if share.lower().startswith("smb:"):
            share = share[4:]
        share = share.strip("/")
        share = "//" + re.sub(r"/+", "/", share) if share else ""
    elif dest_type == "nfs":
        # accept server:/path and nfs://server/path
        if share.lower().startswith("nfs://"):
            host, _, path = share[6:].partition("/")
            share = host + ":/" + path
        if ":" in share:
            host, _, path = share.partition(":")
            share = host + ":/" + re.sub(r"/+", "/", path).strip("/")
            if not share.endswith(":/"):
                share = share.rstrip("/")
    return share


def _normalize_subdir(subdir):
    subdir = str(subdir or "").strip().replace("\\", "/")
    subdir = subdir[1:] if subdir.startswith("/") else subdir
    while subdir.endswith("/"):
        subdir = subdir[:-1]
    return subdir


def _share_problem(dest_type, share, subdir, options):
    """Same checks as the helper's validate_destination(); returns a message or None."""
    if dest_type == "smb":
        if not share:
            return "Enter the share, e.g. //nas.local/backup."
        if not SMB_RE.match(share):
            return "Invalid SMB share - expected //server/share (letters, digits, space, . _ $ @ + -)."
    elif dest_type == "nfs":
        if not share:
            return "Enter the export, e.g. nas.local:/volume1/backup."
        if not NFS_RE.match(share):
            return "Invalid NFS export - expected server:/path."
    rest = "/" + share.split("/", 1)[-1] + "/"
    if "/../" in rest or "/./" in rest:
        return "The share path must not contain . or .. parts."
    if subdir:
        if not SUBDIR_RE.match(subdir) or "/../" in "/" + subdir + "/" or "/./" in "/" + subdir + "/":
            return "Invalid folder on the share (letters, digits, . _ - + @ and /, no spaces)."
    if options:
        if not OPTIONS_RE.match(options):
            return "Invalid mount options (comma separated, no spaces)."
        for opt in options.split(","):
            if opt.split("=", 1)[0] in FORBIDDEN_OPTIONS:
                return "Mount option '{}' is set by the plugin - remove it.".format(opt.split("=", 1)[0])
    return None


def _split_services(value):
    names = []
    for part in re.split(r"[\s,]+", str(value or "").strip()):
        if not part:
            continue
        if not part.endswith(".service"):
            part += ".service"
        if part not in names:
            names.append(part)
    return names


class SystemImageBackupPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.BlueprintPlugin,
):
    def __init__(self):
        self._port = 5000
        self._start_lock = threading.Lock()
        self._install_lock = threading.Lock()
        self._install = {"running": False, "rc": None, "output": "", "finished": None}
        self._cache = {}

    # ------------------------------------------------------------------ startup

    def on_startup(self, host, port):
        self._port = port or 5000

    def on_after_startup(self):
        if not self._settings.get(["octodash_token"]):
            self._settings.set(["octodash_token"], secrets.token_urlsafe(18))
            self._settings.save()
        helper = self._helper_info()
        if helper["ready"]:
            self._logger.info("Backup helper ready (version %s)", helper["version"])
        else:
            self._logger.info("Backup helper needs setup: %s", "; ".join(helper["problems"]))

    # ----------------------------------------------------------------- settings

    def get_settings_defaults(self):
        return {
            "destination": "/mnt/backup",
            "mode": "update",
            "image_name": "octopi-backup",
            "keep": 3,
            "extra_mb": 1024,
            "stop_services": "",
            "show_on_screen": False,
            "screen_vt": 8,
            "allow_atcommand": True,
            "octodash_token": "",
            "dest_type": "local",
            "net_share": "",
            "net_subdir": "",
            "net_options": "",
            "smb_user": "",
            "smb_user_saved": None,
        }

    def get_settings_restricted_paths(self):
        return {"admin": [["octodash_token"], ["smb_user"], ["smb_user_saved"]]}

    def on_settings_save(self, data):
        if "destination" in data:
            dest = str(data["destination"] or "").strip()
            while len(dest) > 1 and dest.endswith("/"):
                dest = dest[:-1]
            data["destination"] = dest
        if "mode" in data and data["mode"] not in ("update", "new"):
            del data["mode"]
        if "image_name" in data:
            name = str(data["image_name"] or "").strip()
            if name.endswith(".img"):
                name = name[:-4]
            data["image_name"] = name
        if "keep" in data:
            data["keep"] = _to_int(data["keep"], 3, 0, 999)
        if "extra_mb" in data:
            data["extra_mb"] = _to_int(data["extra_mb"], 1024, 0, 1048576)
        if "screen_vt" in data:
            data["screen_vt"] = _to_int(data["screen_vt"], 8, 2, 63)
        if "stop_services" in data:
            data["stop_services"] = " ".join(_split_services(data["stop_services"]))
        for key in ("show_on_screen", "allow_atcommand"):
            if key in data:
                data[key] = bool(data[key])
        if "dest_type" in data and data["dest_type"] not in ("local", "smb", "nfs"):
            del data["dest_type"]
        if "net_share" in data:
            dest_type = data.get("dest_type") or self._settings.get(["dest_type"])
            data["net_share"] = _normalize_share(dest_type, data["net_share"])
        if "net_subdir" in data:
            data["net_subdir"] = _normalize_subdir(data["net_subdir"])
        if "net_options" in data:
            data["net_options"] = re.sub(r"\s+", "", str(data["net_options"] or "")).strip(",")
        if "smb_user" in data:
            data["smb_user"] = str(data["smb_user"] or "").strip()
        data.pop("octodash_token", None)  # only changed through the API command
        data.pop("smb_user_saved", None)  # only set when the password is stored
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

    # ---------------------------------------------------------------- templates

    def get_template_configs(self):
        return [{"type": "tab", "name": "Image Backup", "custom_bindings": True}]

    def is_template_autoescaped(self):
        return True

    def get_assets(self):
        return {
            "js": ["js/systemimagebackup.js"],
            "css": ["css/systemimagebackup.css"],
        }

    # --------------------------------------------------------------- simple api

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return {
            "start": [],
            "install_helper": [],
            "regenerate_token": [],
            "set_credentials": [],
            "test_destination": [],
        }

    def on_api_get(self, request):
        if not Permissions.ADMIN.can():
            return flask.abort(403)
        return flask.jsonify(self._full_status())

    def on_api_command(self, command, data):
        if not Permissions.ADMIN.can():
            return flask.abort(403)
        try:
            if command == "start":
                message = self._start_backup(fresh=bool(data.get("fresh")), source="web interface")
                return flask.jsonify(ok=True, message=message)
            if command == "install_helper":
                self._start_install()
                return flask.jsonify(ok=True, message="Installing the helper...")
            if command == "regenerate_token":
                self._settings.set(["octodash_token"], secrets.token_urlsafe(18))
                self._settings.save()
                return flask.jsonify(ok=True)
            if command == "set_credentials":
                self._set_credentials(str(data.get("user") or "").strip(), str(data.get("password") or ""))
                return flask.jsonify(ok=True)
            if command == "test_destination":
                return flask.jsonify(self._test_destination())
        except BackupError as e:
            return flask.make_response(flask.jsonify(ok=False, error=e.message), e.code)
        return flask.abort(400)

    # ------------------------------------------------------- OctoDash endpoints
    #
    # OctoDash authenticates with an API key and opens [!WEB] pages without an OctoPrint
    # session, so these endpoints use their own token (shown in the plugin tab).

    def is_blueprint_protected(self):
        return False  # checked per endpoint: token

    def is_blueprint_csrf_protected(self):
        return True

    def _check_token(self):
        expected = self._settings.get(["octodash_token"]) or ""
        given = flask.request.values.get("token", "")
        if not expected or not hmac.compare_digest(str(given), str(expected)):
            flask.abort(403)

    @octoprint.plugin.BlueprintPlugin.route("/octodash", methods=["GET"])
    def octodash_page(self):
        self._check_token()
        path = os.path.join(self._basefolder, "static", "octodash.html")
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        response = flask.make_response(html)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["Cache-Control"] = "no-store"
        # OctoPrint adds "X-Frame-Options: sameorigin" to every response unless the header is
        # already set. OctoDash shows [!WEB] pages in an iframe of its own (file://) origin,
        # so this token-protected page opts out with a value browsers ignore.
        response.headers["X-Frame-Options"] = "ALLOWALL"
        return response

    @octoprint.plugin.BlueprintPlugin.route("/octodash/status", methods=["GET"])
    def octodash_status(self):
        self._check_token()
        status = self._full_status(for_octodash=True)
        response = flask.jsonify(status)
        response.headers["Cache-Control"] = "no-store"
        return response

    @octoprint.plugin.BlueprintPlugin.route("/octodash/start", methods=["POST"])
    @octoprint.plugin.BlueprintPlugin.csrf_exempt()
    def octodash_start(self):
        self._check_token()
        try:
            message = self._start_backup(fresh=False, source="OctoDash")
            return flask.jsonify(ok=True, message=message)
        except BackupError as e:
            return flask.make_response(flask.jsonify(ok=False, error=e.message), e.code)

    # ------------------------------------------------------------- @IMAGEBACKUP

    def on_atcommand_queuing(self, comm, phase, command, parameters, tags=None, *args, **kwargs):
        if command.upper() != AT_COMMAND:
            return
        if not self._settings.get_boolean(["allow_atcommand"]):
            self._logger.info("@%s received but disabled in the settings", AT_COMMAND)
            return

        def run():
            time.sleep(1)  # let the command queue settle
            try:
                self._start_backup(fresh=False, source="@" + AT_COMMAND)
            except BackupError as e:
                self._logger.warning("@%s: backup not started: %s", AT_COMMAND, e.message)
                self._plugin_manager.send_plugin_message(
                    self._identifier, {"type": "error", "message": e.message}
                )

        threading.Thread(target=run, name="imagebackup-atcommand", daemon=True).start()

    # ------------------------------------------------------------------- status

    def _job_file(self):
        return os.path.join(self.get_plugin_data_folder(), "job.conf")

    def _cached(self, key, ttl, fn):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (now, value)
        return value

    def _run(self, args, timeout=15):
        try:
            p = subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                universal_newlines=True,
            )
            return p.returncode, p.stdout
        except (OSError, subprocess.SubprocessError) as e:
            return -1, str(e)

    def _sudo_rule_ok(self):
        for args in (
            [SYSTEMCTL, "start", "--no-block", SERVICE_UNIT],
            [HELPER_PATH, "test-destination"],
            [HELPER_PATH, "set-credentials"],
        ):
            rc, _ = self._run([SUDO, "-n", "-l"] + args, timeout=10)
            if rc != 0:
                return False
        return True

    def _passwordless_sudo(self):
        rc, _ = self._run([SUDO, "-n", "true"], timeout=10)
        return rc == 0

    def _service_state(self):
        rc, out = self._run([SYSTEMCTL, "show", "-p", "ActiveState", "--value", SERVICE_UNIT])
        return out.strip() if rc == 0 else "unknown"

    def _service_running(self):
        return self._service_state() in ("activating", "active", "deactivating", "reloading")

    def _image_backup_path(self):
        for path in IMAGE_BACKUP_CANDIDATES:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return shutil.which("image-backup", path=SYS_PATH)

    def _helper_version(self):
        try:
            with open(HELPER_PATH, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r"^HELPER_VERSION=(\d+)", line)
                    if m:
                        return int(m.group(1))
        except OSError:
            pass
        return None

    def _install_command(self, with_image_utils):
        parts = [
            "sudo",
            "bash",
            os.path.join(self._basefolder, "helper", "install.sh"),
            "--user",
            getpass.getuser(),
            "--job-file",
            self._job_file(),
        ]
        if with_image_utils:
            parts.append("--with-image-utils")
        return " ".join(shlex.quote(p) for p in parts)

    def _helper_info(self, force=False):
        if force:
            self._cache.pop("sudo_rule", None)
            self._cache.pop("sudo_nopasswd", None)
        info = {
            "installed": os.path.isfile(HELPER_PATH) and os.path.isfile(UNIT_PATH),
            "version": self._helper_version(),
            "expected_version": EXPECTED_HELPER_VERSION,
            "image_backup": self._image_backup_path(),
            "missing_tools": [],
            "sudo_rule": False,
            "problems": [],
        }
        for tool in ("rsync", "sgdisk"):
            if not shutil.which(tool, path=SYS_PATH):
                info["missing_tools"].append("gdisk" if tool == "sgdisk" else tool)

        env = _read_kv(ENV_FILE) or {}
        job_ok = os.path.realpath(env.get("JOB_FILE", "")) == os.path.realpath(self._job_file())

        if not info["installed"]:
            info["problems"].append("The backup helper and its system service are not installed yet.")
        else:
            if info["version"] != EXPECTED_HELPER_VERSION:
                info["problems"].append(
                    "The installed helper (version {}) does not match this plugin (version {}).".format(
                        info["version"], EXPECTED_HELPER_VERSION
                    )
                )
            if not job_ok:
                info["problems"].append("The helper is set up for a different OctoPrint data folder.")
            info["sudo_rule"] = self._cached("sudo_rule", 300, self._sudo_rule_ok)
            if not info["sudo_rule"]:
                info["problems"].append("OctoPrint is not allowed to run the backup helper (sudoers rules missing or outdated).")
        if not info["image_backup"]:
            info["problems"].append("image-backup (image-utils) is not installed.")
        if info["missing_tools"]:
            info["problems"].append("Missing tools: " + ", ".join(info["missing_tools"]) + ".")

        info["ready"] = not info["problems"]
        info["install_command"] = self._install_command(with_image_utils=not info["image_backup"])
        info["can_install_from_ui"] = self._cached("sudo_nopasswd", 300, self._passwordless_sudo)
        return info

    def _target(self, fresh=False):
        """Where the next backup goes and what it will do (as far as the OctoPrint user can tell)."""
        if (self._settings.get(["dest_type"]) or "local") != "local":
            return self._network_target(fresh)
        s = self._settings
        dest = s.get(["destination"]) or ""
        mode = s.get(["mode"])
        name = s.get(["image_name"]) or "octopi-backup"
        result = {
            "type": "local",
            "network": False,
            "path": dest,
            "ok": False,
            "message": "",
            "free": None,
            "needed": None,
            "image": None,
            "image_exists": False,
            "image_size": None,
            "image_mtime": None,
            "creates": True,
        }
        if not dest:
            result["message"] = "No destination folder set."
            return result
        if not DEST_RE.match(dest) or "/../" in dest + "/" or "/./" in dest + "/":
            result["message"] = (
                "Must be a folder under /mnt or /media (letters, digits, . _ - + @ only, no spaces)."
            )
            return result
        if dest == NET_MNT or dest.startswith(NET_MNT + "/"):
            result["message"] = "{} is used for network shares - choose another folder.".format(NET_MNT)
            return result
        if not NAME_RE.match(name):
            result["message"] = "Invalid image name (letters, digits, . _ - only)."
            return result
        real = os.path.realpath(dest)
        if not DEST_RE.match(real):
            result["message"] = "{} points to {}, which is outside /mnt and /media.".format(dest, real)
            return result
        if not os.path.isdir(real):
            result["message"] = "Folder does not exist. Is the USB drive or network share mounted?"
            return result
        try:
            if os.stat(real).st_dev == os.stat("/").st_dev:
                result["message"] = (
                    "This folder is on the SD card itself. Mount a USB drive or network share here first."
                )
                return result
            usage = shutil.disk_usage(real)
            result["free"] = usage.free
        except OSError as e:
            result["message"] = "Cannot access the folder: {}".format(e.strerror or e)
            return result

        if mode == "update":
            image = os.path.join(real, name + ".img")
            result["image"] = image
            if os.path.isfile(image):
                result["image_exists"] = True
                try:
                    st = os.stat(image)
                    result["image_size"] = st.st_size
                    result["image_mtime"] = int(st.st_mtime)
                except OSError:
                    pass
            result["creates"] = fresh or not result["image_exists"]
        else:
            result["image"] = os.path.join(real, name + "-YYYYMMDD-HHMMSS.img")

        if result["creates"]:
            used = shutil.disk_usage("/").used
            result["needed"] = int(used * 1.2) + 768 * 1024 * 1024
            if result["free"] is not None and result["free"] < result["needed"]:
                result["message"] = "Not enough free space: about {:.1f} GB needed, {:.1f} GB free.".format(
                    result["needed"] / 1e9, result["free"] / 1e9
                )
                return result
        result["ok"] = True
        return result

    def _network_location(self):
        s = self._settings
        dest_type = s.get(["dest_type"])
        share = _normalize_share(dest_type, s.get(["net_share"]))
        subdir = _normalize_subdir(s.get(["net_subdir"]))
        return share + ("/" + subdir if subdir else "")

    def _credentials_state(self):
        """(needs_password, message) for the SMB login."""
        user = (self._settings.get(["smb_user"]) or "").strip()
        if not user:
            return False, None
        if not SMB_USER_RE.match(user):
            return True, "Invalid username."
        saved = self._settings.get(["smb_user_saved"])
        if saved != user or not os.path.exists(CRED_FILE):
            return True, "Enter and save the password for {}.".format(user)
        return False, None

    def _network_target(self, fresh=False):
        s = self._settings
        dest_type = s.get(["dest_type"])
        mode = s.get(["mode"])
        name = s.get(["image_name"]) or "octopi-backup"
        share = _normalize_share(dest_type, s.get(["net_share"]))
        subdir = _normalize_subdir(s.get(["net_subdir"]))
        location = self._network_location()
        result = {
            "type": dest_type,
            "network": True,
            "path": location,
            "ok": False,
            "message": "",
            "free": None,
            "needed": None,
            "image": None,
            "image_exists": None,
            "image_size": None,
            "image_mtime": None,
            "creates": None,
            "checked_at": None,
        }
        problem = _share_problem(dest_type, share, subdir, s.get(["net_options"]) or "")
        if not problem and not NAME_RE.match(name):
            problem = "Invalid image name (letters, digits, . _ - only)."
        if not problem and dest_type == "smb":
            _, problem = self._credentials_state()
        if problem:
            result["message"] = problem
            return result

        result["image"] = location + "/" + (name + ".img" if mode == "update" else name + "-YYYYMMDD-HHMMSS.img")
        info = _read_kv(DEST_INFO_FILE) or {}
        if (
            info.get("dest_type") == dest_type
            and info.get("location") == location
            and info.get("name") == name
            and info.get("mode") == mode
        ):
            def num(key):
                try:
                    return int(info.get(key) or "") or None
                except ValueError:
                    return None

            result["free"] = num("free")
            result["checked_at"] = num("checked_at")
            result["image_exists"] = info.get("image_exists") == "1"
            result["image_size"] = num("image_size")
            result["image_mtime"] = num("image_mtime")
            if mode == "update":
                result["creates"] = bool(fresh) or not result["image_exists"]
            else:
                result["creates"] = True
        elif mode == "new":
            result["creates"] = True
        result["ok"] = True
        return result

    def _last_backup(self, running):
        status = _read_kv(STATUS_FILE)
        if not status:
            return None
        for key in ("started", "finished", "duration", "image_size", "created"):
            try:
                status[key] = int(status.get(key) or 0) or None
            except ValueError:
                status[key] = None
        if status.get("state") == "running" and not running:
            status["state"] = "interrupted"
            status["message"] = "The backup did not finish (the Pi was probably restarted or lost power)."
        return status

    def _printer_busy(self):
        try:
            return self._printer.get_state_id() in BUSY_STATES
        except Exception:
            return False

    def _detected_services(self):
        def detect():
            rc, out = self._run(
                [SYSTEMCTL, "list-units", "--type=service", "--state=active", "--no-legend", "--plain", "--no-pager"]
            )
            if rc != 0:
                return []
            names = [line.split()[0] for line in out.splitlines() if line.strip()]
            return sorted(n for n in names if SUGGEST_RE.search(n) and not PROTECTED_RE.match(n))

        return self._cached("services", 60, detect)

    def _full_status(self, for_octodash=False):
        running = self._service_running()
        last = self._last_backup(running)
        target = self._target()
        busy = self._printer_busy()
        status = {
            "running": running and (last is None or last.get("state") == "running"),
            "last": last,
            "target": target,
            "printer_busy": busy,
            "mode": self._settings.get(["mode"]),
        }
        if for_octodash:
            helper = self._helper_info()
            status["ready"] = helper["ready"]
            status["problems"] = helper["problems"]
            return status

        helper = self._helper_info()
        with self._install_lock:
            install = dict(self._install)
        status.update(
            {
                "helper": helper,
                "install": install,
                "log": _tail(LOG_FILE),
                "detected_services": self._detected_services(),
                "settings": {
                    key: self._settings.get([key])
                    for key in (
                        "destination",
                        "mode",
                        "image_name",
                        "keep",
                        "extra_mb",
                        "stop_services",
                        "show_on_screen",
                        "screen_vt",
                        "allow_atcommand",
                        "dest_type",
                        "net_share",
                        "net_subdir",
                        "net_options",
                        "smb_user",
                    )
                },
                "smb_password_saved": bool(
                    self._settings.get(["smb_user"])
                    and self._settings.get(["smb_user_saved"]) == self._settings.get(["smb_user"])
                    and os.path.exists(CRED_FILE)
                ),
                "octodash": {
                    "web_action": "[!WEB]http://localhost:{}/plugin/{}/octodash?token={}".format(
                        self._port, self._identifier, self._settings.get(["octodash_token"]) or ""
                    ),
                    "at_command": "@" + AT_COMMAND,
                },
            }
        )
        return status

    # ------------------------------------------------------------- start backup

    def _start_backup(self, fresh, source):
        with self._start_lock:
            if self._service_running():
                raise BackupError("A backup is already running.")
            if self._printer_busy():
                raise BackupError("The printer is busy. Start the backup when the print has finished.")
            helper = self._helper_info(force=True)
            if not helper["ready"]:
                raise BackupError("Setup is not complete: " + " ".join(helper["problems"]))
            target = self._target(fresh=fresh)
            if not target["ok"]:
                raise BackupError("Destination: " + target["message"])

            self._write_job(fresh, source)

            rc, out = self._run([SUDO, "-n", SYSTEMCTL, "start", "--no-block", SERVICE_UNIT], timeout=20)
            if rc != 0:
                raise BackupError("Could not start the backup service: {}".format(out.strip() or rc), 500)

        self._logger.info("Image backup started from %s (fresh=%s)", source, fresh)
        return "Backup started. OctoPrint stops in a few seconds and comes back when the backup is done."

    def _write_job(self, fresh, source):
        services = _split_services(self._settings.get(["stop_services"]))
        for unit in services:
            if not SERVICE_RE.match(unit):
                raise BackupError("Invalid service name: {}".format(unit))
            if PROTECTED_RE.match(unit):
                raise BackupError("{} must not be stopped (network, SSH or system service).".format(unit))

        s = self._settings
        dest_type = s.get(["dest_type"]) or "local"
        job = [
            ("DEST_TYPE", dest_type),
            ("DEST_DIR", s.get(["destination"]) or ""),
            ("NET_SHARE", _normalize_share(dest_type, s.get(["net_share"])) if dest_type != "local" else ""),
            ("NET_SUBDIR", _normalize_subdir(s.get(["net_subdir"])) if dest_type != "local" else ""),
            ("NET_OPTIONS", (s.get(["net_options"]) or "") if dest_type != "local" else ""),
            ("SMB_AUTH", "credentials" if dest_type == "smb" and (s.get(["smb_user"]) or "").strip() else "guest"),
            ("MODE", s.get(["mode"])),
            ("NAME", s.get(["image_name"])),
            ("KEEP", _to_int(s.get(["keep"]), 3, 0, 999)),
            ("EXTRA_MB", _to_int(s.get(["extra_mb"]), 1024, 0, 1048576)),
            ("FRESH", 1 if fresh else 0),
            ("STOP_SERVICES", " ".join(services)),
            ("SCREEN", 1 if s.get_boolean(["show_on_screen"]) else 0),
            ("SCREEN_VT", _to_int(s.get(["screen_vt"]), 8, 2, 63)),
            ("OCTOPRINT_PORT", self._port),
            ("REQUESTED_BY", source),
            ("REQUESTED_AT", int(time.time())),
        ]
        path = self._job_file()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("# Written by the System Image Backup plugin - settings of the next backup\n")
            for key, value in job:
                f.write("{}={}\n".format(key, str(value).replace("\n", " ").replace("\r", " ")))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)

    # ------------------------------------------------------ network shares

    def _helper_command(self, command, stdin=None, timeout=30):
        try:
            p = subprocess.run(
                [SUDO, "-n", HELPER_PATH, command],
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                universal_newlines=True,
            )
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "The helper did not answer in time."
        except OSError as e:
            return -1, "", str(e)

    def _set_credentials(self, user, password):
        if self._service_running():
            raise BackupError("A backup is running. Try again when it has finished.")
        if user:
            if not SMB_USER_RE.match(user):
                raise BackupError("Invalid username (letters, digits, space and . _ @ - only, optionally DOMAIN\\user).")
            if not password:
                raise BackupError("Enter the password for {}.".format(user))
            if "\n" in password or "\r" in password or len(password) > 256:
                raise BackupError("The password contains unsupported characters or is too long.")
            payload = "user={}\npassword={}\n".format(user, password)
        else:
            payload = "clear=1\n"
        rc, out, err = self._helper_command("set-credentials", stdin=payload)
        if rc != 0:
            raise BackupError(
                "Could not save the credentials: {}".format((err or out).strip() or "exit code {}".format(rc)), 500
            )
        self._settings.set(["smb_user"], user)
        self._settings.set(["smb_user_saved"], user or None)
        self._settings.save()
        self._logger.info("SMB credentials %s", "saved for user {}".format(user) if user else "removed")

    def _test_destination(self):
        if self._service_running():
            raise BackupError("A backup is running right now.")
        helper = self._helper_info(force=True)
        if not helper["installed"] or helper["version"] != EXPECTED_HELPER_VERSION or not helper["sudo_rule"]:
            raise BackupError("Install or update the helper first (see the setup box above).")
        target = self._target()
        if not target["ok"]:
            raise BackupError(target["message"])
        self._write_job(False, "connection test")
        rc, out, err = self._helper_command("test-destination", timeout=150)
        result = {}
        for line in out.splitlines():
            m = re.match(r"^([a-z_]+)=(.*)$", line)
            if m:
                result[m.group(1)] = m.group(2)
        if not result:
            raise BackupError("The test did not run: {}".format(err.strip() or "exit code {}".format(rc)), 500)
        for key in ("free", "image_size", "image_mtime", "images", "needed"):
            try:
                result[key] = int(result.get(key) or "") or None
            except ValueError:
                result[key] = None
        result["ok"] = result.get("ok") == "1"
        result["image_exists"] = result.get("image_exists") == "1"
        result["creates"] = result.get("creates") == "1"
        self._logger.info("Destination test: %s", "ok" if result["ok"] else result.get("message"))
        return result

    # --------------------------------------------------------- install helper

    def _start_install(self):
        with self._install_lock:
            if self._install["running"]:
                raise BackupError("The installation is already running.")
            if self._service_running():
                raise BackupError("A backup is running. Try again when it has finished.")
            if not self._passwordless_sudo():
                raise BackupError(
                    "OctoPrint's user cannot use sudo without a password here. "
                    "Run the install command over SSH instead.",
                    403,
                )
            self._install = {"running": True, "rc": None, "output": "", "finished": None}

        args = [
            SUDO,
            "-n",
            "/bin/bash",
            os.path.join(self._basefolder, "helper", "install.sh"),
            "--user",
            getpass.getuser(),
            "--job-file",
            self._job_file(),
        ]
        if not self._image_backup_path():
            args.append("--with-image-utils")

        def run():
            self._logger.info("Installing backup helper: %s", " ".join(shlex.quote(a) for a in args))
            env = dict(os.environ, TERM="dumb")
            try:
                p = subprocess.run(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=900,
                    universal_newlines=True,
                    env=env,
                )
                rc, out = p.returncode, p.stdout
            except (OSError, subprocess.SubprocessError) as e:
                rc, out = -1, str(e)
            out = re.sub(r"\x1b\[[0-9;]*m", "", out or "")
            self._cache.clear()
            with self._install_lock:
                self._install = {"running": False, "rc": rc, "output": out[-16000:], "finished": int(time.time())}
            self._logger.info("Backup helper installation finished with exit code %s", rc)
            self._plugin_manager.send_plugin_message(self._identifier, {"type": "install_finished", "rc": rc})

        threading.Thread(target=run, name="imagebackup-install", daemon=True).start()


__plugin_name__ = "System Image Backup"
__plugin_pythoncompat__ = ">=3.7,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = SystemImageBackupPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.protocol.atcommand.queuing": __plugin_implementation__.on_atcommand_queuing,
    }
