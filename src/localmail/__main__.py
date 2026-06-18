# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Enable `python -m localmail …`.

The console-script entry point (`localmail = localmail.cli:main`) is the
normal invocation, but the 2B.4 supervisor launches the daemon via
`sys.executable -m localmail run`, which does not depend on the script being
on PATH. This shim makes that work.
"""
from localmail.cli import main

if __name__ == "__main__":
    main()
