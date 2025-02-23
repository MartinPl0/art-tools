from future import standard_library

import artcommonlib.util

standard_library.install_aliases()
from contextlib import contextmanager
from collections import namedtuple

import os
import tempfile
import shutil
import atexit
import datetime
import yaml
import click
import logging
import traceback
import urllib.parse
import signal
import io
import pathlib
from typing import Optional, List, Dict, Tuple, Union
import time
import re

from jira import JIRA

from artcommonlib.runtime import GroupRuntime
from doozerlib import gitdata
from . import logutil
from . import assertion
from . import exectools
from . import dblib
from .pushd import Dir

from .image import ImageMetadata
from .rpmcfg import RPMMetadata
from doozerlib import state
from .model import Model, Missing
from multiprocessing import Lock, RLock, Semaphore
from .repos import Repos
from doozerlib.exceptions import DoozerFatalError
from doozerlib import constants
from doozerlib import util
from doozerlib import brew
from doozerlib.assembly import assembly_group_config, assembly_basis_event, assembly_type, AssemblyTypes, assembly_streams_config
from doozerlib.build_status_detector import BuildStatusDetector

# Values corresponds to schema for group.yml: freeze_automation. When
# 'yes', doozer itself will inhibit build/rebase related activity
# (exiting with an error if someone tries). Other values can
# be interpreted & enforced by the build pipelines (e.g. by
# invoking config:read-config).
FREEZE_AUTOMATION_YES = 'yes'
FREEZE_AUTOMATION_SCHEDULED = 'scheduled'  # inform the pipeline that only manually run tasks should be permitted
FREEZE_AUTOMATION_NO = 'no'


# doozer cancel brew builds on SIGINT (Ctrl-C)
# but Jenkins sends a SIGTERM when cancelling a job.
def handle_sigterm(*_):
    raise KeyboardInterrupt()


signal.signal(signal.SIGTERM, handle_sigterm)


# Registered atexit to close out debug/record logs
def close_file(f):
    f.close()


def remove_tmp_working_dir(runtime):
    if runtime.remove_tmp_working_dir:
        shutil.rmtree(runtime.working_dir)
    else:
        click.echo("Temporary working directory preserved by operation: %s" % runtime.working_dir)


# A named tuple for caching the result of Runtime._resolve_source.
SourceResolution = namedtuple('SourceResolution', [
    'source_path', 'url', 'branch', 'public_upstream_url', 'public_upstream_branch'
])


