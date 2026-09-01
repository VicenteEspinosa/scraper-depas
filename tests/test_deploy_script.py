import subprocess
from pathlib import Path

from pytest import fixture

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy-remote.sh"

# `docker compose run` attaches the container's stdin. The workflow pipes this script
# into `bash -s`, so stdin *is* the script: a run that does not redirect it swallows
# the remaining lines and bash exits 0 having never restarted anything.
STDIN_EATING_DOCKER = '#!/bin/sh\n[ "$2" = run ] && cat > /dev/null\nexit 0\n'
NO_OP = "#!/bin/sh\nexit 0\n"


@fixture
def deploy_path(tmp_path: Path) -> Path:
    """A checkout where everything that touches the world is stubbed, leaving the script.

    `sed` among them: the box is Linux and BSD `sed -i` reads the next argument as a
    backup suffix, which would fail the run on a developer's Mac for its own reasons.
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (("docker", STDIN_EATING_DOCKER), ("git", NO_OP), ("sed", NO_OP)):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    return tmp_path


def _deploy(deploy_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-s"],
        stdin=SCRIPT.open("rb"),
        capture_output=True,
        text=True,
        env={"PATH": f"{deploy_path / 'stubs'}:/usr/bin:/bin",
             "DEPLOY_PATH": str(deploy_path), "GITHUB_SHA": "a" * 40,
             "ENV_B64": "VFo9QW1lcmljYS9TYW50aWFnbwo="},
    )


def test_deploy_reaches_the_restart_when_a_step_reads_stdin(deploy_path):
    """The script is fed to bash on stdin, so no step may consume it and cut the deploy short."""
    finished = _deploy(deploy_path)

    assert finished.returncode == 0, finished.stderr
    assert (deploy_path / ".last-deployed-sha").read_text().strip() == "a" * 40
    assert "deploy of aaaaaaa applied" in finished.stdout


def test_deploy_stops_before_the_restart_when_the_env_check_rejects(deploy_path):
    """A refused .env must abort the deploy, leaving the old containers serving."""
    rejecting = deploy_path / "stubs" / "docker"
    rejecting.write_text('#!/bin/sh\n[ "$2" = run ] || exit 0\ncat > /dev/null\nexit 1\n')
    rejecting.chmod(0o755)

    finished = _deploy(deploy_path)

    assert finished.returncode != 0
    assert not (deploy_path / ".last-deployed-sha").exists()
    assert "docker compose up -d" not in finished.stdout
