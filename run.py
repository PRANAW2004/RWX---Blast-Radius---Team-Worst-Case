# Entry point: importing selector lets Python use its precompiled bytecode.
import sys

sys.path.insert(0, "/app")
import selector  # noqa: E402

selector.main()
