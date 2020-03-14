from abc import ABC, abstractmethod
import typing, datetime, collections, inspect
from dateutil import parser

from ..shared import client_utils

Token = typing.NewType("Token", str)
Tag = typing.NewType("Tag", str)
UserId = typing.NewType("UserId", str)
PipelineId = typing.NewType("PipelineId", str)
OrgId = typing.NewType("OrgId", int)
TaskId = typing.NewType("TaskId", str)
InstanceId = typing.NewType("InstanceId", str)

def runtime_type(typ):
    """Find the real, underlying Python type"""
    if isinstance(typ, type):
        return typ
    elif is_NewType(typ):
        return runtime_type(typ.__supertype__)
    elif isinstance(typ, typing._GenericAlias):
        return runtime_type(typ.__origin__)
    # TODO: (kzhang) support for `typing.TypeVar`
    return typ


def is_instance(obj, typ):
    """Instance check against a given typ, which can be a proper "Python" type or a "typing" type"""
    if typ == inspect._empty or isinstance(typ, typing.TypeVar):
        # TODO: (kzhang) for now, pass all checks against `typing.TypeVar`. To be complete
        # we should check `obj` against `TypeVar.__constraints__`
        return True
    elif isinstance(typ, type): # ex: `str`
        return isinstance(obj, typ)
    elif isinstance(typ, typing._GenericAlias): # ex: `typing.List[int]`
        # TODO: (kzhang) add support for typing.Set|Dict|etc. ?
        if typ.__origin__ != list:
            raise TypeError(f'Only typing.List[T] is allowed, got {typ}')
        if not isinstance(obj, list):
            return False
        item_type = typ.__args__[0]
        return all(is_instance(o, item_type) for o in obj)
    elif is_NewType(typ): # ex: `typing.NewType('MyId', str)`
        return is_instance(obj, typ.__supertype__)
    else:
        raise TypeError(f'Invalid type annotation {typ}/{type(type)}')


def is_NewType(typ):
    """Checks whether the given `typ` was produced by `typing.NewType`"""
    # @see typing.py:NewType
    return inspect.isfunction(typ) and hasattr(typ, '__name__') and hasattr(typ, '__supertype__')


class SpecialFunc(ABC):
    """Decorator used to indicate special roles for member/static methods.

    ex:
        @CmdLineDeserializer
        def from_str(obj_str):
            ...

    will turn `from_str` into a callable `CmdLineDeserializer` instance which is
    a subtype of `SpecialFunc`. Clients can use this property to infer special
    behavior. The resulting member/static functions should have no differences
    in behavior, aside from the fact that they are now `SpecialFunc`s.
    """
    def __init__(self, func):
        self._validate(func)
        self._func = func
        # At `@decorator` time, we only have a context-less function, which will
        # be lazily bound when `__get__` is called. This is especially important
        # for member functions, which require a reference to `self`.
        self._bound_func = None

    @abstractmethod
    def _validate(self, function):
        pass

    def __call__(self, *args, **kwargs):
        return self._bound_func(*args, **kwargs)

    def __get__(self, obj, typ):
        self._bound_func = self._func.__get__(obj, typ)
        return self


class CmdLineSerializer(SpecialFunc):
    """Tags a non-static member function as the serializer for the containing class"""
    def _validate(self, function):
        assert inspect.isfunction(function), f'Expecting function, got {function}'


class CmdLineDeserializer(SpecialFunc):
    """Tags a static function as the de-serializer for the containing class"""
    def _validate(self, function):
        assert isinstance(function, staticmethod), f'Expecting staticmethod, got {function}'


def _serializer(obj):
    serializers = inspect.getmembers(obj, lambda m: isinstance(m, CmdLineSerializer))
    if serializers:
        _, func = client_utils.getOnly(serializers)
        return func

    if type(obj) == list:
        return lambda: LIST_DELIM.join(map(serialize, obj))
    return obj.__str__


def _deserializer(typ):
    deserializers = inspect.getmembers(typ, lambda m: isinstance(m, CmdLineDeserializer))
    if deserializers:
        _, func = client_utils.getOnly(deserializers)
        return func
    else:
        return typ


