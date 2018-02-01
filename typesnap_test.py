# Copyright (c) Shopkick 2017
# See LICENSE for details.
import attr
import pytest

from opensky.typesnap import snap


def test():
    A = attr.make_class("A", ["b", "c"])
    B = attr.make_class("B", [])
    C = attr.make_class("C", [])
    # test types
    a = snap(dict(a=A, b=B, c=C))['a']
    assert isinstance(a.b, B) and isinstance(a.c, C)
    # test constants
    a = snap(dict(a=A, b='cat', c='dog'))['a']
    assert a.b == 'cat' and a.c == 'dog'
    # test failure
    A = attr.make_class("A", ["b"])
    B = attr.make_class("B", ["a"])
    with pytest.raises(ValueError):
        snap(dict(a=A, b=B))
    # test class constants
    A = attr.make_class("A", [])
    B = attr.make_class("B", ["a"])
    C = attr.make_class("C", ["a"])
    instances = snap(dict(a=A, b=B, c=C))
    assert instances['b'].a is instances['c'].a

