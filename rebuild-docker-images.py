#!/usr/bin/env python3
"""
Rebuilds the Session CI images with `docker buildx bake`, and pushes them.

Each distro is one bake run, building its -base, -builder and final images (./Dockerfile) on every
arch, followed by the images built on top of them (images/*.Dockerfile).  Distros run in parallel and
fail independently, and a failed one is retried.  A released distro's images are rebuilt only when
apt would upgrade something in them (see upgrades_available); sid's and testing's every run.  With
--scheduled, only the images due for a refresh are considered (see refresh_days and EXTRAS).
"""

import fcntl
import json
import optparse
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests

KEEP_LOGS_DAYS = 30

parser = optparse.OptionParser()
parser.add_option("--registry", default="reg.session.codes:80",
                  help="Registry to push to [default: %default]")
parser.add_option("--registry-https", action="store_true",
                  help="The registry speaks HTTPS (by default it is plain HTTP, as ours is over the VPN)")
parser.add_option("--builder", default="session-ci",
                  help="buildx builder to use, created on first use [default: %default]")
parser.add_option("--cache-size", type="int", default=150,
                  help="GB of build cache the builder keeps, when it creates it: it has to hold every image's "
                       "install layers for a week [default: %default]")
parser.add_option("--distro", "-d", action="append", default=[],
                  help="Only build this distro, e.g. debian-sid, or one arch of it, e.g. debian-sid/amd64 "
                       "(repeatable).  The multi-arch tags are only pushed by a run that builds every arch")
parser.add_option("--debug", action="store_true",
                  help="Show the build output instead of writing it to log files; implies -j1")
parser.add_option("--arch", default="",
                  help="Only build these arches, comma separated: amd64,i386,arm64v8,arm32v7.  The multi-arch "
                       "tags are only pushed by a run that builds every arch")
parser.add_option("--parallel", "-j", type="int", default=3,
                  help="Build this many distros at once [default: %default]")
parser.add_option("--retries", type="int", default=2,
                  help="Retry a failed distro this many times [default: %default]")
parser.add_option("--bake-timeout", type="int", default=120,
                  help="Minutes before a bake attempt is killed and counted as failed [default: %default]")
parser.add_option("--check-timeout", type="int", default=10,
                  help="Minutes before an image's apt check is killed, which counts as behind [default: %default]")
parser.add_option("--retry-delay", type="int", default=60,
                  help="Seconds before the first retry; doubles on each further retry [default: %default]")
parser.add_option("--no-push", action="store_true", help="Build only, keeping the result in the build cache")
parser.add_option("--no-cache", action="store_true", help="Ignore the build cache")
parser.add_option("--scheduled", action="store_true",
                  help="Only build the images due for a refresh, as the nightly timer does; other runs build "
                       "everything they are asked to")
parser.add_option("--log-dir", default=os.environ.get('LOGS_DIRECTORY', 'logs'),
                  help="Where each run writes its build logs; runs older than %d days are deleted "
                       "[default: $LOGS_DIRECTORY, else logs/ next to this script]" % KEEP_LOGS_DAYS)
parser.add_option("--print", action="store_true", dest="print_only",
                  help="Print the bake definitions and exit")
(options, args) = parser.parse_args()

here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)

def notify_discord(message):
    """Posts to the Discord webhook in $DISCORD_WEBHOOK_URL, if set."""
    url = os.environ.get('DISCORD_WEBHOOK_URL')
    if not url:
        return
    try:
        requests.post(url, json={"content": message[:2000]}, timeout=30).raise_for_status()
    except Exception as e:
        print(f"Discord notification failed: {e}", file=sys.stderr)


def notify_crash(exc_type, exc, tb):
    if not issubclass(exc_type, KeyboardInterrupt):
        trace = ''.join(traceback.format_exception(exc_type, exc, tb))[-1800:]
        notify_discord(f"**Session CI image rebuild crashed on {socket.gethostname()}**\n```\n{trace}\n```")
    sys.__excepthook__(exc_type, exc, tb)


sys.excepthook = notify_crash


def die(message):
    """Exits on a configuration error, reported like a crash: sys.exit never reaches the excepthook."""
    notify_discord(f"**Session CI image rebuild on {socket.gethostname()} did not start**\n{message}")
    sys.exit(message)


