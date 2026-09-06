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
# `compose images -q` answers with a different id once the restart has happened, which
# is what tells the script its build replaced something.
LOGGING_DOCKER = ('#!/bin/sh\n'
                  'echo "docker $*" >> docker.log\n'
                  '[ "$2" = run ] && cat > /dev/null\n'
                  'if [ "$2" = images ]; then\n'
                  '  if grep -q "compose up -d" docker.log; then echo newimageid\n'
                  '  else echo oldimageid; fi\n'
                  'fi\n'
                  'exit 0\n')

# Anything that reaches past this project is a bug: the box runs other stacks whose
# images are built on it and pushed to no registry, so a daemon-wide `-a` is fatal
# to them. Dangling images and aged build cache are the two things nothing can miss.
PRUNES_ALLOWED = ("docker image prune -f", 'docker builder prune -f --filter "until=')


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
    assert "reclaiming what Docker can spare" in finished.stdout
    assert (deploy_path / ".last-deployed-sha").read_text().strip() == "a" * 40


def test_a_deploy_with_room_to_spare_keeps_the_build_cache(deploy_path):
    """Pruning the cache costs the next build, so it is only ever worth it when short."""
    finished = _deploy(deploy_path)

    assert finished.returncode == 0, finished.stderr
    assert "reclaiming what Docker can spare" not in finished.stdout


def test_the_image_the_build_replaced_is_reaped_by_id(deploy_path):
    """The leak that filled the box: every build orphans one layer set, nothing collected it."""
    _stub(deploy_path, "docker", LOGGING_DOCKER)

    finished = _deploy(deploy_path)
    calls = (deploy_path / "docker.log").read_text()

    assert finished.returncode == 0, finished.stderr
    # By id and after the restart, so what is removed is the one this deploy replaced.
    assert "docker image rm oldimageid" in calls
    assert calls.index("compose up -d") < calls.index("image rm oldimageid")
    assert "removed oldimageid" in finished.stdout


def test_the_image_still_in_use_is_left_alone(deploy_path):
    """A build that changed nothing must not reap the image the containers are running."""
    # Same id before and after: `compose images` never learns a new one.
    _stub(deploy_path, "docker",
          '#!/bin/sh\necho "docker $*" >> docker.log\n[ "$2" = run ] && cat > /dev/null\n'
          '[ "$2" = images ] && echo sameimageid\nexit 0\n')

    finished = _deploy(deploy_path)

    assert finished.returncode == 0, finished.stderr
    assert "image rm" not in (deploy_path / "docker.log").read_text()


def test_a_reap_that_is_refused_is_reported_and_not_forced(deploy_path):
    """Something still references it, on a box full of other stacks. Take the no."""
    _stub(deploy_path, "docker",
          '#!/bin/sh\necho "docker $*" >> docker.log\n[ "$2" = run ] && cat > /dev/null\n'
          'if [ "$2" = images ]; then\n'
          '  if grep -q "compose up -d" docker.log; then echo newimageid\n'
          '  else echo oldimageid; fi\nfi\n'
          '[ "$1" = image ] && [ "$2" = rm ] && exit 1\nexit 0\n')

    finished = _deploy(deploy_path)

    assert finished.returncode == 0, finished.stderr
    assert "kept oldimageid, something still references it" in finished.stdout
    # -f would have taken it anyway, and an image is shared state on a shared box.
    assert "image rm -f" not in (deploy_path / "docker.log").read_text()


def test_no_docker_command_can_reach_another_stack():
    """The one thing on the box that is not scoped to this project is the daemon itself.

    Read off the script rather than a run, because the danger is a line somebody adds
    later: every other test here would still pass with an `-a` on the image prune.
    """
    # Comments stripped: they are where the footguns are named, which is not using one.
    code = [line.strip() for line in SCRIPT.read_text().splitlines()
            if not line.lstrip().startswith("#")]

    prunes = [line for line in code if "prune" in line]
    assert prunes, "the reclaim path went missing"
    for line in prunes:
        assert line.startswith(PRUNES_ALLOWED), line
    # Both are daemon-wide, and unrecoverable for a stack built on the box with no registry.
    assert not [line for line in code if "image prune -a" in line or "system prune" in line]
    # -f on a removal overrides exactly the refusal that keeps another stack's image safe.
    assert not [line for line in code if "image rm -f" in line or "rmi -f" in line]


def test_the_reclaim_never_touches_a_tagged_image(deploy_path):
    """Short on space is not a licence to delete what another stack cannot rebuild."""
    _stub(deploy_path, "df", _df(10, then=50_000))
    _stub(deploy_path, "docker", LOGGING_DOCKER)

    finished = _deploy(deploy_path)
    calls = (deploy_path / "docker.log").read_text()

    assert finished.returncode == 0, finished.stderr
    assert "docker image prune -f\n" in calls        # dangling only, never -a
    assert "builder prune -f --filter until=24h" in calls
