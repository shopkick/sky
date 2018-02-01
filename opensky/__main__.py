if __name__ != "__main__":
    raise ImportError

import sys
from . import cmd

sys.exit(cmd.main())
