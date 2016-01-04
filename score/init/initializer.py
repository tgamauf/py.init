# Copyright © 2015 STRG.AT GmbH, Vienna, Austria
#
# This file is part of the The SCORE Framework.
#
# The SCORE Framework and all its parts are free software: you can redistribute
# them and/or modify them under the terms of the GNU Lesser General Public
# License version 3 as published by the Free Software Foundation which is in the
# file named COPYING.LESSER.txt.
#
# The SCORE Framework and all its parts are distributed without any WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. For more details see the GNU Lesser General Public
# License.
#
# If you have not received a copy of the GNU Lesser General Public License see
# http://www.gnu.org/licenses/.
#
# The License-Agreement realised between you as Licensee and STRG.AT GmbH as
# Licenser including the issue of its valid conclusion and its pre- and
# post-contractual effects is governed by the laws of Austria. Any disputes
# concerning this License-Agreement including the issue of its valid conclusion
# and its pre- and post-contractual effects are exclusively decided by the
# competent court, in whose district STRG.AT GmbH has its registered seat, at
# the discretion of STRG.AT GmbH also the competent court, in whose district the
# Licensee has his registered seat, an establishment or assets.

import abc
import importlib
from inspect import signature, Parameter
import logging
import networkx as nx
from .config import parse_list, parse_config_file
from .exceptions import InitializationError, ConfigurationError, DependencyLoop
from collections import OrderedDict


log = logging.getLogger(__name__)


def init(confdict, *, overrides={}, init_logging=True):
    """
    This function automates the process of initializing all other modules. It
    will operate on given *confdict*, which is expected to be a 2-dimensional
    `dict` mapping names of modules to their respective :term:`confdicts
    <confdict>`. The recommended way of acquiring such a confdict is through
    :func:`.parse_config_file`, but any 2-dimensional `dict` is fine.

    The *confdict* should also contain the configuration for this module, which
    interprets the configuration key ``modules`` (which should be accessible as
    ``confdict['score.init']['modules']``):

    :confkey:`modules` :faint:`[optional]`
        A list of module names that shall be initialized. If this value is
        missing, you will end up with an empty :class:`.ConfiguredScore` object.

    The provided *overrides* will be integrated into the actual *confdict*
    prior to initialization. While the confdict is assumed to be retrieved from
    external resources (like a configuration file), this parameter aims to make
    programmatic adjustment of the configuration a bit easier.

    The final parameter *init_logging* makes sure python's own logging
    facility is initialized with the provided configuration, too.

    This function returns a :class:`.ConfiguredScore` object.
    """
    if init_logging and 'formatters' in confdict:
        import logging.config
        # TODO: the fileConfig() function below expects a RawConfigParser
        # instance -> convert the confdict if it is not an object of that type
        logging.config.fileConfig(confdict, disable_existing_loggers=False)
    for section in overrides:
        if section not in confdict:
            confdict[section] = {}
        for key, value in overrides[section].items():
            confdict[section][key] = value
    return _init(confdict)


def _init(confdict):
    try:
        modconf = parse_list(confdict['score.init']['modules'])
    except KeyError:
        return ConfiguredScore(confdict, dict())
    modules = {}
    for line in modconf:
        name = line[line.rindex('.') + 1:]
        modules[name] = line
    dependency_map = _collect_dependencies(modules)
    initialized = dict()
    for module in _sorted_dependency_map(dependency_map, 'initialization'):
        module_dependencies = dependency_map[module]
        modconf = {}
        if module in confdict:
            modconf = confdict[module]
        kwargs = {}
        for dep in module_dependencies:
            argname = next(k for k, v in modules.items() if v == dep)
            kwargs[argname] = initialized[dep]
        log.debug('Initializing %s' % module)
        conf = importlib.import_module(module).init(modconf, **kwargs)
        if not isinstance(conf, ConfiguredModule):
            raise InitializationError(
                __package__,
                '%s initializer did not return ConfiguredModule but %s' %
                (module, repr(conf)))
        initialized[module] = conf
    return ConfiguredScore(confdict, initialized)


def init_from_file(file, *, overrides={}, init_logging=True):
    """
    Reads configuration from given *file* using
    :func:`.config.parse_config_file` and initializes score using :func:`.init`.
    See the documentation of :func:`.init` for a description of all keyword
    arguments.
    """
    return init(parse_config_file(file),
                overrides=overrides,
                init_logging=init_logging)


def init_logging_from_file(file):
    """
    Just the part of :func:`.init_from_file` that would initialize logging.
    """
    import logging.config
    confdict = parse_config_file(file)
    if 'formatters' in confdict:
        logging.config.fileConfig(confdict, disable_existing_loggers=False)


