import bdb
import pdb
import sys
import traceback


def post_mortem(exc_type, exc_value, exc_tb):
    # has a good terminal and not in interactive mode
    has_usable_prompt = (getattr(sys, 'ps1', None) is None
                         and sys.stdin.isatty()
                         and sys.stdout.isatty()
                         and sys.stderr.isatty())
    skip_exc_types = (SyntaxError, bdb.BdbQuit, KeyboardInterrupt)

    if issubclass(exc_type, skip_exc_types) or not has_usable_prompt:
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    else:
        traceback.print_exception(exc_type, exc_value, exc_tb)
        print
        pdb.pm()


sys.excepthook = post_mortem
