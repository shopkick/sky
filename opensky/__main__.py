# Copyright (c) Shopkick 2017
# See LICENSE for details.
if __name__ != "__main__":
    raise ImportError

import sys
from . import cmd

sys.exit(cmd.main())
