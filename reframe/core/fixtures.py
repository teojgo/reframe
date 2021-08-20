# Copyright 2016-2021 Swiss National Supercomputing Centre (CSCS/ETH Zurich)
# ReFrame Project Developers. See the top-level LICENSE file for details.
#
# SPDX-License-Identifier: BSD-3-Clause

#
# Functionality to use fixtures in ReFrame tests.
#

import copy
import itertools

import reframe.core.namespaces as namespaces
import reframe.core.runtime as runtime
import reframe.utility.udeps as udeps
from reframe.core.exceptions import ReframeSyntaxError
from reframe.core.variables import Undefined


class FixtureRegistry:
    '''Fixture registry.

    The fixture classes are stored as the keys in the dictionary, where
    the values are dictionaries holding all the variants for the given
    fixture class. These sub-dictionaries use the fixture's name keys and
    a tuple with the fixture variant index, the list of PEs and valid sys.

    :meta private:
    '''
    def __init__(self):
        self._reg = dict()

    def add(self, fixture, fid, branch, partitions, prog_envs):
        '''Add a fixture to the registry.

        This function handles the naming convention to avoid the clash when
        multiple tests use the same fixtures with the different scope levels.
        Fixtures steal the ``valid_systems`` and ``valid_prog_environs`` from
        the root test. How many of this get stolen from the root test depends
        on the fixture's scope level:
         - session: Only one PE+sys combination per fixture.
         - partition: Only one environment per partition.
         - environment: One test per available par-env combination.
         - test: Use the ``valid_systems`` and ``valid_prog_environs`` from the
           root test without any modification. Fixtures with this scope are not
           shared with any other tests, so their name contains the full tree
           branch leading up to the fixture.

        :param fixture: An instance of :class:`TestFixture`.
        :param fid: The variant index to instantiate the fixture with.
        :param branch: The branch the fixture belongs to.
        :param partitions: The system partitions supported by the root test.
        :param prog_envs: The valid programming environments from the root.
        '''

        test = fixture.test
        scope = fixture.scope
        fname = fixture.get_name(fid)
        reg_names = []
        self._reg.setdefault(test, dict())
        if scope == 'session':
            # The name is just the class name
            name = fname
            self._reg[test][name] = (fid, [prog_envs[0]], [partitions[0]])
            reg_names.append(name)
        elif scope == 'partition':
            for p in partitions:
                # The name contains the full partition name
                name = '_'.join([fname, p])
                self._reg[test][name] = (fid, [prog_envs[0]], [p])
                reg_names.append(name)
        elif scope == 'environment':
            for p in partitions:
                for env in prog_envs:
                    # The name contains the full part and env names
                    name = '_'.join([fname, p, env])
                    self._reg[test][name] = (fid, [env], [p])
                    reg_names.append(name)
        elif scope == 'test':
            # The name contains the full tree branch.
            name = '_'.join([fname, branch])
            self._reg[test][name] = (fid, list(prog_envs), list(partitions))
            reg_names.append(name)

        return reg_names

    def update(self, other):
        '''Join another registry into this one.'''
        self._is_registry(other)
        for test, variants in other._reg.items():
            self._reg.setdefault(test, dict())
            for name, args in variants.items():
                self._reg[test][name] = args

    def difference(self, other):
        '''Build a new registry with the elements from self not in other.'''
        self._is_registry(other)
        ret = FixtureRegistry()
        for test, variants in self._reg.items():
            if test in other._reg:
                other_variants = other._reg[test]
                for name, args in variants.items():
                    if name not in other_variants:
                        ret._reg.setdefault(test, dict())
                        ret._reg[test][name] = args
            else:
                ret._reg[test] = copy.deepcopy(variants)

        return ret

    def instantiate_all(self):
        '''Instantiate all the fixtures in the registry.'''
        ret = []
        for test, variants in self._reg.items():
            for name, args in variants.items():
                test_id, penv, part = args

                # Set the fixture name and stolen env and part from the parent
                test.name = name
                test.valid_prog_environs = penv
                test.valid_systems = part

                # Instantiate the fixture
                obj = test(_rfm_test_id=test_id)

                # Reset test defaults and append instance
                test.name = Undefined
                test.valid_prog_environs = Undefined
                test.valid_systems = Undefined
                ret.append(obj)
        return ret

    def _is_registry(self, other):
        if not isinstance(other, FixtureRegistry):
            raise TypeError('argument is not a FixtureRegistry')

    def __getitem__(self, test):
        '''Return all the fixture names registered from a given class.'''
        if test not in self:
            raise KeyError(f'{test.__qualname__} is not a registered fixture')
        else:
            return self._reg[test].keys()

    def __contains__(self, test):
        return test in self._reg