# Two runs (a timer and a manual one, say) pushing the same tags break each other.
lockfile = open('.rebuild.lock', 'w')
try:
    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("Another rebuild-docker-images.py run holds .rebuild.lock; not starting", file=sys.stderr)
    sys.exit(75)



printlock = threading.Lock()
started = time.time()


def say(message):
    with printlock:
        print(f"[{time.time() - started:6.0f}s] {message}", flush=True)


# Error-Mode: by default `update` exits 0 with a mirror unreachable, and the dry-run upgrade on the
# old lists then reads as nothing to upgrade.
APT = "apt-get -o=Dpkg::Use-Pty=0 -o=Acquire::Retries=5 -o=APT::Update::Error-Mode=any -q"
# The last layer of every pushed image, on the install layers its Dockerfile builds: the packages
# that moved since those were built.  Rebuilt on every run, a few MB; the install layers below it
# keep their digests, so that is all a runner downloads for a refreshed image.  Nothing runs after
# it to notice a package it removed, so it fails if a package a Dockerfile named is gone after the
# upgrade (apt's manual set: Debian's images mark none of their own, Ubuntu's mark all, so there
# the base packages are covered too).  A transition apt can only resolve by dropping one waits for
# the weekly rebuild of the install layers.  No `${`: bake would interpolate it as its own variable.
UPGRADE_LAYER = '''FROM pkgs
ARG APT
ARG APT_STATE
RUN echo "apt state: $APT_STATE" \\
    && $APT update \\
    && apt-mark showmanual > /tmp/asked \\
    && $APT dist-upgrade -y \\
    && $APT autoremove -y \\
    && apt-mark showmanual | comm -23 /tmp/asked - > /tmp/gone \\
    && if [ -s /tmp/gone ]; then echo "the upgrade removed packages the Dockerfile installed:"; cat /tmp/gone; exit 1; fi \\
    && rm -rf /var/lib/apt/lists/* /tmp/asked /tmp/gone
'''
PLATFORMS = {'amd64': 'linux/amd64', 'i386': 'linux/386', 'arm64v8': 'linux/arm64', 'arm32v7': 'linux/arm/v7'}
DEBIAN_ARCHES = ['amd64', 'arm64v8', 'i386', 'arm32v7']
UBUNTU_ARCHES = ['amd64', 'arm64v8']

# Every new image is a fresh pull for each CI runner that uses it.  A release that still moves (sid,
# testing) is refreshed nightly: a week of sid is 100+ upgrades, minutes of dist-upgrade in every CI
# step.  A released one upgrades a handful of packages in a week, so it is refreshed weekly.  See
# refresh_days.
NIGHTLY, WEEKLY = 1, 7

# name: (upstream image, deb.session.foundation suite, arches).  @SUITE@: see the Dockerfile.
DISTROS = {
    'debian-sid': ('debian:sid', 'sid', DEBIAN_ARCHES),
    'debian-stable': ('debian:stable', 'stable', DEBIAN_ARCHES),
    'debian-testing': ('debian:testing', 'testing', DEBIAN_ARCHES),
    'debian-forky': ('debian:forky', 'forky', DEBIAN_ARCHES),
    'debian-trixie': ('debian:trixie', 'trixie', DEBIAN_ARCHES),
    'debian-bookworm': ('debian:bookworm', 'bookworm', DEBIAN_ARCHES),
    'ubuntu-rolling': ('ubuntu:rolling', '@SUITE@', UBUNTU_ARCHES),
    'ubuntu-lts': ('ubuntu:latest', '@SUITE@', UBUNTU_ARCHES),
    'ubuntu-stonking': ('ubuntu:stonking', 'stonking', UBUNTU_ARCHES),
    'ubuntu-resolute': ('ubuntu:resolute', 'resolute', UBUNTU_ARCHES),
    'ubuntu-noble': ('ubuntu:noble', 'noble', UBUNTU_ARCHES),
    'ubuntu-jammy': ('ubuntu:jammy', 'jammy', UBUNTU_ARCHES),
}

