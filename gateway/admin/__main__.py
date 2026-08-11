"""Allows ``python -m gateway.admin`` as an alternative to the ``msg-admin`` script."""

from gateway.admin.cli import main

if __name__ == "__main__":
    main()