class TestFixture:
    '''Regression test fixture class.

    :meta private:
    '''

    def __init__(self, cls, scope='test'):
        # Can't use isinstance here because of circular deps.
        rfm_kind = getattr(cls, '_rfm_regression_class_kind', 0)
        if rfm_kind==0:
            raise ReframeSyntaxError(
                f"{cls.__qualname__!r} must be a derived class from "
                f"'RegressionTest'"
            )
        elif rfm_kind & 1:
            if scope in {'session', 'partition'}:
                raise ReframeSyntaxError(
                    f'incompatible scope for fixture {cls.__qualname__}; '
                    f'scope {scope!r} only supports run-only fixtures.'
                )

        if scope not in {'session', 'partition', 'environment', 'test'}:
            raise ReframeSyntaxError(
                f'invalid scope for fixture {cls.__qualname__} ({scope!r})'
            )

        self._cls = cls
        self._scope = scope

    @property
    def test(self):
        return self._cls

    @property
    def scope(self):
        return self._scope

    def get_name(self, variant_id=None):
        return self.test.fullname(variant_id)


class FixtureSpace(namespaces.Namespace):
    ''' Regression test fixture space.'''

    @property
    def local_namespace_name(self):
        return '_rfm_local_fixture_space'

    @property
    def namespace_name(self):
        return '_rfm_fixture_space'

    def __init__(self, target_cls=None, target_namespace=None):
        super().__init__(target_cls, target_namespace)

        self.__random_access_iter = tuple(x for x in iter(self))

    def join(self, other, cls):
        '''Join other fixture spaces into the current one.

        :param other: instance of the FixtureSpace class.
        :param cls: the target class.
        '''
        for key, value in other.fixtures.items():
            if key in self.fixtures:
                raise ReframeSyntaxError(
                    f'fixture space conflict: '
                    f'fixture {key!r} is defined in more than '
                    f'one base class of class {cls.__qualname__!r}'
                )

            self.fixtures[key] = value

    def extend(self, cls):
        local_fixture_space = getattr(cls, self.local_namespace_name)
        while local_fixture_space:
            name, fixture = local_fixture_space.popitem()
            self.fixtures[name] = fixture

        # If any previously declared fixture was defined in the class body
        # by directly assigning it a value, raise an error. Fixtures must be
        # changed using the `x = fixture(...)` syntax.
        for key, values in cls.__dict__.items():
            if key in self.fixtures:
                raise ReframeSyntaxError(
                    f'fixture {key!r} must be modified through the built-in '
                    f'fixture type'
                )

    def inject(self, obj, cls=None, fixture_index=None):
        if fixture_index is not None and fixture_index >= len(self):
            raise RuntimeError(
                f'fixture index out of range for '
                f'{obj.__class__.__qualname__}'
            )

        # Nothing to do if the fixture space is empty
        if not self.fixtures or fixture_index is None:
            return

        # Create the fixture registry
        obj._rfm_fixture_registry = FixtureRegistry()

        # Prepare the partitions and prog_envs
        try:
            part = tuple(obj.valid_systems)
        except AttributeError:
            raise ReframeSyntaxError(
                f'valid_systems is undefined in test {obj.name}'
            )
        else:
            rt = runtime.runtime()
            if '*' in part or rt.system.name in part:
                part = tuple(p.fullname for p in rt.system.partitions)

        try:
            prog_envs = tuple(obj.valid_prog_environs)
        except AttributeError:
            raise ReframeSyntaxError(
                f'valid_prog_environs is undefined in test {obj.name}'
            )
        else:
            if '*' in prog_envs:
                all_pes = set()
                for p in runtime.runtime().system.partitions:
                    for e in p.environs:
                        all_pes.add(e.name)
                prog_envs = tuple(all_pes)

        # Get the fixture indices
        fixture_idx = self[fixture_index]

        # Register the fixtures
        for name, fixture in self.fixtures.items():
            fid = fixture_idx[name]
            dep_names = obj._rfm_fixture_registry.add(fixture, fid, obj.name,
                                                      part, prog_envs)

            # Add dependencies
            if fixture.scope == 'session':
                dep_mode = udeps.fully
            elif fixture.scope == 'partition':
                dep_mode = udeps.by_part
            elif fixture.scope == 'environment':
                dep_mode = udeps.by_env
            else:
                dep_mode = udeps.by_case

            # Inject the dependency
            for name in dep_names:
                obj.depends_on(name, dep_mode)

    def __iter__(self):
        '''Walk through all index combinations for all fixtures.'''
        yield from itertools.product(
            *(list(range(f.test.num_variants)
            for f in self.fixtures.values()))
        )

    def __len__(self):
        l = 1
        for f in self.fixtures.values():
            l *= f.test.num_variants

        return l

    def __getitem__(self, key):
        '''Access an element in the fixture space.

        If the key is an integer, this function will retrieve a given
        combination of variant IDs for the fixtures present in this space.
        If the key is just a fixture name, this function will return the
        underlying fixture object with that name.
        '''
        if isinstance(key, int):
            ret = dict()
            f_ids = self.__random_access_iter[key]
            for i,f in enumerate(self.fixtures):
                ret[f] = f_ids[i]

            return ret

        return self.fixtures[key]

    @property
    def fixtures(self):
        return self._namespace
