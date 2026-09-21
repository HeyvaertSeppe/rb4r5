"""Provisioning: make a stock Raspberry Pi OS install able to run the player.

Everything here is idempotent and reversible.  Edits to files the OS owns
(config.txt, cmdline.txt) are written inside a marked block:

    # >>> rb4r5 >>>
    ...
    # <<< rb4r5 <<<

so `rb4r5 setup --undo` can take them out again, and so re-running setup never
appends duplicates.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import config, platform5, util, zones

BEGIN = "# >>> rb4r5 >>>"
END = "# <<< rb4r5 <<<"

# Runtime packages; the build needs more (see build.py).
PACKAGES_RUNTIME = ["alsa-utils", "coreutils", "util-linux", "psmisc",
                    "exfatprogs", "dosfstools",
                    # firmware unpacking: AES for the .UPD, bsdtar for the ISO
                    "python3-cryptography", "libarchive-tools", "git"]
PACKAGES_BUILD = ["build-essential", "gcc-arm-linux-gnueabi",
                  "libc6-dev-armel-cross", "autoconf", "automake", "libtool",
                  "libtool-bin", "patchelf", "pkg-config", "git", "patch"]


def _apt(packages: list[str], update: bool = True) -> list[str]:
    missing = []
    for pkg in packages:
        proc = util.run(["dpkg-query", "-W", "-f=${Status}", pkg], check=False)
        if "install ok installed" not in (proc.stdout or ""):
            missing.append(pkg)
    if not missing:
        return []
    if not util.have("apt-get"):
        raise util.Fail(f"missing packages {missing} and no apt-get to install them")
    if update:
        util.step("apt-get update")
        util.run(["apt-get", "update"], check=False, timeout=900, capture=True)
    util.step(f"apt-get install {' '.join(missing)}")
    util.run(["apt-get", "install", "-y", "--no-install-recommends"] + missing,
             timeout=2400, env={"DEBIAN_FRONTEND": "noninteractive"})
    return missing


def install_runtime_packages() -> list[str]:
    return _apt(PACKAGES_RUNTIME)


def install_build_packages() -> list[str]:
    return _apt(PACKAGES_BUILD)


def _marked_block(lines: list[str]) -> str:
    return "\n".join([BEGIN] + lines + [END]) + "\n"


def _apply_block(path: Path, lines: list[str], undo: bool = False) -> str:
    """Insert/replace/remove our marked block in a config file."""
    if not path.exists():
        return f"{path} does not exist - skipped"
    text = path.read_text()
    backup = path.with_suffix(path.suffix + ".rb4r5.bak")
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?",
                         re.DOTALL)
    if undo:
        if not pattern.search(text):
            return f"{path}: nothing of ours to remove"
        new = pattern.sub("", text)
    else:
        block = _marked_block(lines)
        new = pattern.sub(block, text) if pattern.search(text) else \
            text.rstrip("\n") + "\n\n" + block
    if new == text:
        return f"{path}: already correct"
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(new)
    return f"{path}: {'removed our block' if undo else 'updated'} " \
           f"(backup: {backup.name})"


def config_txt(cfg, undo: bool = False) -> str:
    """Make sure KMS is on and, if asked, force an HDMI mode."""
    path = platform5.boot_dir() / "config.txt"
    lines = [
        "# rb4r5: the player draws through DirectFB into the DRM fbdev",
        "# emulation of vc4-kms-v3d, so the KMS driver must be active.",
        "dtoverlay=vc4-kms-v3d",
        "max_framebuffers=2",
        "disable_fw_kms_setup=1",
        "disable_splash=1",
    ]
    mode = cfg.get("display.force_mode")
    port = int(cfg.get("display.hdmi_port", 0) or 0)
    if mode:
        match = re.match(r"(\d+)x(\d+)(?:@(\d+))?", str(mode))
        if not match:
            raise util.Fail(f"display.force_mode '{mode}' should look like "
                            "1920x1080@60")
        width, height, rate = match.group(1), match.group(2), match.group(3) or "60"
        lines += [
            f"# force {width}x{height}@{rate} on HDMI{port}",
            f"hdmi_group:{port}=2",
            f"hdmi_mode:{port}=87",
            f"hdmi_cvt:{port}={width} {height} {rate} 3 0 0 0",
            f"hdmi_drive:{port}=2",
            f"hdmi_force_hotplug:{port}=1",
        ]
    return _apply_block(path, lines, undo)


def cmdline_txt(cfg, undo: bool = False) -> str:
    """Quiet the console and stop it blanking over the player.

    cmdline.txt is a single line, so the marked-block trick does not work here;
    we add/remove known tokens instead.
    """
    path = platform5.boot_dir() / "cmdline.txt"
    if not path.exists():
        return f"{path} does not exist - skipped"
    wanted = ["logo.nologo", "consoleblank=0", "vt.global_cursor_default=0"]
    if cfg.get("display.quiet_console", 2):
        wanted.append("loglevel=3")
    text = path.read_text().strip()
    tokens = text.split()
    backup = path.with_suffix(".txt.rb4r5.bak")
    changed = False
    if undo:
        for token in wanted:
            if token in tokens:
                tokens.remove(token)
                changed = True
    else:
        # loglevel=N may already be present with another value
        if "loglevel=3" in wanted:
            tokens = [t for t in tokens if not t.startswith("loglevel=")]
        for token in wanted:
            if token not in tokens:
                tokens.append(token)
                changed = True
    if not changed:
        return f"{path}: already correct"
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(" ".join(tokens) + "\n")
    return f"{path}: {'cleaned' if undo else 'updated'} (backup: {backup.name})"


def console_mode(undo: bool = False) -> list[str]:
    """Give the framebuffer to the player: no desktop, no login prompt on tty1."""
    notes = []
    if not util.have("systemctl"):
        return ["systemctl not available - cannot change the boot target"]
    if undo:
        util.run(["systemctl", "set-default", "graphical.target"], check=False)
        util.run(["systemctl", "enable", "getty@tty1.service"], check=False)
        return ["boot target back to graphical.target, getty@tty1 re-enabled"]

    current = util.out(["systemctl", "get-default"])
    if current != "multi-user.target":
        util.run(["systemctl", "set-default", "multi-user.target"], check=False)
        notes.append(f"boot target {current} -> multi-user.target "
                     "(no desktop; the player owns the screen)")
    else:
        notes.append("boot target already multi-user.target")

    # A getty on tty1 paints a login prompt over DirectFB.
    proc = util.run(["systemctl", "is-enabled", "getty@tty1.service"], check=False)
    if "enabled" in (proc.stdout or ""):
        util.run(["systemctl", "disable", "--now", "getty@tty1.service"], check=False)
        notes.append("getty@tty1 disabled (SSH and tty2-6 are unaffected)")
    return notes


def sysctl_quiet(undo: bool = False) -> str:
    path = Path("/etc/sysctl.d/99-rb4r5.conf")
    if undo:
        if path.exists():
            path.unlink()
            return f"removed {path}"
        return f"{path}: nothing to remove"
    content = ("# rb4r5: only emergencies reach the framebuffer console\n"
               "kernel.printk = 1 4 1 7\n")
    changed = util.write_text(path, content)
    if changed and util.have("sysctl"):
        util.run(["sysctl", "-q", "-p", str(path)], check=False)
    return f"{'wrote' if changed else 'kept'} {path}"


def systemd_quiet(undo: bool = False) -> str:
    path = Path("/etc/systemd/system.conf.d/99-rb4r5-quiet.conf")
    if undo:
        if path.exists():
            path.unlink()
            return f"removed {path}"
        return f"{path}: nothing to remove"
    content = ("# rb4r5: no '[ OK ] Started ...' lines over the player\n"
               "[Manager]\nShowStatus=no\n")
    return f"{'wrote' if util.write_text(path, content) else 'kept'} {path}"


def udev_rules(repo: Path, undo: bool = False) -> str:
    path = Path("/etc/udev/rules.d/99-rb4r5.rules")
    if undo:
        if path.exists():
            path.unlink()
            util.run(["udevadm", "control", "--reload"], check=False)
            return f"removed {path}"
        return f"{path}: nothing to remove"
    src = repo / "udev/99-rb4r5.rules"
    if not src.exists():
        return f"{src} missing in the repository - skipped"
    changed = util.write_text(path, src.read_text())
    if changed and util.have("udevadm"):
        util.run(["udevadm", "control", "--reload"], check=False)
        util.run(["udevadm", "trigger", "--subsystem-match=sound",
                  "--subsystem-match=input"], check=False)
    return f"{'installed' if changed else 'kept'} {path}"


def service_unit(cfg, repo: Path, undo: bool = False, enable: bool = True) -> list[str]:
    """Install (and optionally enable) the boot service."""
    path = Path("/etc/systemd/system/rb4r5.service")
    notes = []
    if undo:
        if path.exists():
            util.run(["systemctl", "disable", "--now", "rb4r5.service"], check=False)
            path.unlink()
            util.run(["systemctl", "daemon-reload"], check=False)
            notes.append(f"removed {path}")
        return notes or [f"{path}: nothing to remove"]

    src = repo / "systemd/rb4r5.service"
    if not src.exists():
        raise util.Fail(f"{src} missing in the repository")
    content = src.read_text().replace("@REPO@", str(repo)) \
                             .replace("@PYTHON@", shutil.which("python3") or "/usr/bin/python3") \
                             .replace("@LOGS@", str(cfg.logs))
    if util.write_text(path, content):
        notes.append(f"installed {path}")
    else:
        notes.append(f"{path} already current")
    util.run(["systemctl", "daemon-reload"], check=False)
    if enable:
        util.run(["systemctl", "enable", "rb4r5.service"], check=False)
        notes.append("rb4r5.service enabled (starts the player on every boot)")
    return notes


def directories(cfg) -> list[str]:
    made = []
    for path in (cfg.get("paths.root"), cfg.chroot, cfg.payload, cfg.work,
                 cfg.logs, "/etc/rb4r5"):
        target = Path(path)
        if not target.exists():
            util.ensure_dir(target)
            made.append(str(target))
    return made or ["all directories already present"]


def default_configs(cfg, repo: Path) -> list[str]:
    """Write the editable config files if they are not there yet."""
    notes = []
    if not cfg.path.exists():
        cfg.save()
        notes.append(f"wrote {cfg.path}")
    zone_path = cfg.get("touch.zones_file")
    if zone_path and not Path(zone_path).exists():
        zones.save_default(zone_path)
        notes.append(f"wrote {zone_path} (touch layout - edit to taste)")
    map_path = cfg.get("controller.map_file")
    src = repo / "config/flx4-map.conf"
    if map_path and src.exists() and not Path(map_path).exists():
        util.write_text(map_path, src.read_text())
        notes.append(f"wrote {map_path} (controller key overrides)")
    return notes or ["config files already present"]


def modules() -> list[str]:
    """Make sure the USB audio / multitouch drivers are loaded."""
    notes = []
    for module in ("snd_usb_audio", "hid_multitouch", "usb_storage"):
        if platform5.has_module(module):
            continue
        proc = util.run(["modprobe", module], check=False)
        notes.append(f"modprobe {module}" +
                     ("" if proc.returncode == 0 else " FAILED"))
    return notes or ["kernel modules already loaded"]


def all_steps(cfg, repo: Path, enable_service: bool = True,
              undo: bool = False) -> list[str]:
    notes = []
    if undo:
        notes += service_unit(cfg, repo, undo=True)
        notes.append(udev_rules(repo, undo=True))
        notes.append(systemd_quiet(undo=True))
        notes.append(sysctl_quiet(undo=True))
        notes += console_mode(undo=True)
        notes.append(cmdline_txt(cfg, undo=True))
        notes.append(config_txt(cfg, undo=True))
        return notes

    notes += directories(cfg)
    notes += default_configs(cfg, repo)
    installed = install_runtime_packages()
    notes.append(f"installed packages: {' '.join(installed)}" if installed
                 else "runtime packages already installed")
    notes += modules()
    notes.append(config_txt(cfg))
    notes.append(cmdline_txt(cfg))
    notes.append(sysctl_quiet())
    notes.append(systemd_quiet())
    notes += console_mode()
    notes.append(udev_rules(repo))
    notes += service_unit(cfg, repo, enable=enable_service)
    return notes