class Runtime(GroupRuntime):
    # Use any time it is necessary to synchronize feedback from multiple threads.
    mutex = RLock()

    # Serialize access to the shared koji session
    koji_lock = RLock()

    # Build status detector lock
    bs_lock = RLock()

    # Serialize access to the console, and record log
    log_lock = Lock()

    def __init__(self, **kwargs):
        # initialize defaults in case no value is given
        self.verbose = False
        self.quiet = False
        self.load_wip = False
        self.load_disabled = False
        self.data_path = None
        self.data_dir = None
        self.latest_parent_version = False
        self.rhpkg_config = None
        self._koji_client_session = None
        self.db = None
        self.session_pool = {}
        self.session_pool_available = {}
        self.brew_event = None
        self.assembly_basis_event = None
        self.assembly_type = None
        self.releases_config = None
        self.assembly = 'test'
        self._build_status_detector = None
        self.disable_gssapi = False
        self._build_data_product_cache: Model = None

        self.stream: List[str] = []  # Click option. A list of image stream overrides from the command line.
        self.stream_overrides: Dict[str, str] = {}  # Dict of stream name -> pullspec from command line.

        self.upstreams: List[str] = []  # Click option. A list of upstream source commit to use.
        self.upstream_commitish_overrides: Dict[str, str] = {}  # Dict from distgit key name to upstream source commit to use.

        self.downstreams: List[str] = []  # Click option. A list of distgit commits to checkout.
        self.downstream_commitish_overrides: Dict[str, str] = {}  # Dict from distgit key name to distgit commit to check out.

        # See get_named_semaphore. The empty string key serves as a lock for the data structure.
        self.named_semaphores = {'': Lock()}

        for key, val in kwargs.items():
            self.__dict__[key] = val

        if self.latest_parent_version:
            self.ignore_missing_base = True

        self._remove_tmp_working_dir = False
        self._group_config = None

        self.cwd = os.getcwd()

        # If source needs to be cloned by oit directly, the directory in which it will be placed.
        self.sources_dir = None

        self.distgits_dir = None

        self.record_log = None
        self.record_log_path = None

        self.debug_log_path = None

        self.brew_logs_dir = None

        self.flags_dir = None

        # Map of dist-git repo name -> ImageMetadata object. Populated when group is set.
        self.image_map: Dict[str, ImageMetadata] = {}

        # Map of dist-git repo name -> RPMMetadata object. Populated when group is set.
        self.rpm_map: Dict[str, RPMMetadata] = {}

        # Maps component name to the Image or RPM Metadata responsible for the component
        self.component_map: Dict[str, Union[ImageMetadata, RPMMetadata]] = dict()

        # Map of source code repo aliases (e.g. "ose") to a tuple representing the source resolution cache.
        # See registry_repo.
        self.source_resolutions = {}

        # Map of source code repo aliases (e.g. "ose") to a (public_upstream_url, public_upstream_branch) tuple.
        # See registry_repo.
        self.public_upstreams = {}

        self.initialized = False

        # Will be loaded with the streams.yml Model
        self.streams = Model(dict_to_model={})

        self.uuid = None

        # Optionally available if self.fetch_rpms_for_tag() is called
        self.rpm_list = None
        self.rpm_search_tree = None

        # Used for image build ordering
        self.image_tree = {}
        self.image_order = []
        # allows mapping from name or distgit to meta
        self.image_name_map = {}
        # allows mapping from name in bundle to meta
        self.name_in_bundle_map: Dict[str, ImageMetadata] = {}

        # holds untouched group config
        self.raw_group_config = {}

        # Used to capture missing packages for 4.x build
        self.missing_pkgs = set()

        # Whether to prevent builds for this group. Defaults to 'no'.
        self.freeze_automation = FREEZE_AUTOMATION_NO

        self.rhpkg_config_lst = []
        if self.rhpkg_config:
            if not os.path.isfile(self.rhpkg_config):
                raise DoozerFatalError('--rhpkg-config option given is not a valid file! {}'.format(self.rhpkg_config))
            self.rhpkg_config = ' --config {} '.format(self.rhpkg_config)
            self.rhpkg_config_lst = self.rhpkg_config.split()
        else:
            self.rhpkg_config = ''

    def get_named_semaphore(self, lock_name, is_dir=False, count=1):
        """
        Returns a semaphore (which can be used as a context manager). The first time a lock_name
        is received, a new semaphore will be established. Subsequent uses of that lock_name will
        receive the same semaphore.
        :param lock_name: A unique name for resource threads are contending over. If using a directory name
                            as a lock_name, provide an absolute path.
        :param is_dir: The lock_name is a directory (method will ignore things like trailing slashes)
        :param count: The number of times the lock can be claimed. Default=1, which is a full mutex.
        :return: A semaphore associated with the lock_name.
        """
        with self.named_semaphores['']:
            if is_dir:
                p = '_dir::' + str(pathlib.Path(str(lock_name)).absolute())  # normalize (e.g. strip trailing /)
            else:
                p = lock_name
            if p in self.named_semaphores:
                return self.named_semaphores[p]
            else:
                new_semaphore = Semaphore(count)
                self.named_semaphores[p] = new_semaphore
                return new_semaphore

    def get_releases_config(self):
        if self.releases_config is not None:
            return self.releases_config

        load = self.gitdata.load_data(key='releases')
        data = load.data if load else {}
        if self.releases:  # override filename specified on command line.
            rcp = pathlib.Path(self.releases)
            data = yaml.safe_load(rcp.read_text())

        if load:
            self.releases_config = Model(data)
        else:
            self.releases_config = Model()

        return self.releases_config

    @property
    def group_config(self):
        return self._group_config

    @group_config.setter
    def group_config(self, config: Model):
        self._group_config = config

    def get_group_config(self) -> Model:
        # group.yml can contain a `vars` section which should be a
        # single level dict containing keys to str.format(**dict) replace
        # into the YAML content. If `vars` found, the format will be
        # preformed and the YAML model will reloaded from that result
        tmp_config = Model(self.gitdata.load_data(key='group').data)
        replace_vars = self._get_replace_vars(tmp_config)
        try:
            group_yml = yaml.safe_dump(tmp_config.primitive(), default_flow_style=False)
            raw_group_config = yaml.full_load(group_yml.format(**replace_vars))
            tmp_config = Model(dict(raw_group_config))
        except KeyError as e:
            raise ValueError('group.yml contains template key `{}` but no value was provided'.format(e.args[0]))

        return assembly_group_config(self.get_releases_config(), self.assembly, tmp_config)

    def get_errata_config(self, **kwargs):
        return self.gitdata.load_data(key='erratatool', **kwargs).data

    def _get_replace_vars(self, group_config: Model):
        replace_vars = group_config.vars or Model()
        # If assembly mode is enabled, `runtime_assembly` will become the assembly name.
        replace_vars['runtime_assembly'] = ''
        # If running against an assembly for a named release, release_name will become the release name.
        replace_vars['release_name'] = ''
        if self.assembly:
            replace_vars['runtime_assembly'] = self.assembly
            if self.assembly_type is not AssemblyTypes.STREAM:
                replace_vars['release_name'] = util.get_release_name_for_assembly(self.group, self.get_releases_config(), self.assembly)
        return replace_vars

    def init_state(self):
        self.state = dict(state.TEMPLATE_BASE_STATE)
        if os.path.isfile(self.state_file):
            with io.open(self.state_file, 'r', encoding='utf-8') as f:
                self.state = yaml.full_load(f)
            self.state.update(state.TEMPLATE_BASE_STATE)

    def save_state(self):
        with io.open(self.state_file, 'w', encoding='utf-8') as f:
            yaml.safe_dump(self.state, f, default_flow_style=False)

    def initialize(self, mode='images', clone_distgits=True,
                   validate_content_sets=False,
                   no_group=False, clone_source=None, disabled=None,
                   prevent_cloning: bool = False, config_only: bool = False, group_only: bool = False):

        if self.initialized:
            return

        if self.quiet and self.verbose:
            click.echo("Flags --quiet and --verbose are mutually exclusive")
            exit(1)

        self.mode = mode

        # We could mark these as required and the click library would do this for us,
        # but this seems to prevent getting help from the various commands (unless you
        # specify the required parameters). This can probably be solved more cleanly, but TODO
        if not no_group and self.group is None:
            click.echo("Group must be specified")
            exit(1)

        if self.lock_runtime_uuid:
            self.uuid = self.lock_runtime_uuid
        else:
            self.uuid = datetime.datetime.now().strftime("%Y%m%d.%H%M%S")

        if self.working_dir is None:
            self.working_dir = tempfile.mkdtemp(".tmp", "oit-")
            # This can be set to False by operations which want the working directory to be left around
            self.remove_tmp_working_dir = True
            atexit.register(remove_tmp_working_dir, self)
        else:
            self.working_dir = os.path.abspath(os.path.expanduser(self.working_dir))
            if not os.path.isdir(self.working_dir):
                os.makedirs(self.working_dir)

        self.distgits_dir = os.path.join(self.working_dir, "distgits")
        self.distgits_diff_dir = os.path.join(self.working_dir, "distgits-diffs")
        self.sources_dir = os.path.join(self.working_dir, "sources")
        self.record_log_path = os.path.join(self.working_dir, "record.log")
        self.brew_logs_dir = os.path.join(self.working_dir, "brew-logs")
        self.flags_dir = os.path.join(self.working_dir, "flags")
        self.state_file = os.path.join(self.working_dir, 'state.yaml')
        self.debug_log_path = os.path.join(self.working_dir, "debug.log")

        if self.upcycle:
            # A working directory may be upcycle'd numerous times.
            # Don't let anything grow unbounded.
            shutil.rmtree(self.brew_logs_dir, ignore_errors=True)
            shutil.rmtree(self.flags_dir, ignore_errors=True)
            for path in (self.record_log_path, self.state_file, self.debug_log_path):
                if os.path.exists(path):
                    os.unlink(path)

        if not os.path.isdir(self.distgits_dir):
            os.mkdir(self.distgits_dir)

        if not os.path.isdir(self.distgits_diff_dir):
            os.mkdir(self.distgits_diff_dir)

        if not os.path.isdir(self.sources_dir):
            os.mkdir(self.sources_dir)

        if disabled is not None:
            self.load_disabled = disabled

        self.initialize_logging()

        self.init_state()

        try:
            self.db = dblib.DB(self, self.datastore)
        except Exception as err:
            self.logger.warning('Cannot connect to the DB: %s\n%s', str(err), traceback.format_exc())

        self.logger.info(f'Initial execution (cwd) directory: {os.getcwd()}')

        if no_group:
            return  # nothing past here should be run without a group

        if '@' in self.group:
            self.group, self.group_commitish = self.group.split('@', 1)
        else:
            self.group_commitish = self.group

        if group_only:
            return

        # For each "--stream alias image" on the command line, register its existence with
        # the runtime.
        for s in self.stream:
            self.register_stream_override(s[0], s[1])

        for upstream in self.upstreams:
            override_distgit_key = upstream[0]
            override_commitish = upstream[1]
            self.logger.warning(f'Upstream source for {override_distgit_key} being set to {override_commitish}')
            self.upstream_commitish_overrides[override_distgit_key] = override_commitish

        for upstream in self.downstreams:
            override_distgit_key = upstream[0]
            override_commitish = upstream[1]
            self.logger.warning(f'Downstream distgit for {override_distgit_key} will be checked out to {override_commitish}')
            self.downstream_commitish_overrides[override_distgit_key] = override_commitish

        self.resolve_metadata()

        self.record_log = io.open(self.record_log_path, 'a', encoding='utf-8')
        atexit.register(close_file, self.record_log)

        # Directory where brew-logs will be downloaded after a build
        if not os.path.isdir(self.brew_logs_dir):
            os.mkdir(self.brew_logs_dir)

        # Directory for flags between invocations in the same working-dir
        if not os.path.isdir(self.flags_dir):
            os.mkdir(self.flags_dir)

        if self.cache_dir:
            self.cache_dir = os.path.abspath(self.cache_dir)

        # get_releases_config also inits self.releases_config
        self.assembly_type = assembly_type(self.get_releases_config(), self.assembly)

        self.group_dir = self.gitdata.data_dir
        self.group_config = self.get_group_config()

        self.hotfix = False  # True indicates builds should be tagged with associated hotfix tag for the artifacts branch

        if self.group_config.assemblies.enabled or self.enable_assemblies:
            if re.fullmatch(r'[\w.]+', self.assembly) is None or self.assembly[0] == '.' or self.assembly[-1] == '.':
                raise ValueError('Assembly names may only consist of alphanumerics, ., and _, but not start or end with a dot (.).')
        else:
            # If assemblies are not enabled for the group,
            # ignore this argument throughout doozer.
            self.assembly = None

        replace_vars = self._get_replace_vars(self.group_config).primitive()

        # only initialize group and assembly configs and nothing else
        if config_only:
            return

        # Read in the streams definition for this group if one exists
        streams_data = self.gitdata.load_data(key='streams', replace_vars=replace_vars)
        if streams_data:
            org_stream_model = Model(dict_to_model=streams_data.data)
            self.streams = assembly_streams_config(self.get_releases_config(), self.assembly, org_stream_model)

        self.assembly_basis_event = assembly_basis_event(self.get_releases_config(), self.assembly)
        if self.assembly_basis_event:
            if self.brew_event:
                raise IOError(f'Cannot run with assembly basis event {self.assembly_basis_event} and --brew-event at the same time.')
            # If the assembly has a basis event, we constrain all brew calls to that event.
            self.brew_event = self.assembly_basis_event
            self.logger.info(f'Constraining brew event to assembly basis for {self.assembly}: {self.brew_event}')

        # This flag indicates builds should be tagged with associated hotfix tag for the artifacts branch
        self.hotfix = self.assembly_type is not AssemblyTypes.STREAM

        if not self.brew_event:
            self.logger.info("Basis brew event is not set. Using the latest event....")
            with self.shared_koji_client_session() as koji_session:
                # If brew event is not set as part of the assembly and not specified on the command line,
                # lock in an event so that there are no race conditions.
                self.logger.info("Getting the latest event....")
                event_info = koji_session.getLastEvent()
                self.brew_event = event_info['id']

        # register the sources
        # For each "--source alias path" on the command line, register its existence with
        # the runtime.
        for r in self.source:
            self.register_source_alias(r[0], r[1])

        if self.sources:
            with io.open(self.sources, 'r', encoding='utf-8') as sf:
                source_dict = yaml.full_load(sf)
                if not isinstance(source_dict, dict):
                    raise ValueError('--sources param must be a yaml file containing a single dict.')
                for key, val in source_dict.items():
                    self.register_source_alias(key, val)

        with Dir(self.group_dir):

            # Flattens multiple comma/space delimited lists like [ 'x', 'y,z' ] into [ 'x', 'y', 'z' ]
            def flatten_list(names):
                if not names:
                    return []
                # split csv values
                result = []
                for n in names:
                    result.append([x for x in n.replace(' ', ',').split(',') if x != ''])
                # flatten result and remove dupes using set
                return list(set([y for x in result for y in x]))

            def filter_wip(n, d):
                return d.get('mode', 'enabled') in ['wip', 'enabled']

            def filter_enabled(n, d):
                return d.get('mode', 'enabled') == 'enabled'

            def filter_disabled(n, d):
                return d.get('mode', 'enabled') in ['enabled', 'disabled']

            cli_arches_override = flatten_list(self.arches)

            if cli_arches_override:  # Highest priority overrides on command line
                self.arches = cli_arches_override
            elif self.group_config.arches_override:  # Allow arches_override in group.yaml to temporarily override GA architectures
                self.arches = self.group_config.arches_override
            else:
                self.arches = self.group_config.get('arches', ['x86_64'])

            # If specified, signed repo files will be generated to enforce signature checks.
            self.gpgcheck = self.group_config.build_profiles.image.signed.gpgcheck
            if self.gpgcheck is Missing:
                # We should only really be building the latest release with unsigned RPMs, so default to True
                self.gpgcheck = True

            self.repos = Repos(self.group_config.repos, self.arches, self.gpgcheck)
            self.freeze_automation = self.group_config.freeze_automation or FREEZE_AUTOMATION_NO

            if validate_content_sets:
                # as of 2023-06-09 authentication is required to validate content sets with rhsm-pulp
                if not os.environ.get("RHSM_PULP_KEY") or not os.environ.get("RHSM_PULP_CERT"):
                    self.logger.warn("Missing RHSM_PULP auth, will skip validating content sets")
                else:
                    self.repos.validate_content_sets()

            if self.group_config.name != self.group:
                raise IOError(
                    f"Name in group.yml ({self.group_config.name}) does not match group name ({self.group}). Someone "
                    "may have copied this group without updating group.yml (make sure to check branch)")

            if self.branch is None:
                if self.group_config.branch is not Missing:
                    self.branch = self.group_config.branch
                    self.logger.info("Using branch from group.yml: %s" % self.branch)
                else:
                    self.logger.info("No branch specified either in group.yml or on the command line; all included images will need to specify their own.")
            else:
                self.logger.info("Using branch from command line: %s" % self.branch)

            scanner = self.group_config.image_build_log_scanner
            if scanner is not Missing:
                # compile regexen and fail early if they don't
                regexen = []
                for val in scanner.matches:
                    try:
                        regexen.append(re.compile(val))
                    except Exception as e:
                        raise ValueError(
                            "could not compile image build log regex for group:\n{}\n{}"
                            .format(val, e)
                        )
                scanner.matches = regexen

            exclude_keys = flatten_list(self.exclude)
            image_ex = list(exclude_keys)
            rpm_ex = list(exclude_keys)
            image_keys = flatten_list(self.images)

            rpm_keys = flatten_list(self.rpms)

            filter_func = None
            if self.load_wip and self.load_disabled:
                pass  # use no filter, load all
            elif self.load_wip:
                filter_func = filter_wip
            elif self.load_disabled:
                filter_func = filter_disabled
            else:
                filter_func = filter_enabled

            # pre-load the image data to get the names for all images
            # eventually we can use this to allow loading images by
            # name or distgit. For now this is used elsewhere
            image_name_data = self.gitdata.load_data(path='images')

            def _register_name_in_bundle(name_in_bundle: str, distgit_key: str):
                if name_in_bundle in self.name_in_bundle_map:
                    raise ValueError(f"Image {distgit_key} has name_in_bundle={name_in_bundle}, which is already taken by image {self.name_in_bundle_map[name_in_bundle]}")
                self.name_in_bundle_map[name_in_bundle] = img.key

            for img in image_name_data.values():
                name = img.data.get('name')
                short_name = name.split('/')[1]
                self.image_name_map[name] = img.key
                self.image_name_map[short_name] = img.key
                name_in_bundle = img.data.get('name_in_bundle')
                if name_in_bundle:
                    _register_name_in_bundle(name_in_bundle, img.key)
                else:
                    short_name_without_ose = short_name[4:] if short_name.startswith("ose-") else short_name
                    _register_name_in_bundle(short_name_without_ose, img.key)
                    short_name_with_ose = "ose-" + short_name_without_ose
                    _register_name_in_bundle(short_name_with_ose, img.key)

            image_data = self.gitdata.load_data(path='images', keys=image_keys,
                                                exclude=image_ex,
                                                replace_vars=replace_vars,
                                                filter_funcs=None if len(image_keys) else filter_func)

            try:
                rpm_data = self.gitdata.load_data(path='rpms', keys=rpm_keys,
                                                  exclude=rpm_ex,
                                                  replace_vars=replace_vars,
                                                  filter_funcs=None if len(rpm_keys) else filter_func)
            except gitdata.GitDataPathException:
                # some older versions have no RPMs, that's ok.
                rpm_data = {}

            missed_include = set(image_keys + rpm_keys) - set(list(image_data.keys()) + list(rpm_data.keys()))
            if len(missed_include) > 0:
                raise DoozerFatalError('The following images or rpms were either missing or filtered out: {}'.format(', '.join(missed_include)))

            if mode in ['images', 'both']:
                for i in image_data.values():
                    if i.key not in self.image_map:
                        metadata = ImageMetadata(self, i, self.upstream_commitish_overrides.get(i.key), clone_source=clone_source, prevent_cloning=prevent_cloning)
                        self.image_map[metadata.distgit_key] = metadata
                        self.component_map[metadata.get_component_name()] = metadata
                if not self.image_map:
                    self.logger.warning("No image metadata directories found for given options within: {}".format(self.group_dir))

                for image in self.image_map.values():
                    image.resolve_parent()

                # now that ancestry is defined, make sure no cyclic dependencies
                for image in self.image_map.values():
                    for child in image.children:
                        if image.is_ancestor(child):
                            raise DoozerFatalError('{} cannot be both a parent and dependent of {}'.format(child.distgit_key, image.distgit_key))

                self.generate_image_tree()

            if mode in ['rpms', 'both']:
                for r in rpm_data.values():
                    if clone_source is None:
                        # Historically, clone_source defaulted to True for rpms.
                        clone_source = True
                    metadata = RPMMetadata(self, r, self.upstream_commitish_overrides.get(r.key), clone_source=clone_source, prevent_cloning=prevent_cloning)
                    self.rpm_map[metadata.distgit_key] = metadata
                    self.component_map[metadata.get_component_name()] = metadata
                if not self.rpm_map:
                    self.logger.warning("No rpm metadata directories found for given options within: {}".format(self.group_dir))

        # Make sure that the metadata is not asking us to check out the same exact distgit & branch.
        # This would almost always indicate someone has checked in duplicate metadata into a group.
        no_collide_check = {}
        for meta in list(self.rpm_map.values()) + list(self.image_map.values()):
            key = '{}/{}/#{}'.format(meta.namespace, meta.name, meta.branch())
            if key in no_collide_check:
                raise IOError('Complete duplicate distgit & branch; something wrong with metadata: {} from {} and {}'.format(key, meta.config_filename, no_collide_check[key].config_filename))
            no_collide_check[key] = meta

        if clone_distgits:
            self.clone_distgits()

        self.initialized = True

    def initialize_logging(self):

        if self.initialized:
            return

        # Three flags control the output modes of the command:
        # --verbose prints logs to CLI as well as to files
        # --debug increases the log level to produce more detailed internal
        #         behavior logging
        # --quiet opposes both verbose and debug
        if self.debug:
            log_level = logging.DEBUG
        elif self.quiet:
            log_level = logging.WARN
        else:
            log_level = logging.INFO

        default_log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.WARN)
        root_stream_handler = logging.StreamHandler()
        root_stream_handler.setFormatter(default_log_formatter)
        root_logger.addHandler(root_stream_handler)

        # If in debug mode, let all modules log
        if not self.debug:
            # Otherwise, only allow children of ocp to log
            root_logger.addFilter(logging.Filter("ocp"))

        # Get a reference to the logger for doozer
        self.logger = logutil.getLogger()
        self.logger.propagate = False

        # levels will be set at the handler level. Make sure master level is low.
        self.logger.setLevel(logging.DEBUG)

        main_stream_handler = logging.StreamHandler()
        main_stream_handler.setFormatter(default_log_formatter)
        main_stream_handler.setLevel(log_level)
        self.logger.addHandler(main_stream_handler)

        debug_log_handler = logging.FileHandler(self.debug_log_path)
        # Add thread information for debug log
        debug_log_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s (%(thread)d) %(message)s'))
        debug_log_handler.setLevel(logging.DEBUG)
        self.logger.addHandler(debug_log_handler)

    def build_jira_client(self) -> JIRA:
        """
        :return: Returns a JIRA client setup for the server in bug.yaml
        """
        major, minor = self.get_major_minor_fields()
        if major == 4 and minor < 6:
            raise ValueError("ocp-build-data/bug.yml is not expected to be available for 4.X versions < 4.6")
        bug_config = Model(self.gitdata.load_data(key='bug').data)
        server = bug_config.jira_config.server or 'https://issues.redhat.com'

        token_auth = os.environ.get("JIRA_TOKEN")
        if not token_auth:
            raise ValueError(f"Jira activity requires login credentials for {server}. Set a JIRA_TOKEN env var")
        client = JIRA(server, token_auth=token_auth)
        return client

    def build_retrying_koji_client(self):
        """
        :return: Returns a new koji client instance that will automatically retry
        methods when it receives common exceptions (e.g. Connection Reset)
        Honors doozer --brew-event.
        """
        return brew.KojiWrapper([self.group_config.urls.brewhub], brew_event=self.brew_event)

    @contextmanager
    def shared_koji_client_session(self):
        """
        Context manager which offers a shared koji client session. You hold a koji specific lock in this context
        manager giving your thread exclusive access. The lock is reentrant, so don't worry about
        call a method that acquires the same lock while you hold it.
        Honors doozer --brew-event.
        Do not rerun gssapi_login on this client. We've observed client instability when this happens.
        """
        with self.koji_lock:
            if self._koji_client_session is None:
                self._koji_client_session = self.build_retrying_koji_client()
                if not self.disable_gssapi:
                    self.logger.info("Authenticating to Brew...")
                    self._koji_client_session.gssapi_login()
            yield self._koji_client_session

    @contextmanager
    def shared_build_status_detector(self) -> 'BuildStatusDetector':
        """
        Yields a shared build status detector within context.
        """
        with self.bs_lock:
            if self._build_status_detector is None:
                self._build_status_detector = BuildStatusDetector(self, self.logger)
            yield self._build_status_detector

    @contextmanager
    def pooled_koji_client_session(self, caching: bool = False):
        """
        Context manager which offers a koji client session from a limited pool. You hold a lock on this
        session until you return. It is not recommended to call other methods that acquire their
        own pooled sessions, because that may lead to deadlock if the pool is exhausted.
        Honors doozer --brew-event.
        :param caching: Set to True in order for your instance to place calls/results into
                        the global KojiWrapper cache. This is equivalent to passing
                        KojiWrapperOpts(caching=True) in each call within the session context.
        """
        session = None
        session_id = None
        while True:
            with self.mutex:
                if len(self.session_pool_available) == 0:
                    if len(self.session_pool) < 30:
                        # pool has not grown to max size;
                        new_session = self.build_retrying_koji_client()
                        session_id = len(self.session_pool)
                        self.session_pool[session_id] = new_session
                        session = new_session  # This is what we wil hand to the caller
                        break
                    else:
                        # Caller is just going to have to wait and try again
                        pass
                else:
                    session_id, session = self.session_pool_available.popitem()
                    break

            time.sleep(5)

        # Arriving here, we have a session to use.
        try:
            session.force_instance_caching = caching
            yield session
        finally:
            session.force_instance_caching = False
            # Put it back into the pool
            with self.mutex:
                self.session_pool_available[session_id] = session

    @staticmethod
    def timestamp():
        return datetime.datetime.utcnow().isoformat()

    def assert_mutation_is_permitted(self):
        """
        In group.yml, it is possible to instruct doozer to prevent all builds / mutation of distgits.
        Call this method if you are about to mutate anything. If builds are disabled, an exception will
        be thrown.
        """
        if self.freeze_automation == FREEZE_AUTOMATION_YES:
            raise DoozerFatalError('Automation (builds / mutations) for this group is currently frozen (freeze_automation set to {}). Coordinate with the group owner to change this if you believe it is incorrect.'.format(FREEZE_AUTOMATION_YES))

    def image_metas(self) -> List[ImageMetadata]:
        return list(self.image_map.values())

    def ordered_image_metas(self) -> List[ImageMetadata]:
        return [self.image_map[dg] for dg in self.image_order]

    def get_global_arches(self):
        """
        :return: Returns a list of architectures that are enabled globally in group.yml.
        """
        return list(self.arches)

    def get_product_config(self) -> Model:
        """
        Returns a Model of the product.yml in ocp-build-data main branch.
        """
        if self._build_data_product_cache:
            return self._build_data_product_cache
        url = 'https://raw.githubusercontent.com/openshift-eng/ocp-build-data/main/product.yml'
        req = urllib.request.Request(url)
        req.add_header('Accept', 'application/yaml')
        self._build_data_product_cache = Model(yaml.safe_load(exectools.urlopen_assert(req).read()))
        return self._build_data_product_cache

    def filter_failed_image_trees(self, failed):
        for i in self.ordered_image_metas():
            if i.parent and i.parent.distgit_key in failed:
                failed.append(i.distgit_key)

        for f in failed:
            if f in self.image_map:
                del self.image_map[f]

        # regen order and tree
        self.generate_image_tree()

        return failed

    def generate_image_tree(self):
        self.image_tree = {}
        image_lists = {0: []}

        def add_child_branch(child, branch, level=1):
            if level not in image_lists:
                image_lists[level] = []
            for sub_child in child.children:
                if sub_child.distgit_key not in self.image_map:
                    continue  # don't add images that have been filtered out
                branch[sub_child.distgit_key] = {}
                image_lists[level].append(sub_child.distgit_key)
                add_child_branch(sub_child, branch[sub_child.distgit_key], level + 1)

        for image in self.image_map.values():
            if not image.parent:
                self.image_tree[image.distgit_key] = {}
                image_lists[0].append(image.distgit_key)
                add_child_branch(image, self.image_tree[image.distgit_key])

        levels = list(image_lists.keys())
        levels.sort()
        self.image_order = []
        for level in levels:
            for i in image_lists[level]:
                if i not in self.image_order:
                    self.image_order.append(i)

    def image_distgit_by_name(self, name):
        """Returns image meta by full name, short name, or distgit"""
        return self.image_name_map.get(name, None)

    def rpm_metas(self) -> List[RPMMetadata]:
        return list(self.rpm_map.values())

    def all_metas(self) -> List[Union[ImageMetadata, RPMMetadata]]:
        return self.image_metas() + self.rpm_metas()

    def get_payload_image_metas(self) -> List[ImageMetadata]:
        """
        :return: Returns a list of ImageMetadata that are destined for the OCP release payload. Payload images must
                    follow the correct naming convention or an exception will be thrown.
        """
        payload_images = []
        for image_meta in self.image_metas():
            if image_meta.is_payload:
                """
                <Tim Bielawa> note to self: is only for `ose-` prefixed images
                <Clayton Coleman> Yes, Get with the naming system or get out of town
                """
                if not image_meta.image_name_short.startswith("ose-"):
                    raise ValueError(f"{image_meta.distgit_key} does not conform to payload naming convention with image name: {image_meta.image_name_short}")

                payload_images.append(image_meta)

        return payload_images

    def get_for_release_image_metas(self) -> List[ImageMetadata]:
        """
        :return: Returns a list of ImageMetada which are configured to be released by errata.
        """
        return filter(lambda meta: meta.for_release, self.image_metas())

    def get_non_release_image_metas(self) -> List[ImageMetadata]:
        """
        :return: Returns a list of ImageMetada which are not meant to be released by errata.
        """
        return filter(lambda meta: not meta.for_release, self.image_metas())

    def register_source_alias(self, alias, path):
        self.logger.info("Registering source alias %s: %s" % (alias, path))
        path = os.path.abspath(path)
        assertion.isdir(path, "Error registering source alias %s" % alias)
        with Dir(path):
            url = None
            origin_url = "?"
            rc1, out_origin, err_origin = exectools.cmd_gather(
                ["git", "config", "--get", "remote.origin.url"])
            if rc1 == 0:
                url = out_origin.strip()
                origin_url = url
                # Usually something like "git@github.com:openshift/origin.git"
                # But we want an https hyperlink like http://github.com/openshift/origin
                if origin_url.startswith("git@"):
                    origin_url = origin_url[4:]  # remove git@
                    origin_url = origin_url.replace(":", "/", 1)  # replace first colon with /

                    if origin_url.endswith(".git"):
                        origin_url = origin_url[:-4]  # remove .git

                    origin_url = "https://%s" % origin_url
            else:
                self.logger.error("Failed acquiring origin url for source alias %s: %s" % (alias, err_origin))

            branch = None
            rc2, out_branch, err_branch = exectools.cmd_gather(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"])
            if rc2 == 0:
                branch = out_branch.strip()
            else:
                self.logger.error("Failed acquiring origin branch for source alias %s: %s" % (alias, err_branch))

            if self.group_config.public_upstreams:
                if not (url and branch):
                    raise DoozerFatalError(f"Couldn't detect source URL or branch for local source {path}. Is it a valid Git repo?")
                public_upstream_url, public_upstream_branch = self.get_public_upstream(url)
                if branch == 'HEAD':
                    # If branch == HEAD, our source is a detached HEAD.
                    public_upstream_url = None
                    public_upstream_branch = None
                else:
                    if not public_upstream_branch:
                        public_upstream_branch = branch
                self.source_resolutions[alias] = SourceResolution(path, url, branch, public_upstream_url, public_upstream_branch)
            else:
                self.source_resolutions[alias] = SourceResolution(path, url, branch, None, None)

            if 'source_alias' not in self.state:
                self.state['source_alias'] = {}
            self.state['source_alias'][alias] = {
                'url': origin_url,
                'branch': branch or '?',
                'path': path
            }
            self.add_record("source_alias", alias=alias, origin_url=origin_url, branch=branch or '?', path=path)

    def register_stream_override(self, name, image):
        self.logger.info("Registering image stream name override %s: %s" % (name, image))
        self.stream_overrides[name] = image

    @property
    def remove_tmp_working_dir(self):
        """
        Provides thread safe method of checking whether runtime should clean up the working directory.
        :return: Returns True if the directory should be deleted
        """
        with self.log_lock:
            return self._remove_tmp_working_dir

    @remove_tmp_working_dir.setter
    def remove_tmp_working_dir(self, remove):
        """
        Provides thread safe method of setting whether runtime should clean up the working directory.
        :param remove: True if the directory should be removed. Only the last value set impacts the decision.
        """
        with self.log_lock:
            self._remove_tmp_working_dir = remove

    def add_record(self, record_type, **kwargs):
        """
        Records an action taken by oit that needs to be communicated to outside
        systems. For example, the update a Dockerfile which needs to be
        reviewed by an owner. Each record is encoded on a single line in the
        record.log. Records cannot contain line feeds -- if you need to
        communicate multi-line data, create a record with a path to a file in
        the working directory.

        :param record_type: The type of record to create.
        :param kwargs: key/value pairs

        A record line is designed to be easily parsed and formatted as:
        record_type|key1=value1|key2=value2|...|
        """

        # Multiple image build processes could be calling us with action simultaneously, so
        # synchronize output to the file.
        with self.log_lock:
            record = "%s|" % record_type
            for k, v in kwargs.items():
                assert ("\n" not in str(k))
                # Make sure the values have no linefeeds as this would interfere with simple parsing.
                v = str(v).replace("\n", " ;;; ").replace("\r", "")
                record += "%s=%s|" % (k, v)

            # Add the record to the file
            self.record_log.write("%s\n" % record)
            self.record_log.flush()

    def add_distgits_diff(self, distgit, diff):
        """
        Records the diff of changes applied to a distgit repo.
        """

        with io.open(os.path.join(self.distgits_diff_dir, distgit + '.patch'), 'w', encoding='utf-8') as f:
            f.write(diff)

    def resolve_image(self, distgit_name, required=True):
        """
        Returns an ImageMetadata for the specified group member name.
        :param distgit_name: The name of an image member in this group
        :param required: If True, raise an exception if the member is not found.
        :return: The ImageMetadata object associated with the name
        """
        if distgit_name not in self.image_map:
            if not required:
                return None
            raise DoozerFatalError("Unable to find image metadata in group / included images: %s" % distgit_name)
        return self.image_map[distgit_name]

    def late_resolve_image(self, distgit_name, add=False, required=True):
        """Resolve image and retrieve meta, optionally adding to image_map.
        If image not found, error will be thrown
        :param distgit_name: Distgit key
        :param add: Add the image to image_map
        :param required: If False, return None if the image is not enabled
        :return: Image meta
        """

        if distgit_name in self.image_map:
            return self.image_map[distgit_name]

        replace_vars = self._get_replace_vars(self.group_config).primitive()
        data_obj = self.gitdata.load_data(path='images', key=distgit_name, replace_vars=replace_vars)
        if not data_obj:
            raise DoozerFatalError('Unable to resolve image metadata for {}'.format(distgit_name))

        mode = data_obj.data.get("mode", "enabled")
        if mode == "disabled" and not self.load_disabled or mode == "wip" and not self.load_wip:
            if required:
                raise DoozerFatalError('Attempted to load image {} but it has mode {}'.format(distgit_name, mode))
            self.logger.warning("Image %s will not be loaded because it has mode %s", distgit_name, mode)
            return None

        meta = ImageMetadata(self, data_obj, self.upstream_commitish_overrides.get(data_obj.key))
        if add:
            self.image_map[distgit_name] = meta
        self.component_map[meta.get_component_name()] = meta
        return meta

    def resolve_brew_image_url(self, image_name_and_version):
        """
        :param image_name_and_version: The image name to resolve. The image can contain a version tag or sha.
        :return: Returns the pullspec of this image in brew.
        e.g. "openshift/jenkins:5"  => "registry-proxy.engineering.redhat.com/rh-osbs/openshift-jenkins:5"
        """

        if self.group_config.urls.brew_image_host in image_name_and_version:
            # Seems like a full brew url already
            url = image_name_and_version
        elif self.group_config.urls.brew_image_namespace is not Missing:
            # if there is a namespace, we need to flatten the image name.
            # e.g. openshift/image:latest => openshift-image:latest
            # ref: https://source.redhat.com/groups/public/container-build-system/container_build_system_wiki/pulling_pre_quay_switch_over_osbs_built_container_images_using_the_osbs_registry_proxy
            url = self.group_config.urls.brew_image_host
            ns = self.group_config.urls.brew_image_namespace
            name = image_name_and_version.replace('/', '-')
            url = "/".join((url, ns, name))
        else:
            # If there is no namespace, just add the image name to the brew image host
            url = "/".join((self.group_config.urls.brew_image_host, image_name_and_version))

        if ':' not in url.split('/')[-1]:
            # oc image info will return information about all tagged images. So be explicit
            # in indicating :latest if there is no tag.
            url += ':latest'

        return url

    def resolve_stream(self, stream_name):
        """
        :param stream_name: The name of the stream to resolve.
        :return: Resolves and returns the image stream name into its literal value.
                This is usually a lookup in streams.yml, but can also be overridden on the command line. If
                the stream_name cannot be resolved, an exception is thrown.
        """

        # If the stream has an override from the command line, return it.
        if stream_name in self.stream_overrides:
            return Model(dict_to_model={'image': self.stream_overrides[stream_name]})

        if stream_name not in self.streams:
            raise IOError("Unable to find definition for stream: %s" % stream_name)

        return self.streams[stream_name]

    def get_stream_names(self):
        """
        :return: Returns a list of all streams defined in streams.yaml.
        """
        return list(self.streams.keys())

    def get_public_upstream(self, remote_git: str) -> (str, Optional[str]):
        """
        Some upstream repo are private in order to allow CVE workflows. While we
        may want to build from a private upstream, we don't necessarily want to confuse
        end-users by referencing it in our public facing image labels / etc.
        In group.yaml, you can specify a mapping in "public_upstreams". It
        represents private_url_prefix => public_url_prefix. Remote URLs passed to this
        method which contain one of the private url prefixes will be translated
        into a new string with the public prefix in its place. If there is not
        applicable mapping, the incoming url will still be normalized into https.
        :param remote_git: The URL to analyze for private repo patterns.
        :return: tuple (url, branch)
            - url: An https normalized remote address with private repo information replaced. If there is no
                   applicable private repo replacement, remote_git will be returned (normalized to https).
            - branch: Optional public branch name if the public upstream source use a different branch name from the private upstream.
        """
        remote_https = artcommonlib.util.convert_remote_git_to_https(remote_git)

        if self.group_config.public_upstreams:

            # We prefer the longest match in the mapping, so iterate through the entire
            # map and keep track of the longest matching private remote.
            target_priv_prefix = None
            target_pub_prefix = None
            target_pub_branch = None
            for upstream in self.group_config.public_upstreams:
                priv = upstream["private"]
                pub = upstream["public"]
                # priv can be a full repo, or an organization (e.g. git@github.com:openshift)
                # It will be treated as a prefix to be replaced
                https_priv_prefix = artcommonlib.util.convert_remote_git_to_https(priv)  # Normalize whatever is specified in group.yaml
                https_pub_prefix = artcommonlib.util.convert_remote_git_to_https(pub)
                if remote_https.startswith(f'{https_priv_prefix}/') or remote_https == https_priv_prefix:
                    # If we have not set the prefix yet, or if it is longer than the current contender
                    if not target_priv_prefix or len(https_priv_prefix) > len(target_pub_prefix):
                        target_priv_prefix = https_priv_prefix
                        target_pub_prefix = https_pub_prefix
                        target_pub_branch = upstream.get("public_branch")

            if target_priv_prefix:
                return f'{target_pub_prefix}{remote_https[len(target_priv_prefix):]}', target_pub_branch

        return remote_https, None

    def git_clone(self, remote_url, target_dir, gitargs=None, set_env=None, timeout=0):
        gitargs = gitargs or []
        set_env = set_env or []

        if self.cache_dir:
            git_cache_dir = os.path.join(self.cache_dir, self.user or "default", 'git')
            util.mkdirs(git_cache_dir)
            normalized_url = artcommonlib.util.convert_remote_git_to_https(remote_url)
            # Strip special chars out of normalized url to create a human friendly, but unique filename
            file_friendly_url = normalized_url.split('//')[-1].replace('/', '_')
            repo_dir = os.path.join(git_cache_dir, file_friendly_url)
            self.logger.info(f'Cache for {remote_url} going to {repo_dir}')

            if not os.path.exists(repo_dir):
                self.logger.info(f'Initializing cache directory for git remote: {remote_url}')

                # If the cache directory for this repo does not exist yet, we will create one.
                # But we must do so carefully to minimize races with any other doozer instance
                # running on the machine.
                with self.get_named_semaphore(repo_dir, is_dir=True):  # also make sure we cooperate with other threads in this process.
                    tmp_repo_dir = tempfile.mkdtemp(dir=git_cache_dir)
                    exectools.cmd_assert(f'git init --bare {tmp_repo_dir}')
                    with Dir(tmp_repo_dir):
                        exectools.cmd_assert(f'git remote add origin {remote_url}')

                    try:
                        os.rename(tmp_repo_dir, repo_dir)
                    except:
                        # There are two categories of failure
                        # 1. Another doozer instance already created the directory, in which case we are good to go.
                        # 2. Something unexpected is preventing the rename.
                        if not os.path.exists(repo_dir):
                            # Not sure why the rename failed. Raise to user.
                            raise

            # If we get here, we have a bare repo with a remote set
            # Pull content to update the cache. This should be safe for multiple doozer instances to perform.
            self.logger.info(f'Updating cache directory for git remote: {remote_url}')
            # Fire and forget this fetch -- just used to keep cache as fresh as possible
            exectools.fire_and_forget(repo_dir, 'git fetch --all')
            gitargs.extend(['--dissociate', '--reference-if-able', repo_dir])

        gitargs.append('--recurse-submodules')

        self.logger.info(f'Cloning to: {target_dir}')

        # Perform the clone (including --reference args if cache_dir was set)
        cmd = []
        if timeout:
            cmd.extend(['timeout', f'{timeout}'])
        cmd.extend(['git', 'clone', remote_url])
        cmd.extend(gitargs)
        cmd.append(target_dir)
        exectools.cmd_assert(cmd, retries=3, on_retry=["rm", "-rf", target_dir], set_env=set_env)

    def is_branch_commit_hash(self, branch):
        """
        When building custom assemblies, it is sometimes useful to
        pin upstream sources to specific git commits. This cannot
        be done with standard assemblies which should be built from
        branches.
        :param branch: A branch name in rpm or image metadata.
        :returns: Returns True if the specified branch name is actually a commit hash for a custom assembly.
        """
        if len(branch) >= 7:  # The hash must be sufficiently unique
            try:
                int(branch, 16)   # A hash must be a valid hex number
                return True
            except ValueError:
                pass
        return False

    def resolve_source(self, meta):
        """
        Looks up a source alias and returns a path to the directory containing
        that source. Sources can be specified on the command line, or, failing
        that, in group.yml.
        If a source specified in group.yaml has not be resolved before,
        this method will clone that source to checkout the group's desired
        branch before returning a path to the cloned repo.
        :param meta: The MetaData object to resolve source for
        :return: Returns the source path or None if upstream source is not defined
        """
        source = meta.config.content.source

        if not source:
            return None

        parent = f'{meta.namespace}_{meta.name}'

        # This allows passing `--source <distgit_key> path` to
        # override any source to something local without it
        # having been configured for an alias
        if self.local and meta.distgit_key in self.source_resolutions:
            source['alias'] = meta.distgit_key
            if 'git' in source:
                del source['git']

        source_details = None
        if 'git' in source:
            git_url = urllib.parse.urlparse(source.git.url)
            name = os.path.splitext(os.path.basename(git_url.path))[0]
            alias = '{}_{}'.format(parent, name)
            source_details = dict(source.git)
        elif 'alias' in source:
            alias = source.alias
        else:
            return None

        self.logger.debug("Resolving local source directory for alias {}".format(alias))
        if alias in self.source_resolutions:
            path, _, _, meta.public_upstream_url, meta.public_upstream_branch = self.source_resolutions[alias]
            self.logger.debug("returning previously resolved path for alias {}: {}".format(alias, path))
            return path

        # Where the source will land, check early so we know if old or new style
        sub_path = '{}{}'.format('global_' if source_details is None else '', alias)
        source_dir = os.path.join(self.sources_dir, sub_path)

        if not source_details:  # old style alias was given
            if self.group_config.sources is Missing or alias not in self.group_config.sources:
                raise DoozerFatalError("Source alias not found in specified sources or in the current group: %s" % alias)
            source_details = self.group_config.sources[alias]

        self.logger.debug("checking for source directory in source_dir: {}".format(source_dir))

        with self.get_named_semaphore(source_dir, is_dir=True):
            if alias in self.source_resolutions:  # we checked before, but check again inside the lock
                path, _, _, meta.public_upstream_url, meta.public_upstream_branch = self.source_resolutions[alias]
                self.logger.debug("returning previously resolved path for alias {}: {}".format(alias, path))
                return path

            # If this source has already been extracted for this working directory
            if os.path.isdir(source_dir):
                # Store so that the next attempt to resolve the source hits the map
                self.register_source_alias(alias, source_dir)
                if self.group_config.public_upstreams:
                    _, _, _, meta.public_upstream_url, meta.public_upstream_branch = self.source_resolutions[alias]
                self.logger.info("Source '{}' already exists in (skipping clone): {}".format(alias, source_dir))
                if self.upcycle:
                    self.logger.info("Refreshing source for '{}' due to --upcycle: {}".format(alias, source_dir))
                    with Dir(source_dir):
                        exectools.cmd_assert('git fetch --all', retries=3)
                        exectools.cmd_assert('git reset --hard @{upstream}', retries=3)
                return source_dir

            if meta.prevent_cloning:
                raise IOError(f'Attempt to clone upstream {meta.distgit_key} after cloning disabled; a regression has been introduced.')

            url = source_details["url"]
            clone_branch, _ = self.detect_remote_source_branch(source_details)
            if self.group_config.public_upstreams:
                meta.public_upstream_url, meta.public_upstream_branch = self.get_public_upstream(url)
                if not meta.public_upstream_branch:  # default to the same branch name as private upstream
                    meta.public_upstream_branch = clone_branch

            self.logger.info("Attempting to checkout source '%s' branch %s in: %s" % (url, clone_branch, source_dir))
            try:
                # clone all branches as we must sometimes reference master /OWNERS for maintainer information
                if self.is_branch_commit_hash(branch=clone_branch):
                    gitargs = []
                else:
                    gitargs = ['--no-single-branch', '--branch', clone_branch]

                self.git_clone(url, source_dir, gitargs=gitargs, set_env=constants.GIT_NO_PROMPTS)

                if self.is_branch_commit_hash(branch=clone_branch):
                    with Dir(source_dir):
                        exectools.cmd_assert(f'git checkout {clone_branch}')

                # fetch public upstream source
                if meta.public_upstream_branch:
                    util.setup_and_fetch_public_upstream_source(meta.public_upstream_url, meta.public_upstream_branch, source_dir)

            except IOError as e:
                self.logger.info("Unable to checkout branch {}: {}".format(clone_branch, str(e)))
                shutil.rmtree(source_dir)
                raise DoozerFatalError("Error checking out target branch of source '%s' in: %s" % (alias, source_dir))

            # Store so that the next attempt to resolve the source hits the map
            self.register_source_alias(alias, source_dir)

            if meta.commitish:
                # With the alias registered, check out the commit we want
                self.logger.info(f"Determining if commit-ish {meta.commitish} exists")
                cmd = ["git", "-C", source_dir, "branch", "--contains", meta.commitish]
                exectools.cmd_assert(cmd)
                self.logger.info(f"Checking out commit-ish {meta.commitish}")
                exectools.cmd_assert(["git", "-C", source_dir, "checkout", meta.commitish])

            return source_dir

    def detect_remote_source_branch(self, source_details):
        """Find a configured source branch that exists, or raise DoozerFatalError. Returns branch name and git hash"""
        git_url = source_details["url"]
        branches = source_details["branch"]

        branch = branches["target"]  # This is a misnomer as it can also be a git commit hash an not just a branch name.
        fallback_branch = branches.get("fallback", None)
        if self.group_config.use_source_fallback_branch == "always" and fallback_branch:
            # only use the fallback (unless none is given)
            branch, fallback_branch = fallback_branch, None
        elif self.group_config.use_source_fallback_branch == "never":
            # ignore the fallback
            fallback_branch = None
        stage_branch = branches.get("stage", None) if self.stage else None

        if stage_branch:
            self.logger.info('Normal branch overridden by --stage option, using "{}"'.format(stage_branch))
            result = self._get_remote_branch_ref(git_url, stage_branch)
            if result:
                return stage_branch, result
            raise DoozerFatalError('--stage option specified and no stage branch named "{}" exists for {}'.format(stage_branch, git_url))

        if self.is_branch_commit_hash(branch):
            return branch, branch

        result = self._get_remote_branch_ref(git_url, branch)
        if result:
            return branch, result
        elif not fallback_branch:
            raise DoozerFatalError('Requested target branch {} does not exist and no fallback provided'.format(branch))

        self.logger.info('Target branch does not exist in {}, checking fallback branch {}'.format(git_url, fallback_branch))
        result = self._get_remote_branch_ref(git_url, fallback_branch)
        if result:
            return fallback_branch, result
        raise DoozerFatalError('Requested fallback branch {} does not exist'.format(branch))

    def _get_remote_branch_ref(self, git_url, branch):
        """
        Detect whether a single branch exists on a remote repo; returns git hash if found
        :param git_url: The URL to the git repo to check.
        :param branch: The name of the branch. If the name is not a branch and appears to be a commit
                hash, the hash will be returned without modification.
        """
        self.logger.info('Checking if target branch {} exists in {}'.format(branch, git_url))

        try:
            out, _ = exectools.cmd_assert('git ls-remote --heads {} {}'.format(git_url, branch), retries=3)
        except Exception as err:
            # We don't expect and exception if the branch does not exist; just an empty string
            self.logger.error('Error attempting to find target branch {} hash: {}'.format(branch, err))
            return None
        result = out.strip()  # any result means the branch is found; e.g. "7e66b10fbcd6bb4988275ffad0a69f563695901f	refs/heads/some_branch")
        if not result and self.is_branch_commit_hash(branch):
            return branch  # It is valid hex; just return it

        return result.split()[0] if result else None

    def resolve_source_head(self, meta):
        """
        Attempts to resolve the branch a given source alias has checked out. If not on a branch
        returns SHA of head.
        :param meta: The MetaData object to resolve source for
        :return: The name of the checked out branch or None (if required=False)
        """
        source_dir = self.resolve_source(meta)

        if not source_dir:
            return None

        with io.open(os.path.join(source_dir, '.git/HEAD'), encoding="utf-8") as f:
            head_content = f.read().strip()
            # This will either be:
            # a SHA like: "52edbcd8945af0dc728ad20f53dcd78c7478e8c2"
            # a local branch name like: "ref: refs/heads/master"
            if head_content.startswith("ref:"):
                return head_content.split('/', 2)[2]  # limit split in case branch name contains /

            # Otherwise, just return SHA
            return head_content

    def export_sources(self, output):
        self.logger.info('Writing sources to {}'.format(output))
        with io.open(output, 'w', encoding='utf-8') as sources_file:
            yaml.dump({k: v.path for k, v in self.source_resolutions.items()}, sources_file, default_flow_style=False)

    def auto_version(self, repo_type):
        """
        Find and return the version of the atomic-openshift package in the OCP
        RPM repository.

        This repository is the primary input for OCP images.  The group_config
        for a group specifies the location for both signed and unsigned
        rpms.  The caller must indicate which to use.
        """

        repo_url = self.repos['rhel-server-ose-rpms'].baseurl(repo_type, 'x86_64')
        self.logger.info(
            "Getting version from atomic-openshift package in {}".format(
                repo_url)
        )

        # create a randomish repo name to avoid erroneous cache hits
        repoid = "oit" + datetime.datetime.now().strftime("%s")
        version_query = ["/usr/bin/repoquery", "--quiet", "--tempcache",
                         "--repoid", repoid,
                         "--repofrompath", repoid + "," + repo_url,
                         "--queryformat", "%{VERSION}",
                         "atomic-openshift"]
        rc, auto_version, err = exectools.cmd_gather(version_query)
        if rc != 0:
            raise RuntimeError(
                "Unable to get OCP version from RPM repository: {}".format(err)
            )

        version = "v" + auto_version.strip()

        self.logger.info("Auto-detected OCP version: {}".format(version))
        return version

    def valid_version(self, version):
        """
        Check if a version string matches an accepted pattern.
        A single lower-case 'v' followed by one or more decimal numbers,
        separated by a dot.  Examples below are not exhaustive
        Valid:
          v1, v12, v3.4, v2.12.0

        Not Valid:
          1, v1..2, av3.4, .v12  .99.12, v13-55
        """
        return re.match(r"^v\d+((\.\d+)+)?$", version) is not None

    def clone_distgits(self, n_threads=None):
        with util.timer(self.logger.info, 'Full runtime clone'):
            if n_threads is None:
                n_threads = self.global_opts['distgit_threads']
            return exectools.parallel_exec(
                lambda m, _: m.distgit_repo(),
                self.all_metas(),
                n_threads=n_threads).get()

    def push_distgits(self, n_threads=None):
        self.assert_mutation_is_permitted()

        if n_threads is None:
            n_threads = self.global_opts['distgit_threads']
        return exectools.parallel_exec(
            lambda m, _: m.distgit_repo().push(),
            self.all_metas(),
            n_threads=n_threads).get()

    def get_el_targeted_default_branch(self, el_target: Optional[Union[str, int]] = None):
        if not self.branch:
            return None
        if not el_target:
            return self.branch
        # Otherwise, the caller is asking us to determine the branch for
        # a specific RHEL version. Pull apart the default group branch
        # and replace it wth the targeted version.
        el_ver: int = util.isolate_el_version_in_brew_tag(el_target)
        match = re.match(r'^(.*)rhel-\d+(.*)$', self.branch)
        el_specific_branch: str = f'{match.group(1)}rhel-{el_ver}{match.group(2)}'
        return el_specific_branch

    def get_default_candidate_brew_tag(self, el_target: Optional[Union[str, int]] = None):
        branch = self.get_el_targeted_default_branch(el_target=el_target)
        return branch + '-candidate' if branch else None

    def get_default_hotfix_brew_tag(self, el_target: Optional[Union[str, int]] = None):
        branch = self.get_el_targeted_default_branch(el_target=el_target)
        return branch + '-hotfix' if branch else None

    def get_candidate_brew_tags(self):
        """Return a set of known candidate tags relevant to this group"""
        tag = self.get_default_candidate_brew_tag()
        # assumptions here:
        # releases with default rhel-7 tag also have rhel 8.
        # releases with default rhel-8 tag do not also care about rhel-7.
        # adjust as needed (and just imagine rhel 9)!
        return {tag, tag.replace('-rhel-7', '-rhel-8')} if tag else set()

    def get_minor_version(self) -> str:
        """
        Returns: "<MAJOR>.<MINOR>" if the vars are defined in the group config.
        """
        return '.'.join(str(self.group_config.vars[v]) for v in ('MAJOR', 'MINOR'))

    def get_major_minor_fields(self) -> Tuple[int, int]:
        """
        Returns: (int(MAJOR), int(MINOR)) if the vars are defined in the group config.
        """
        major = int(self.group_config.vars['MAJOR'])
        minor = int(self.group_config.vars['MINOR'])
        return major, minor

    def resolve_metadata(self):
        """
        The group control data can be on a local filesystem, in a git
        repository that can be checked out, or some day in a database

        If the scheme is empty, assume file:///...
        Allow http, https, ssh and ssh+git (all valid git clone URLs)
        """

        if self.data_path is None:
            raise DoozerFatalError(
                ("No metadata path provided. Must be set via one of:\n"
                 "* data_path key in {}\n"
                 "* doozer --data-path [PATH|URL]\n"
                 "* Environment variable DOOZER_DATA_PATH\n"
                 ).format(self.cfg_obj.full_path))

        self.gitdata = gitdata.GitData(data_path=self.data_path, clone_dir=self.working_dir,
                                       commitish=self.group_commitish, reclone=self.upcycle, logger=self.logger)
        self.data_dir = self.gitdata.data_dir

    def get_rpm_config(self) -> dict:
        config = {}
        for key, val in self.rpm_map.items():
            config[key] = val.raw_config
        return config