def serialize(obj):
    return _serializer(obj)()


def deserialize(typ, obj_str):
    if typ is type(None):
        if obj_str is None or isinstance(obj_str, str):
            return obj_str
        raise ValueError(f"deserialize with type=NoneType expected None or str, but got {repr(obj_str)}")
    return _deserializer(typ)(obj_str)


# Wrapper types with specialized de-serialization logic. These types cannot
# actually be instantiated, as their `__new__` functions return an instance
# of the type they represent, not themselves. For instance, `Bool('true')`
# returns a `bool`, not a `Bool`.
class Bool(int):  # we cannot subclass `bool`, use next-best option
    def __new__(cls, val):
        return val is not None and str(val).strip().lower() not in ['', '0', 'none', 'false', 'f', '0.0']


class Datetime_Date(datetime.date):
    def __new__(cls, date_str):
        assert isinstance(
            date_str, str), 'input is not a string: {} - {}'.format(date_str, type(date_str))
        # Will work with various types of inputs such as:
        #   - '2019-03-11'
        #   - '20190311'
        #   - 'march 11, 2019'
        dt = parser.parse(date_str)
        if dt.time() != datetime.datetime.min.time():
            raise ValueError("Interpreting input as a date, but got non-zero "
                             "time component: {} -> {}".format(date_str, dt))
        return dt.date()


# NOTE (kzhang):
# When I first wrote this, I thought it'd be nice to have a wrapper type that is a real `type`
# so we can do type comparisons (ex: `issubclass()`). I didn't actually end up
# running type comparisons with these, so this design may be too complex for what
# it provides. Changing it back to the old model is easy, however, and I can
# do that at any time.

# This is a metaclass for dynamically creating `List[T]` class types. Usage:
# - List('12,34') = ['12', '34'] // <class 'list'> (*not* List)
# - issubclass(List, list) = True
#
# - List[int]('12,34') = [12, 34] // <class 'list'> (*not* List)
# - issubclass(List[int], List) = True
#
# - List[123] => err (not a valid type param)
# - List[list] => err (cannot have nested iterables)
# - List[int][str] => err (cannot parameterize again)
class _Meta_List(type):
    _cache = dict()

    def _type_err(err, typ):
        raise TypeError(
            f'A {List.__name__}[T] {err}. Got T = {typ} - {type(typ)}')

    def __getitem__(cls, typ):  # `typ` is the `T` in `List[T]`
        if cls != List:
            raise TypeError(
                'Cannot parameterized already-parameterized list. Did you call List[S][T]?')
        # TODO (kzhang): Add support for custom typing.X types
        if not isinstance(typ, type):
            _Meta_List._type_err(
                'must be parameterized with a valid Python type.', typ)
        if typ != str and issubclass(typ, collections.abc.Iterable):
            _Meta_List._type_err(
                'cannot be parameterized with an Iterable type.', typ)
        if typ not in cls._cache:
            # using `type` as a programmatic class factory
            cls._cache[typ] = type(
                f'{List.__name__}[{typ.__name__}]',  # name
                (List,),  # parent class
                # TODO: (kzhang) The _de_serializer is hidden under a lambda so that this `List[T]` type
                # does not have a member with type `CmdLineDeserializer`, which would otherwise interfere
                # with the cmd-line parsers when de-serializing this type. The de-serializing function is
                # only intended for the contained items, not the `List[T]` itself.
                dict(_de_serializer=lambda obj_str: _deserializer(typ)(obj_str)))  # attributes
        return cls._cache[typ]

# base list type with configurable de-serializer


class List(list, metaclass=_Meta_List):
    _de_serializer = str

    def __new__(cls, list_str):
        assert isinstance(
            list_str, str), 'list input is not a string: {} - {}'.format(list_str, type(list_str))
        # cls can be `List` or some `List[T]` if we are using a class created by `_Meta_List`
        res = []
        for token in list_str.split(LIST_DELIM):
            try:
                res.append(cls._de_serializer(token))
            except Exception as e:
                raise TypeError(f'An error occured while using the de-serializer '
                                f'{cls._de_serializer} on string "{token}"', e)
        return res


LIST_DELIM = ','