# The images built from images/NAME.Dockerfile, on the image its FROM names: NAME => arches, and
# optionally its refresh days (default: its distro's) and, for a FROM that isn't one of ours, the
# distro whose schedule it follows.  An image is built with the images built on it.
EXTRAS = {
    'debian-sid-clang': {'arches': ['amd64']},
    # 2 GB: refreshed weekly, though testing is nightly.
    'android': {'arches': ['amd64'], 'days': WEEKLY},
    'debian-win32-cross': {'arches': ['amd64']},
    'debian-forky-s390x-cross': {'arches': ['amd64']},
    'lint': {'arches': ['amd64']},
    'clone': {'arches': ['amd64', 'arm64v8'], 'distro': 'debian-bookworm'},
}

# A refresh is due a little early, so that the timer's random delay and a slower build don't push a
# weekly image to the eighth night.
DUE_SLACK = 6 * 3600

SCHEDULE_FILE = os.path.join(os.environ.get('STATE_DIRECTORY', here), 'schedule.json')


def distro_images(distro):
    """The distro's own images: name => Dockerfile stage."""
    return {distro + '-base': 'base', distro + '-builder': 'builder', distro: 'final'}


# alias distro => the distro named after the release it currently is (debian-stable => debian-trixie),
# which builds it and pushes its images under both names.  Filled by main().
ALIASES = {}


# distro => (codename, Debian's Suite for it: unstable, testing, stable, oldstable...; "released" for
# Ubuntu).  Filled by main(); a distro missing from it could not be resolved.
RELEASES = {}


def release_info(distro):
    """The release a distro's tag names today: (trixie, stable) for debian-stable, (resolute, released) for ubuntu-lts."""
    _, suite, _ = DISTROS[distro]
    if not distro.startswith('debian-'):
        return codename(distro), 'released'
    r = fetch(f"http://deb.debian.org/debian/dists/{suite}/InRelease")
    fields = dict(line.split(': ', 1) for line in r.text.splitlines() if line.startswith(('Codename: ', 'Suite: ')))
    return fields['Codename'], fields['Suite']


def moving(distro):
    """Whether the distro's release still moves (sid, testing): some package of its images changes every day."""
    return RELEASES.get(distro, (None, 'unknown'))[1] in ('unstable', 'testing', 'unknown')


def refresh_days(distro):
    """
    Nightly while the distro's release still moves, weekly once it is released: forky turns weekly by
    itself when it becomes stable.  An unresolved release counts as moving: rebuilt nightly, unchecked.
    """
    return NIGHTLY if moving(distro) else WEEKLY


def find_aliases():
    """Fills RELEASES, and returns the aliases: see ALIASES."""
    releases = {}
    for distro in DISTROS:
        try:
            RELEASES[distro] = release_info(distro)
        except Exception as e:
            say(f"{distro}: release unknown ({e}); built on its own")
            continue
        releases.setdefault(f"{distro.split('-')[0]}-{RELEASES[distro][0]}", []).append(distro)
    aliases = {}
    for release, names in releases.items():
        # Only into a distro named after the release: its image and suite stay put when the alias moves.
        if release not in names:
            continue
        for name in names:
            if name != release and DISTROS[name][2] == DISTROS[release][2]:
                aliases[name] = release
                say(f"{name} is {release} today: built once, pushed under both names")
    return aliases


def tag_names(name):
    """The names image `name` is pushed under: its own, and the same image of each alias of its distro."""
    for distro in DISTROS:
        stage = distro_images(distro).get(name)
        if stage:
            return [name] + [alias_name for alias, target in ALIASES.items() if target == distro
                             for alias_name, alias_stage in distro_images(alias).items() if alias_stage == stage]
    return [name]


def extra_parents():
    """EXTRAS image => the image its Dockerfile is built FROM, or None for one that isn't ours."""
    ours = set(EXTRAS).union(*(distro_images(d) for d in DISTROS))
    files = {f[:-len('.Dockerfile')] for f in os.listdir('images') if f.endswith('.Dockerfile')}
    if files != set(EXTRAS):
        die(f"images/*.Dockerfile and EXTRAS disagree on {', '.join(sorted(files ^ set(EXTRAS)))}")
    parents = {}
    for name in EXTRAS:
        with open(f'images/{name}.Dockerfile') as f:
            parent = next(m.group(1) for line in f if (m := re.match(r'FROM\s+(?:--\S+\s+)*(\S+)', line)))
        parents[name] = parent if parent in ours else None
        if parents[name] is None and 'distro' not in EXTRAS[name]:
            die(f"images/{name}.Dockerfile is FROM {parent}: EXTRAS['{name}'] needs a 'distro'")
    return parents


