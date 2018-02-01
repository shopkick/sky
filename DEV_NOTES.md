# Copyright (c) Shopkick 2017
# See LICENSE for details.
# Sky Development Notes

## Developing SKY

After git clone, point your system to the local sky instance using
`pip install -e path/to/sky` with a virtualenv activated.

## Set up for building on OS X

If you are developing sky itself and want to do a build:

```
/usr/bin/python -m virtualenv ./build/sky
./build/sky/bin/activate
./build/sky/bin/pip install tox
tox -e pex
```
