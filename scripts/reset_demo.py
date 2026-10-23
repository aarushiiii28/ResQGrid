"""Wipe all demo data (including the audit log) and reseed."""

from _common import main

if __name__ == "__main__":
    main(reset=True)