class ConfiguredModule(metaclass=abc.ABCMeta):
    """
    The return value of an ``init`` function. This class is abstract and
    modules must create sub-classes containing their respective configuration.
    """

    def __init__(self, module):
        self._module = module

    def _finalize(self, score):
        """
        The final function that will be called before the score initialization
        is considered complete. The parameter *score* contains the
        :class:`.ConfiguredScore` object.
        """
        pass

    @property
    def log(self):
        try:
            return self._log
        except AttributeError:
            self._log = logging.getLogger(self._module)
            return self._log


class AwaitFinalization(Exception):

    def __init__(self, modules):
        self.modules = modules


class ConfiguredScore(ConfiguredModule):
    """
    The return value of :func:`.init`. Contains the resulting
    :class:`.ConfiguredModule` of every initialized module as a member. It is
    also possible to access configured modules as a dictionary value:

    >>> conf.ctx == conf['score.ctx']
    """

    def __init__(self, confdict, modules):
        ConfiguredModule.__init__(self, __package__)
        self.conf = {}
        for section in confdict:
            self.conf[section] = dict(confdict[section].items())
        self._modules = modules
        for name, conf in modules.items():
            if name.startswith('score.'):
                setattr(self, name[6:], conf)
            else:
                setattr(self, name, conf)

    def _finalize(self):
        dependency_map = {}
        # start out by finalizing the modules in the same order they were
        # initialized
        for name, conf in self._modules.items():
            try:
                conf._finalize(self)
                conf._finalized = True
            except AwaitFinalization as e:
                if not e.modules:
                    raise InitializationError(
                        conf._module,
                        'Module raised AwaitFinalization with an empty '
                        'modules list') from e
                # TODO: ConfiguredModule -> string
                unknowns = set(e.modules) - set(self._modules.keys())
                if unknowns:
                    raise InitializationError(
                        conf._module,
                        'Module awaits the finalization of some ' +
                        'modules that were not configured:\n - ' +
                        '\n - '.join(unknowns)) from e
                dependency_map[name] = e.modules
        for name in _sorted_dependency_map(dependency_map, 'finalization'):
            conf = self._modules[name]
            try:
                conf._finalize()
                conf._finalized = True
            except AwaitFinalization as e:
                raise InitializationError(
                    conf._module,
                    'Module changed its finalization dependencies') from e

    def __hasitem__(self, module):
        return module in self._modules

    def __getitem__(self, module):
        return self._modules[module]


def _collect_dependencies(modules):
    missing = []
    dependency_map = dict()
    for name, modname in modules.items():
        if modname == 'score.init':
            continue
        try:
            module = importlib.import_module(modname)
        except ImportError:
            missing.append(modname)
            continue
        if not hasattr(module, 'init'):
            raise InitializationError(
                __package__,
                'Cannot initialize %s: it has no init() function' % modname)
        if not callable(module.init):
            raise InitializationError(
                __package__,
                'Cannot initialize %s: its init is not a function' % modname)
        module_dependencies = []
        sig = signature(module.init)
        for i, (param_name, param) in enumerate(sig.parameters.items()):
            if i == 0:
                # this should be the confdict
                continue
            module_dependencies.append(
                (param_name, param.default != Parameter.empty))
        dependency_map[modname] = module_dependencies
    if missing:
        raise ConfigurationError(
            __package__,
            'Could not find the following modules:\n - ' +
            '\n - '.join(missing))
    _remove_missing_optional_dependencies(modules, dependency_map)
    return dependency_map


def _remove_missing_optional_dependencies(modules, dependency_map):
    missing = {}
    for name, module_dependencies in dependency_map.items():
        newdeps = []
        for dependency, is_optional in module_dependencies:
            if dependency in modules:
                newdeps.append(modules[dependency])
                continue
            if is_optional:
                continue
            if dependency not in missing:
                missing[dependency] = []
            missing[dependency].append(name)
        dependency_map[name] = newdeps
    if not missing:
        return
    msglist = []
    for dependency, dependants in missing.items():
        msglist.append('%s (required by %s)' %
                       (dependency, ', '.join(dependants)))
    raise ConfigurationError(
        __package__,
        'Could not find the following dependencies:\n - ' +
        '\n - '.join(msglist))


def _sorted_dependency_map(dependency_map, operation):
    sorted_ = OrderedDict()
    graph = nx.DiGraph()
    for module, module_dependencies in dependency_map.items():
        if not module_dependencies:
            graph.add_edge(None, module)
        for dep in module_dependencies:
            graph.add_edge(dep, module)
    for loop in nx.simple_cycles(graph):
        raise DependencyLoop(__package__, operation, loop)
    for module in nx.topological_sort(graph):
        if module is None:
            continue
        sorted_[module] = dependency_map[module]
    return sorted_
