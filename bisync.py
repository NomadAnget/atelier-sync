"""Compatibility entry point. Bidirectional-main synchronization has been removed."""
from coordinator import main
if __name__ == "__main__":
    raise SystemExit(main())

