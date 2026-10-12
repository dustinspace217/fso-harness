"""Allow `python -m fsoctl`."""
import sys

from .cli import main

sys.exit(main())