def extra_distro(name, parents):
    parent = parents[name]
    if parent is None:
        return EXTRAS[name]['distro']
    if parent in EXTRAS:
        return extra_distro(parent, parents)
    return next(d for d in DISTROS if parent in distro_images(d))


def fetch(url):
    """GET with retries."""
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            error = e
            time.sleep(5)
    raise RuntimeError(f"{url}: {error}")


def image_id(ref):
    out = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', ref],
                         capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


def pulling(ref, run):
    """
    Calls run(), a `docker run --pull always` of ref.  The pulled image stays in the daemon's store, so
    the next pull fetches only what changed; the image it replaces is removed, where it would otherwise
    stay as a dangling image that nothing prunes.
    """
    before = image_id(ref)
    try:
        return run()
    finally:
        after = image_id(ref)
        if before and after and before != after:
            subprocess.run(['docker', 'image', 'rm', before], capture_output=True, timeout=300)


def codename(distro):
    image, suite, _ = DISTROS[distro]
    if suite != '@SUITE@':
        return suite
    out = pulling(image, lambda: subprocess.run(['docker', 'run', '--rm', '--pull', 'always', image, 'sh', '-c',
                                                 '. /etc/os-release && echo "$VERSION_CODENAME"'],
                                                check=True, capture_output=True, text=True, timeout=600))
    return out.stdout.strip()


def pkgs_spec(name, parents, pkgs_state):
    """bake attributes of the build of image `name`'s install layers, without platforms and output."""
    for distro in DISTROS:
        stage = distro_images(distro).get(name)
        if stage:
            # An alias's image (an extra's FROM debian-stable-base) has to be the same build as its
            # target's, or the extra would rebuild the whole distro from the alias's upstream image.
            image, suite, _ = DISTROS[ALIASES.get(distro, distro)]
            return {"context": ".", "dockerfile": "Dockerfile", "target": stage, "pull": True,
                    "args": {"APT": APT, "DISTRO_IMAGE": image, "SESSION_SUITE": suite, "PKGS_STATE": pkgs_state}}
    # An extra built on one of ours is rebuilt with it; only one on an upstream image needs the state.
    args = {"APT": APT} if parents[name] else {"APT": APT, "PKGS_STATE": pkgs_state}
    return {"context": ".", "dockerfile": f"images/{name}.Dockerfile", "pull": True, "args": args}


def pkgs_target(targets, name, arches, parents, pkgs_state):
    """
    Adds the build-only target of image `name`'s install layers, on those of the image its FROM
    names, and returns its name.  Shared by the pushed images built on it, which BuildKit then builds
    from the cache.
    """
    target = f"pkgs-{name}-{'-'.join(arches)}"
    if target not in targets:
        spec = pkgs_spec(name, parents, pkgs_state)
        parent = parents.get(name)
        if parent:
            spec["contexts"] = {parent: f"target:{pkgs_target(targets, parent, arches, parents, pkgs_state)}"}
        targets[target] = {**spec, "platforms": [PLATFORMS[a] for a in arches], "output": ["type=cacheonly"]}
    return target


def add_target(targets, target, name, arches, parents, state, tags, output):
    """Adds the bake target pushing image `name`: the upgrade layer on its install layers."""
    pkgs_state, apt_state = state
    targets[target] = {"context": ".", "dockerfile-inline": UPGRADE_LAYER,
                       "contexts": {"pkgs": f"target:{pkgs_target(targets, name, arches, parents, pkgs_state)}"},
                       "args": {"APT": APT, "APT_STATE": apt_state},
                       "platforms": [PLATFORMS[a] for a in arches], "tags": tags, "output": output}


def image_targets(targets, name, arches, all_arches, parents, state):
    """
    Adds the bake targets for one image: X/<arch> for each arch, which CI also uses directly, and the
    multi-arch X.  They share every layer, so BuildKit builds each only once.
    """
    # Docker's v2 manifest formats rather than OCI's, as the registry has always served.
    output = ["type=cacheonly"] if options.no_push else ["type=registry,oci-mediatypes=false"]
    repos = [f"{options.registry}/{n}" for n in tag_names(name)]
    # CI pulls X on ARM too: a run missing an arch must leave X alone rather than replace it with a
    # list that lacks that arch.
    complete = arches == all_arches
    for a in arches:
        tags = [f"{repo}/{a}" for repo in repos] + (repos if complete and len(arches) == 1 else [])
        add_target(targets, f"{name}-{a}", name, [a], parents, state, tags, output)
    if complete and len(arches) > 1:
        add_target(targets, name, name, arches, parents, state, repos, output)


