"""Isolated Python execution with bubblewrap: no network, read-only system, tmpfs workdir, rlimits.
Falls back to Docker (--network none) if bwrap is unavailable. Never runs on the bare host."""
import asyncio, os, shutil, sys, tempfile
from ..config import get_settings


def backend() -> str | None:
    if shutil.which("bwrap"):
        return "bwrap"
    if shutil.which("docker") and os.access("/var/run/docker.sock", os.W_OK):
        return "docker"
    return None


def unavailable_reason() -> str | None:
    return None if backend() else "no sandbox available (install bubblewrap or grant Docker access)"


RLIMIT_PRELUDE = (
    "import resource as _r\n"
    "_r.setrlimit(_r.RLIMIT_AS,(1024*1024*1024,)*2)\n"
    "_r.setrlimit(_r.RLIMIT_NPROC,(64,64))\n"
    "_r.setrlimit(_r.RLIMIT_FSIZE,(50*1024*1024,)*2)\n"
    "del _r\n"
)


async def run_python(code: str, files: dict[str, bytes] | None = None, timeout: int | None = None) -> dict:
    """Returns {stdout, stderr, exit_code, timed_out, outputs: {name: bytes}} for files written to ./out/."""
    timeout = timeout or get_settings().sandbox_timeout_s
    kind = backend()
    if not kind:
        raise RuntimeError(unavailable_reason())
    with tempfile.TemporaryDirectory(prefix="sbx-") as work:
        os.makedirs(f"{work}/out")
        for name, data in (files or {}).items():
            with open(os.path.join(work, os.path.basename(name)), "wb") as f:
                f.write(data)
        with open(f"{work}/main.py", "w") as f:
            f.write(RLIMIT_PRELUDE + code)
        if kind == "bwrap":
            cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session",
                   "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                   "--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                   "--bind", work, "/work", "--chdir", "/work", "--clearenv", "--setenv", "HOME", "/tmp",
                   "--setenv", "PATH", "/usr/bin",
                   # app venv mounted read-only at a neutral path so pandas/numpy/openpyxl are importable
                   # without exposing the host's home directory layout
                   "--ro-bind", sys.prefix, "/venv", "/venv/bin/python", "-I", "main.py"]
        else:
            cmd = ["docker", "run", "--rm", "--network", "none", "--memory", "1g", "--cpus", "1", "--pids-limit", "64",
                   "-v", f"{work}:/work", "-w", "/work", "python:3.12-slim", "python", "-I", "main.py"]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.CancelledError:                  # user pressed Stop (bwrap --die-with-parent ends the child)
            proc.kill()
            raise
        except asyncio.TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            timed_out = True
        outputs = {}
        for name in os.listdir(f"{work}/out")[:10]:
            p = f"{work}/out/{name}"
            if os.path.isfile(p) and os.path.getsize(p) < 20 * 1024 * 1024:
                outputs[name] = open(p, "rb").read()
        return {"stdout": out.decode(errors="replace")[-20000:], "stderr": err.decode(errors="replace")[-8000:],
                "exit_code": proc.returncode, "timed_out": timed_out, "outputs": outputs}
