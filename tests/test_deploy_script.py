import subprocess
from pathlib import Path

from pytest import fixture

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy-remote.sh"

# `docker compose run` attaches the container's stdin. The workflow pipes this script
# into `bash -s`, so stdin *is* the script: a run that does not redirect it swallows
# the remaining lines and bash exits 0 having never restarted anything.
STDIN_EATING_DOCKER = '#!/bin/sh\n[ "$2" = run ] && cat > /dev/null\nexit 0\n'
NO_OP = "#!/bin/sh\nexit 0\n"


def _df(free_mb: int, *, then: int | None = None) -> str:
    """A `df -Pm` whose Available column is what we say, and which can change its mind.

    `then` is what every call after the first reports, which is how a prune that did or
    did not reclaim anything is told apart without a Docker daemon in the test. The
    counter is written beside the checkout, where the script has already `cd`-ed.
    """
    after = free_mb if then is None else then
    return ("#!/bin/sh\n"
            "seen=$(cat .df-calls 2>/dev/null || echo 0)\n"
            'echo $((seen + 1)) > .df-calls\n'
            'echo "Filesystem 1M-blocks Used Available Capacity Mounted on"\n'
            f'[ "$seen" -eq 0 ] && echo "/dev/sda 999 1 {free_mb} 9% /" '
            f'|| echo "/dev/sda 999 1 {after} 9% /"\n')


# Every docker call, in order, written beside the checkout for a test to read back.
LOGGING_DOCKER = ('#!/bin/sh\n[ "$2" = run ] && cat > /dev/null\n'
                  'echo "docker $*" >> docker.log\nexit 0\n')


@fixture
def deploy_path(tmp_path: Path) -> Path:
    """A checkout where everything that touches the world is stubbed, leaving the script.

    `sed` among them: the box is Linux and BSD `sed -i` reads the next argument as a
    backup suffix, which would fail the run on a developer's Mac for its own reasons.
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (("docker", STDIN_EATING_DOCKER), ("git", NO_OP), ("sed", NO_OP),
                       # Plenty of room by default, so the space check is out of the way
                       # of every test that is not about it.
                       ("df", _df(99_999))):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    return tmp_path


def _stub(deploy_path: Path, name: str, body: str) -> None:
    (deploy_path / "stubs" / name).write_text(body)
    (deploy_path / "stubs" / name).chmod(0o755)


def _deploy(deploy_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-s"],
        stdin=SCRIPT.open("rb"),
        capture_output=True,
        text=True,
        env={"PATH": f"{deploy_path / 'stubs'}:/usr/bin:/bin",
             "DEPLOY_PATH": str(deploy_path), "GITHUB_SHA": "a" * 40,
             "ENV_B64": "VFo9QW1lcmljYS9TYW50aWFnbwo=", "MIN_FREE_MB": "2048"},
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


# -- the box running out of disk --------------------------------------------------


def test_a_full_box_is_refused_before_anything_is_written(deploy_path):
    """The failure this replaces was `sed: couldn't flush` two writes in, naming a temp file."""
    _stub(deploy_path, "df", _df(10))

    finished = _deploy(deploy_path)

    assert finished.returncode != 0
    assert "refusing to deploy" in finished.stderr and "10MB free" in finished.stderr
    # Nothing written means the old containers are still serving what they were.
    assert not (deploy_path / ".env").exists()
    assert not (deploy_path / ".last-deployed-sha").exists()


def test_a_full_box_deploys_once_the_prune_has_room_again(deploy_path):
    """Docker holding the space is the common case, and it is one the deploy can fix itself."""
    _stub(deploy_path, "df", _df(10, then=50_000))

    finished = _deploy(deploy_path)

    assert finished.returncode == 0, finished.stderr
    assert "reclaiming what Docker is holding" in finished.stdout
    assert (deploy_path / ".last-deployed-sha").read_text().strip() == "a" * 40


def test_a_deploy_with_room_to_spare_keeps_the_build_cache(deploy_path):
    """Pruning the cache costs the next build, so it is only ever worth it when short."""
    finished = _deploy(deploy_path)

    assert finished.returncode == 0, finished.stderr
    assert "reclaiming what Docker is holding" not in finished.stdout


def test_the_image_the_build_replaced_is_reaped(deploy_path):
    """The leak that filled the box: every build orphans one layer set, nothing collected it."""
    _stub(deploy_path, "docker", LOGGING_DOCKER)

    finished = _deploy(deploy_path)
    calls = (deploy_path / "docker.log").read_text()

    assert finished.returncode == 0, finished.stderr
    # Dangling only: -a here would be a different, much larger promise.
    assert "docker image prune -f" in calls
    assert calls.index("compose up -d") < calls.index("image prune -f")


def test_a_failed_reap_does_not_fail_an_applied_deploy(deploy_path):
    """By then the containers are already restarted; there is nothing left to abort."""
    _stub(deploy_path, "docker",
          '#!/bin/sh\n[ "$2" = run ] && cat > /dev/null\n'
          '[ "$1" = image ] && exit 1\nexit 0\n')

    finished = _deploy(deploy_path)

    assert finished.returncode == 0, finished.stderr
    assert "applied" in finished.stdout
