"""Allows ``python -m gateway.admin`` as an alternative to the ``acg-provision`` script."""

from gateway.admin.cli import main

if __name__ == "__main__":
    main()