def unit_plan(distro, parents):
    """
    The distro's build units, in build order: (name, refresh days, [(image, arches)]).  First its own
    images, then each image built on them, together with the images built on that one.  bake cancels
    the rest of a run when one target fails, so each unit has its own run, and a broken one costs only
    itself.
    """
    _, _, distro_arches = DISTROS[distro]
    days = refresh_days(distro)
    units = [(distro, days, [(image, distro_arches) for image in distro_images(distro)])]
    for root in EXTRAS:
        root_distro = extra_distro(root, parents)
        if parents[root] in EXTRAS or ALIASES.get(root_distro, root_distro) != distro:
            continue
        members = [root]
        for member in members:
            members += [n for n in EXTRAS if parents[n] == member]
        units.append((root, EXTRAS[root].get('days', days), [(m, EXTRAS[m]['arches']) for m in members]))
    return units


def upgrades_available(image, rundir):
    """
    The packages apt would upgrade in the pushed image, as its `apt-get -s dist-upgrade` reports: the
    same apt, sources and pins the image's own build uses.  The multi-arch tag's amd64 image: that tag
    is pushed only when every arch built, where IMAGE/amd64 is pushed on its own and would read as
    current after a night whose ARM or i386 build failed.  amd64 stands for every arch: the archives
    move together, and an arch-specific update is caught the next time anything else moves.  Returns
    (what, problem): the packages, or why the image is treated as behind instead, and the check's log
    when it failed for a reason worth reporting (not for an image that isn't pushed yet).
    """
    ref = f"{options.registry}/{image}"
    out = os.path.join(rundir, 'checks', f"{image}.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # --entrypoint: clone's is its script.
    cmd = ['docker', 'run', '--rm', '--pull', 'always', '--platform', PLATFORMS['amd64'], '--entrypoint', 'sh',
           ref, '-c', f'{APT} update >/dev/null && {APT} -s dist-upgrade']
    with open(out, 'w') as f:
        f.write(f"=== {' '.join(cmd)}\n")
        f.flush()
        try:
            code = pulling(ref, lambda: run_stoppable(cmd, options.check_timeout * 60, stdout=f, stderr=subprocess.STDOUT))
        except subprocess.TimeoutExpired:
            code = None
    with open(out, errors='replace') as f:
        lines = f.read().splitlines()
    if code != 0:
        if any('manifest unknown' in line or 'not found' in line for line in lines):
            return ["not pushed yet"], None
        return [f"check failed ({'timed out' if code is None else f'exit {code}'})"], out
    return [line.split()[1] for line in lines if line.startswith(('Inst ', 'Remv '))], None


def bake_units(distro, arches, state, parents):
    """
    unit_plan's units as bake definitions: {unit name: (complete, definition, [(image, arches)])}, in
    build order.
    """
    definitions = {}
    for name, _, images in unit_plan(distro, parents):
        images = [(image, [a for a in all_arches if a in arches], all_arches) for image, all_arches in images]
        if not all(image_arches for _, image_arches, _ in images):
            continue
        targets = {}
        for image, image_arches, all_arches in images:
            image_targets(targets, image, image_arches, all_arches, parents, state)
        built = [t for t in targets if not t.startswith('pkgs-')]
        complete = all(image_arches == all_arches for _, image_arches, all_arches in images)
        definitions[name] = (complete, {"group": {"default": {"targets": built}}, "target": targets},
                             [(image, image_arches) for image, image_arches, _ in images])
    return definitions


def ensure_builder(rundir):
    """Creates the builder if it doesn't exist; returns the platforms it can build."""
    if subprocess.run(['docker', 'buildx', 'inspect', options.builder], capture_output=True, timeout=60).returncode != 0:
        config = os.path.join(rundir, 'buildkitd.toml')
        with open(config, 'w') as f:
            # Explicit, because BuildKit's defaults are shares of the disk: on a mostly full disk it
            # evicts the cache after every build, and an extra then rebuilds the distro it is built on.
            f.write(f'[worker.oci]\n  maxUsedSpace = "{options.cache_size}GB"\n'
                    f'  reservedSpace = "{options.cache_size // 2}GB"\n  minFreeSpace = "10GB"\n')
            if not options.registry_https:
                f.write(f'[registry."{options.registry}"]\n  http = true\n  insecure = true\n')
        # network=host so that a registry on localhost is the host's, not the builder container's.
        subprocess.run(['docker', 'buildx', 'create', '--name', options.builder, '--driver', 'docker-container',
                        '--driver-opt', 'network=host', '--buildkitd-config', config], check=True, timeout=300)
    out = subprocess.run(['docker', 'buildx', 'inspect', '--bootstrap', options.builder],
                         check=True, capture_output=True, text=True, timeout=600).stdout
    # A node's platforms set with `buildx create --platform` are printed with a trailing *.
    return {p.strip().rstrip('*') for line in out.splitlines() if line.strip().startswith('Platforms:')
            for p in line.split(':', 1)[1].split(',')}


# Without buildx's default provenance attestations, which add "unknown/unknown" platform entries to
# every pushed image list and that some clients list as an extra platform.
bake_env = {**os.environ, 'BUILDX_NO_DEFAULT_ATTESTATIONS': '1'}

# Set on SIGTERM (systemd's stop, or TimeoutStartSec running out): the running bakes are killed, nothing
# new starts, and the run still saves the schedule of what finished and reports what didn't.
stopping = threading.Event()
running = set()
running_lock = threading.Lock()


def stop(signum, frame):
    stopping.set()
    with running_lock:
        for process in running:
            process.terminate()


signal.signal(signal.SIGTERM, stop)


def run_stoppable(cmd, timeout, **kwargs):
    """Runs cmd, killed after `timeout` seconds (TimeoutExpired) or on SIGTERM; returns its exit code."""
    with subprocess.Popen(cmd, **kwargs) as process:
        with running_lock:
            running.add(process)
        if stopping.is_set():
            process.terminate()
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        finally:
            with running_lock:
                running.discard(process)


def run_bake(name, definition, rundir, retries=None):
    """Runs one bake, with retries.  Returns None on success, else its log."""
    if retries is None:
        retries = options.retries
    bakefile = os.path.join(rundir, f"{name}.bake.json")
    with open(bakefile, 'w') as f:
        json.dump(definition, f, indent=2)
    log = os.path.join(rundir, f"{name}.log")
    cmd = ['docker', 'buildx', 'bake', '--builder', options.builder, '--file', bakefile, '--progress', 'plain',
           *(['--no-cache'] if options.no_cache else [])]
    began = time.time()
    for attempt in range(retries + 1):
        if attempt:
            delay = options.retry_delay * 2 ** (attempt - 1)
            say(f"{name}: failed, retrying in {delay}s (see {log})")
            if stopping.wait(delay):
                break
        count = len(definition['group']['default']['targets'])
        say(f"{name}: building {count} targets" + (f", attempt {attempt + 1}" if attempt else ""))
        with open(os.devnull if options.debug else log, 'a') as f:
            f.write(f"\n=== attempt {attempt + 1}: {' '.join(cmd)}\n")
            f.flush()
            output = {} if options.debug else {'stdout': f, 'stderr': subprocess.STDOUT}
            try:
                code = run_stoppable(cmd, options.bake_timeout * 60, env=bake_env, **output)
            except subprocess.TimeoutExpired:
                say(f"{name}: attempt {attempt + 1} timed out after {options.bake_timeout} minutes")
                f.write(f"\n=== attempt {attempt + 1} killed after {options.bake_timeout} minutes\n")
                continue
        if code == 0:
            say(f"{name}: done in {time.time() - began:.0f}s")
            return None
        if stopping.is_set():
            break
    if stopping.is_set():
        say(f"{name}: stopped")
    if options.debug:
        say(f"{name}: FAILED, see the output above")
        return "(the output above)"
    say(f"{name}: FAILED, see {log}")
    return log


def load_schedule():
    """unit name => when it was last built and pushed on every arch."""
    try:
        with open(SCHEDULE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_schedule(built, when):
    schedule = load_schedule()
    schedule.update({name: when for name in built})
    tmp = SCHEDULE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(schedule, f, indent=2, sort_keys=True)
    os.replace(tmp, SCHEDULE_FILE)


def build_distro(distro, arches, rundir, parents, schedule):
    """
    Builds a distro's units that are due: its own images, then each extra unit.  Returns its failures
    as (what, log), and the units built and pushed on every arch.
    """
    due = []
    for name, days, _ in unit_plan(distro, parents):
        elapsed = time.time() - schedule.get(name, 0)
        if not options.scheduled or elapsed >= days * 86400 - DUE_SLACK:
            due.append(name)
        else:
            say(f"{name}: built {elapsed / 86400:.1f} days ago, refreshed every {days}; skipped")
    if not due or stopping.is_set():
        return [(f"{name} (not built: the run was stopped)", None) for name in due], []
    # The install layers are rebuilt once a week (the ISO week), the upgrade layer on every run; every
    # bake of a run shares both values, so the units rebuilt in it share their layers.
    state = (time.strftime('%G-W%V'), f"run-{time.strftime('%Y%m%dT%H%M%S')}")
    definitions = bake_units(distro, arches, state, parents)
    failures, built = [], []
    for name in due:
        if name not in definitions:
            continue
        if stopping.is_set():
            failures.append((f"{name} (not built: the run was stopped)", None))
            continue
        complete, definition, images = definitions[name]
        if not moving(distro) and not options.no_cache:
            checks = {}
            for image, _ in images:
                if stopping.is_set():
                    break
                checks[image] = upgrades_available(image, rundir)
            if stopping.is_set():
                failures.append((f"{name} (not built: the run was stopped)", None))
                continue
            # A failed check still rebuilds, and is reported: a host that can't check (the registry
            # missing from the daemon's insecure-registries, say) would otherwise rebuild everything
            # every night without anyone noticing.
            failures += [(f"{image}: apt check failed, rebuilt anyway", problem)
                         for image, (_, problem) in checks.items() if problem]
            behind = {image: what for image, (what, _) in checks.items() if what}
            if not behind:
                say(f"{name}: apt would upgrade nothing in {', '.join(image for image, _ in images)}; skipped")
                built += [name] if complete and not options.no_push else []
                continue
            say(f"{name}: rebuilding, " + "; ".join(f"{image}: {', '.join(what[:5])}" + (f" and {len(what) - 5} more" if len(what) > 5 else "")
                                                  for image, what in behind.items()))
        # Even after the distro's own images fail: each extra rebuilds the stages it needs through its
        # context, so it fails only if those are what broke.
        log = run_bake(name, definition, rundir)
        if log is not None:
            week = fallback_week(name, state[0], schedule)
            if week and not stopping.is_set():
                say(f"{name}: trying {week}'s install layers with tonight's upgrade")
                _, definition, _ = bake_units(distro, arches, (week, state[1]), parents)[name]
                if run_bake(f"{name}-{week}", definition, rundir, retries=0) is None:
                    failures.append((f"{name}: this week's install layers failed, pushed {week}'s "
                                     "with tonight's upgrade instead", log))
                    continue
            failures.append((name, log))
            continue
        built += [name] if complete and not options.no_push else []
    return failures, built


def fallback_week(name, week, schedule):
    """
    The install layers to build a unit on when this week's fail (a package uninstallable today): the
    week it was last pushed, whose layers are still in the builder's cache, so only the upgrade layer
    builds.  None when that is this week: the failure is elsewhere.  The fallback is not recorded as
    a push, so the next run tries this week's layers again.
    """
    last = schedule.get(name, time.time() - 7 * 86400)
    last = time.strftime('%G-W%V', time.localtime(last))
    return last if last != week else None


def failure_report(failures):
    header = "\n".join([f"**Session CI image rebuild on {socket.gethostname()}: {len(failures)} failure(s)**"]
                       + [f"- `{what}`" + (f" (log: `{log}`)" if log else "") for what, log in failures])
    first_log = next((log for _, log in failures if log and os.path.isfile(log)), None)
    if not first_log:
        return header
    with open(first_log, errors='replace') as f:
        lines = f.read().splitlines()
    # BuildKit's own summary of what failed, else the end of the log.
    errors = [l for l in lines if l.startswith('ERROR') or ' ERROR: ' in l] or lines[-25:]
    tail = "\n".join(errors[-25:])
    budget = 1900 - len(header)
    return header if budget < 200 else f"{header}\n```\n{tail[-budget:]}\n```"


def new_rundir():
    """This run's directory for its bake files and logs; deletes runs older than KEEP_LOGS_DAYS."""
    os.makedirs(options.log_dir, exist_ok=True)
    for entry in os.scandir(options.log_dir):
        if entry.is_dir() and entry.name.startswith('run-') \
                and time.time() - entry.stat().st_mtime > KEEP_LOGS_DAYS * 86400:
            shutil.rmtree(entry.path, ignore_errors=True)
    return os.path.abspath(tempfile.mkdtemp(prefix=f"run-{time.strftime('%Y%m%dT%H%M%S')}-", dir=options.log_dir))


def main():
    asked = [d.partition('/')[::2] for d in options.distro]
    for name, arch in asked:
        if name not in DISTROS or (arch and arch not in PLATFORMS):
            parser.error(f"bad --distro {name}/{arch}; distros: {', '.join(DISTROS)}; arches: {', '.join(PLATFORMS)}")
    parents = extra_parents()
    wanted_arches = [a for a in options.arch.split(',') if a] or list(PLATFORMS)
    if not set(wanted_arches) <= set(PLATFORMS):
        parser.error(f"--arch takes {', '.join(PLATFORMS)}")
    ALIASES.update(find_aliases())
    requested = {}  # distro => arches asked for with --distro
    for name, arch in asked:
        requested.setdefault(ALIASES.get(name, name), set()).update([arch] if arch else PLATFORMS)
    distros = list(requested) or [d for d in DISTROS if d not in ALIASES]

    failures = []
    if options.print_only:
        supported = set(PLATFORMS.values())
    else:
        rundir = new_rundir()
        supported = ensure_builder(rundir)
        missing = [a for a in wanted_arches if PLATFORMS[a] not in supported]
        if missing:
            # Never skipped silently: a builder that lost its ARM node would otherwise stop
            # refreshing those images without anyone noticing.
            failures.append((f"builder {options.builder} cannot build {', '.join(missing)}; "
                             "add a node for them (see the README) or pass --arch", None))
            wanted_arches = [a for a in wanted_arches if a not in missing]

    plans = {d: [a for a in DISTROS[d][2] if a in wanted_arches and a in requested.get(d, PLATFORMS)]
             for d in distros}
    plans = {d: arches for d, arches in plans.items() if arches}

    if options.print_only:
        for d, arches in plans.items():
            for name, (_, definition, _) in bake_units(d, arches, ("<pkgs state>", "<apt state>"), parents).items():
                print(f"# {name}")
                print(json.dumps(definition, indent=2))
        return 0

    say(f"building {len(plans)} distro(s) for {', '.join(wanted_arches)}; logs in {rundir}")
    if any(arches != DISTROS[d][2] for d, arches in plans.items()):
        say("not every arch is built, so the multi-arch tags are left as they are")
    run_started = time.time()
    schedule = load_schedule()
    with ThreadPoolExecutor(max_workers=1 if options.debug else max(options.parallel, 1)) as executor:
        results = {d: executor.submit(build_distro, d, arches, rundir, parents, schedule)
                   for d, arches in plans.items()}
    built = []
    for distro, future in results.items():
        try:
            distro_failures, distro_built = future.result()
        except Exception:
            trace = traceback.format_exc()
            print(trace, file=sys.stderr)
            distro_failures, distro_built = [(f"{distro} (crashed: {trace.strip().splitlines()[-1]})", None)], []
        failures += distro_failures
        built += distro_built
    # Only the timer's runs keep the schedule: a manual run doesn't change when the next refresh is due.
    if options.scheduled:
        save_schedule(built, run_started)
    if stopping.is_set():
        failures.insert(0, ("the run was stopped by SIGTERM", None))

    if failures:
        print("\nFailed:", file=sys.stderr)
        for what, log in failures:
            print(f"  {what}" + (f"  (log: {log})" if log else ""), file=sys.stderr)
        notify_discord(failure_report(failures))
        return 1
    say("all done")
    return 0


sys.exit(main())
