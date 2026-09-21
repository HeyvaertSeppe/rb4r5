"""Building the runtime: shims, host daemons, DirectFB, and the patched player.

What can be built here, on the Pi itself:

    host daemons      flx4-bridge, rbkeyd, fakekbd    (native gcc, seconds)
    LD_PRELOAD shims  memshim/fbshim/audioshim/keyshim (arm-linux-gnueabi,
                      soft-float, sysroot = the RX3 chroot)
    DirectFB 1.4.16   the patched fbdev driver + core (~10 min on a Pi 5)
    rbp               your own firmware binary + the patch sets

What you must supply (nothing vendor-owned ships with rb4r5 - see NOTICE.md):

    <payload>/XDJRX3-rootfs, <payload>/XDJRX3   your extracted firmware
    PrimeBox                                    the firmware patch tooling
                                                (github.com/erhan-/PrimeBox)
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from . import chroot, provision, util

CROSS = "arm-linux-gnueabi-"
SHIMS = ["memshim.so", "fbshim.so", "audioshim.so", "keyshim.so"]
HOST_TOOLS = ["flx4-bridge", "rbkeyd", "fakekbd"]


def md5(path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def have_cross() -> bool:
    return util.have(CROSS + "gcc")


def require_cross() -> None:
    if have_cross():
        return
    raise util.Fail(
        f"the soft-float cross compiler is missing ({CROSS}gcc).\n"
        "Install it:\n"
        "    sudo apt-get install gcc-arm-linux-gnueabi libc6-dev-armel-cross\n"
        "It must be the *gnueabi* (soft-float) toolchain, not gnueabihf: the "
        "XDJ-RX3 userland is soft-float EABI5.")


def jobs(cfg) -> int:
    configured = int(cfg.get("build.jobs", 0) or 0)
    return configured or (os.cpu_count() or 4)


# --------------------------------------------------------------------------
def build_host_tools(cfg, repo: Path, install: bool = True) -> list[str]:
    """Compile the daemons that run outside the chroot."""
    out = util.ensure_dir(cfg.work / "host")
    notes = []
    util.run(["make", "-C", str(repo / "src/host"), f"OUT={out}",
              f"-j{jobs(cfg)}"], timeout=600)
    for name in HOST_TOOLS:
        built = out / name
        if not built.exists():
            raise util.Fail(f"{name} did not build (see the output above)")
        notes.append(f"{name} ({built.stat().st_size} bytes)")
        if install:
            target = cfg.bindir / name
            util.ensure_dir(cfg.bindir)
            shutil.copy2(built, target)
            os.chmod(target, 0o755)
            notes.append(f"installed {target}")
    return notes


def build_shims(cfg, repo: Path) -> list[str]:
    """Cross-compile the LD_PRELOAD shims against the RX3 userland.

    The chroot *is* the sysroot: it holds the RX3's glibc 2.13, libdl and
    friends, which is exactly what the shims must link against so they carry
    only GLIBC_2.4/2.7 symbol versions.
    """
    require_cross()
    root = cfg.chroot
    if not (root / "lib").is_dir():
        raise util.Fail(
            f"{root}/lib is missing - assemble the runtime first:\n"
            f"    sudo python3 launch.py payload")
    out = util.ensure_dir(cfg.work / "shims")
    compat = util.ensure_dir(cfg.work / "compat")

    # The runtime rootfs has no libc_nonshared.a / libpthread_nonshared.a
    # (they are dev-only archives); give the linker empty stubs.
    for stub in ("libc_nonshared.a", "libpthread_nonshared.a"):
        if not (compat / stub).exists():
            util.run([CROSS + "ar", "rcs", str(compat / stub)])

    util.run(["make", "-C", str(repo / "src/shims"), f"RX3={root}",
              f"OUT={out}", f"COMPAT={compat}", f"-j{jobs(cfg)}"], timeout=900)

    notes = []
    for name in SHIMS:
        built = out / name
        if not built.exists():
            raise util.Fail(f"{name} did not build")
        notes.append(f"{name} md5 {md5(built)}")
    notes += check_abi(out)
    return notes


def check_abi(directory: Path) -> list[str]:
    """Fail loudly if a shim needs a glibc the RX3 chroot does not have."""
    notes = []
    objdump = CROSS + "objdump"
    if not util.have(objdump):
        return ["(objdump missing: skipped the ABI check)"]
    allowed = {"GLIBC_2.0", "GLIBC_2.1", "GLIBC_2.2", "GLIBC_2.3", "GLIBC_2.4",
               "GLIBC_2.5", "GLIBC_2.6", "GLIBC_2.7", "GLIBC_2.11",
               "GLIBC_2.13", "GLIBC_PRIVATE"}
    for so in sorted(directory.glob("*.so")):
        text = util.out([objdump, "-T", str(so)])
        versions = {word for word in text.split() if word.startswith("GLIBC_")}
        bad = sorted(versions - allowed)
        if bad:
            raise util.Fail(
                f"{so.name} references {', '.join(bad)}, which the RX3's "
                "glibc 2.13 cannot resolve - the player would fail to start.\n"
                "This usually means the wrong sysroot: the chroot must hold "
                "the RX3 rootfs.")
        notes.append(f"{so.name}: {', '.join(sorted(versions)) or 'no versioned symbols'}")
    return notes


def build_directfb(cfg, repo: Path, fast: bool = False) -> list[str]:
    """Build the patched DirectFB 1.4.16 stack (or just the fbdev module)."""
    require_cross()
    primebox = Path(cfg.get("build.primebox"))
    script = repo / ("src/directfb/rebuild-fbdev.sh" if fast
                     else "src/directfb/build-directfb.sh")
    env = {
        "REPO": str(repo),
        "RX3": str(cfg.chroot),
        "PRIMEBOX": str(primebox),
        "OUT": str(cfg.work / "dfb"),
        "BUILD": str(cfg.work / "dfb-build"),
        "SYS": str(cfg.work / "sysroot"),
        "NEON": "1" if cfg.get("build.neon", True) else "0",
        "JOBS": str(jobs(cfg)),
    }
    if not fast:
        diff = primebox / "tools/build-directfb/directfb-full.diff"
        if not diff.exists():
            raise util.Fail(
                "the DirectFB base diff is missing:\n"
                f"    {diff}\n"
                "Clone the PrimeBox project next to the payload and point "
                "build.primebox at it:\n"
                f"    sudo git clone https://github.com/erhan-/PrimeBox "
                f"{primebox}\n"
                "It carries the RK3288/soft-float DirectFB diff and the rbp "
                "patch tooling that rb4r5 builds on (docs/03-payload.md).")
    util.step(f"running {script.name} (this takes a while on first build)")
    util.run(["sh", str(script)], env=env, timeout=7200, capture=False)
    module = cfg.work / "dfb/lib/directfb-1.4-6/systems/libdirectfb_fbdev.so"
    if not module.exists():
        raise util.Fail(f"{module} was not produced - see the build output")
    return [f"libdirectfb_fbdev.so md5 {md5(module)}"]


def build_player(cfg, repo: Path) -> list[str]:
    """stock rbp -> PrimeBox patch set -> our crash guards -> work/rbp-pi5."""
    primebox = Path(cfg.get("build.primebox"))
    patcher = primebox / "tools/patch-rbp/rbp_patch.py"
    stock = cfg.payload / "XDJRX3/pdj/rbp"
    if not stock.exists():
        stock_alt = cfg.chroot / "root/pdj/rbp"
        if stock_alt.exists():
            stock = stock_alt
        else:
            raise util.Fail(
                f"the stock player is missing: {stock}\n"
                "Extract your XDJ-RX3 firmware into the payload directory "
                "(docs/03-payload.md).")
    if not patcher.exists():
        raise util.Fail(
            f"PrimeBox's rbp patcher is missing: {patcher}\n"
            "It turns the stock XDJ-RX3 binary into one that runs on "
            "non-Pioneer hardware.  Clone PrimeBox (docs/03-payload.md).")

    work = util.ensure_dir(cfg.work)
    audio_out = work / "rbp-audio"
    final = work / "rbp-pi5"
    notes = [f"stock rbp md5 {md5(stock)}"]

    util.step("applying the PrimeBox patch set")
    util.run(["python3", str(patcher), str(stock), "-o", str(audio_out)],
             timeout=600, capture=False)
    notes.append(f"rbp-audio md5 {md5(audio_out)}")

    util.step("applying the getPcController crash guard")
    guards = repo / "src/patch/patch-rbp-crashguards.py"
    util.run(["python3", str(guards), "--in", str(audio_out),
              "--out", str(final)], timeout=120, capture=False)
    os.chmod(final, 0o755)
    notes.append(f"rbp-pi5 md5 {md5(final)}")
    return notes


def all_steps(cfg, repo: Path, with_directfb: bool = True,
              with_player: bool = True, fast_dfb: bool = False) -> list[str]:
    notes = []
    provision.install_build_packages()

    util.step("host daemons")
    notes += build_host_tools(cfg, repo)

    if not (cfg.chroot / "lib").is_dir():
        util.step("assembling the chroot from the payload")
        notes += chroot.assemble(cfg)

    util.step("LD_PRELOAD shims (soft-float)")
    notes += build_shims(cfg, repo)

    if with_player:
        util.step("player binary")
        try:
            notes += build_player(cfg, repo)
        except util.Fail as exc:
            notes.append(f"player NOT built: {exc}")

    if with_directfb:
        util.step("DirectFB")
        try:
            notes += build_directfb(cfg, repo, fast=fast_dfb)
        except util.Fail as exc:
            notes.append(f"DirectFB NOT built: {exc}")

    util.step("installing into the chroot")
    notes += chroot.install_runtime_bits(cfg, repo)
    notes.append(chroot.write_directfbrc(cfg))
    return notes
