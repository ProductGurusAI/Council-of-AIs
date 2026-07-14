import os
import subprocess
import json
import sys
from typing import Tuple, Union, List

def is_docker_available() -> bool:
    try:
        # Check if docker daemon is running and command is available
        res = subprocess.run(["docker", "info"], capture_output=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False

def run_sandboxed(cmd_or_code: Union[str, List[str]], workdir: str, timeout: float = 30) -> Tuple[int, str, str]:
    """
    Runs a command or Python code in a secure sandbox.
    If Docker is available:
      Runs inside a python:3.12-slim container with restricted memory, CPU, and no network.
    If Docker is not available:
      Falls back to subprocess execution with a visible warning logged to the transcript/logger.
    """
    docker_ok = is_docker_available()
    
    # Load sandbox.required configuration
    # Check if models.json is in the active workdir first, then fall back to the root CWD models.json
    sandbox_required = False
    paths_to_check = [
        os.path.join(workdir, "models.json"),
        "models.json"
    ]
    for p in paths_to_check:
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    config = json.load(f)
                sandbox_required = config.get("sandbox", {}).get("required", False)
                break
            except Exception:
                pass

    if not docker_ok:
        if sandbox_required:
            error_msg = "Docker sandbox is required but Docker is not available. Refusing to execute."
            print(f"ERROR: {error_msg}", file=sys.stderr, flush=True)
            return (-99, "", error_msg)
        
        # Log visible warning to transcript/logger (as required by BRIEF)
        warning_msg = "running without container isolation"
        print(f"WARNING: {warning_msg}", file=sys.stderr, flush=True)

        # Fallback to subprocess execution
        if isinstance(cmd_or_code, list):
            try:
                proc = subprocess.run(
                    cmd_or_code,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                return (proc.returncode, proc.stdout, proc.stderr)
            except subprocess.TimeoutExpired as te:
                return (-1, te.stdout or "", te.stderr or f"Timeout of {timeout}s expired.")
            except Exception as e:
                return (-2, "", str(e))
        else:
            try:
                proc = subprocess.run(
                    cmd_or_code,
                    shell=True,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                return (proc.returncode, proc.stdout, proc.stderr)
            except subprocess.TimeoutExpired as te:
                return (-1, te.stdout or "", te.stderr or f"Timeout of {timeout}s expired.")
            except Exception as e:
                return (-2, "", str(e))

    # Docker backend implementation
    # Resolve absolute path for workdir mount
    abs_workdir = os.path.abspath(workdir)
    
    # Construct Docker command
    docker_base = [
        "docker", "run", "--rm",
        "--network=none",
        "--memory=512m",
        "--cpus=1",
        "-v", f"{abs_workdir}:/work",
        "-w", "/work",
        "python:3.12-slim"
    ]
    
    if isinstance(cmd_or_code, list):
        # Translate local executable (e.g. sys.executable or python3) to generic python inside the container
        translated_cmd = []
        for arg in cmd_or_code:
            basename = os.path.basename(arg)
            if basename in ("python", "python3", "python.exe"):
                translated_cmd.append("python3")
            else:
                translated_cmd.append(arg)
        docker_cmd = docker_base + translated_cmd
    else:
        # Run string commands inside a shell inside the container
        docker_cmd = docker_base + ["sh", "-c", cmd_or_code]
        
    try:
        proc = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as te:
        return (-1, te.stdout or "", te.stderr or f"Timeout of {timeout}s expired.")
    except Exception as e:
        return (-2, "", str(e))
