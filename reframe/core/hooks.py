# Copyright 2016-2021 Swiss National Supercomputing Centre (CSCS/ETH Zurich)
# ReFrame Project Developers. See the top-level LICENSE file for details.
#
# SPDX-License-Identifier: BSD-3-Clause

import contextlib
import functools
import inspect

import reframe.utility as util


def _runx(phase):
    '''Backend function to attach a hook to a given phase.

    :meta private:
    '''
    def deco(func):
        if hasattr(func, '_rfm_attach'):
            func._rfm_attach.append(phase)
        else:
            func._rfm_attach = [phase]

        try:
            # no need to resolve dependencies independently; this function is
            # already attached to a different phase
            func._rfm_resolve_deps = False
        except AttributeError:
            pass

        @functools.wraps(func)
        def _fn(*args, **kwargs):
            func(*args, **kwargs)

        return _fn

    return deco


# Valid pipeline stages that users can specify in the `run_before()` and
# `run_after()` decorators
_USER_PIPELINE_STAGES = (
    'init', 'setup', 'compile', 'run', 'sanity', 'performance', 'cleanup'
)


def run_before(stage):
    '''Decorator for attaching a test method to a pipeline stage.

    The method will run just before the specified pipeline stage and it should
    not accept any arguments except ``self``.

    This decorator can be stacked, in which case the function will be attached
    to multiple pipeline stages.

    The ``stage`` argument can be any of ``'setup'``, ``'compile'``,
    ``'run'``, ``'sanity'``, ``'performance'`` or ``'cleanup'``.

    '''
    if stage not in _USER_PIPELINE_STAGES:
        raise ValueError(f'invalid pipeline stage specified: {stage!r}')

    if stage == 'init':
        raise ValueError('pre-init hooks are not allowed')

    return _runx('pre_' + stage)


def run_after(stage):
    '''Decorator for attaching a test method to a pipeline stage.

    This is analogous to the :py:attr:`~reframe.core.decorators.run_before`,
    except that ``'init'`` can also be used as the ``stage`` argument. In this
    case, the hook will execute right after the test is initialized (i.e.
    after the :func:`__init__` method is called), before entering the test's
    pipeline. In essence, a post-init hook is equivalent to defining
    additional :func:`__init__` functions in the test. All the other
    properties of pipeline hooks apply equally here. The following code

    .. code-block:: python

       @rfm.run_after('init')
       def foo(self):
           self.x = 1


    is equivalent to

    .. code-block:: python

       def __init__(self):
           self.x = 1

    .. versionchanged:: 3.5.2
       Add the ability to define post-init hooks in tests.

    '''

    if stage not in _USER_PIPELINE_STAGES:
        raise ValueError(f'invalid pipeline stage specified: {stage!r}')

    # Map user stage names to the actual pipeline functions if needed
    if stage == 'init':
        stage = '__init__'
    elif stage == 'compile':
        stage = 'compile_wait'
    elif stage == 'run':
        stage = 'run_wait'

    return _runx('post_' + stage)


def require_deps(func):
    '''Denote that the decorated test method will use the test dependencies.

    The arguments of the decorated function must be named after the
    dependencies that the function intends to use. The decorator will bind the
    arguments to a partial realization of the
    :func:`reframe.core.pipeline.RegressionTest.getdep` function, such that
    conceptually the new function arguments will be the following:

    .. code-block:: python

       new_arg = functools.partial(getdep, orig_arg_name)

    The converted arguments are essentially functions accepting a single
    argument, which is the target test's programming environment.

    Additionally, this decorator will attach the function to run *after* the
    test's setup phase, but *before* any other "post_setup" pipeline hook.

    This decorator is also directly available under the :mod:`reframe` module.

    .. versionadded:: 2.21

    '''

    tests = inspect.getfullargspec(func).args[1:]
    func._rfm_resolve_deps = True

    @functools.wraps(func)
    def _fn(obj, *args):
        newargs = [functools.partial(obj.getdep, t) for t in tests]
        func(obj, *newargs)

    return _fn


def attach_hooks(hooks):
    '''Attach pipeline hooks to phase ``name''.

    This function returns a decorator for pipeline functions that will run the
    registered hooks before and after the function.

    If ``name'' is :class:`None`, both pre- and post-hooks will run, otherwise
    only the hooks of the phase ``name'' will be executed.
    '''

    def _deco(func):
        def select_hooks(obj, kind):
            phase = kind + func.__name__
            if phase not in hooks:
                return []

            return [h for h in hooks[phase]
                    if h.__name__ not in obj._disabled_hooks]

        @functools.wraps(func)
        def _fn(obj, *args, **kwargs):
            for h in select_hooks(obj, 'pre_'):
                getattr(obj, h.__name__)()

            func(obj, *args, **kwargs)
            for h in select_hooks(obj, 'post_'):
                getattr(obj, h.__name__)()

        return _fn

    return _deco


class Hook:
    '''A pipeline hook.

    This is essentially a function wrapper that hashes the functions by name,
    since we want hooks to be overriden by name in subclasses.
    '''

    def __init__(self, fn):
        self.__fn = fn

    def __getattr__(self, attr):
        return getattr(self.__fn, attr)

    @property
    def fn(self):
        return self.__fn

    def __hash__(self):
        return hash(self.__name__)

    def __eq__(self, other):
        if not isinstance(other, type(self)):
            return NotImplemented

        return self.__name__ == other.__name__

    def __call__(self, *args, **kwargs):
        return self.__fn(*args, **kwargs)

    def __repr__(self):
        return repr(self.__fn)


class HookRegistry:
    '''Global hook registry.'''

    @classmethod
    def create(cls, namespace):
        '''Create a hook registry from a class namespace.

        Hook functions have an `_rfm_attach` attribute that specify the stages
        of the pipeline where they must be attached. Dependencies will be
        resolved first in the post-setup phase if not assigned elsewhere.
        '''

        local_hooks = {}
        fn_with_deps = []
        for v in namespace.values():
            if hasattr(v, '_rfm_attach'):
                for phase in v._rfm_attach:
                    try:
                        local_hooks[phase].append(Hook(v))
                    except KeyError:
                        local_hooks[phase] = [Hook(v)]

            with contextlib.suppress(AttributeError):
                if v._rfm_resolve_deps:
                    fn_with_deps.append(Hook(v))

        if fn_with_deps:
            local_hooks['post_setup'] = (
                fn_with_deps + local_hooks.get('post_setup', [])
            )

        return cls(local_hooks)

    def __init__(self, hooks=None):
        self.__hooks = {}
        if hooks is not None:
            self.update(hooks)

    def __getitem__(self, key):
        return self.__hooks[key]

    def __setitem__(self, key, name):
        self.__hooks[key] = name

    def __contains__(self, key):
        return key in self.__hooks

    def __getattr__(self, name):
        return getattr(self.__hooks, name)

    def update(self, hooks):
        for phase, hks in hooks.items():
            self.__hooks.setdefault(phase, util.OrderedSet())
            for h in hks:
                self.__hooks[phase].add(h)

    def __repr__(self):
        return repr(self.__hooks)
