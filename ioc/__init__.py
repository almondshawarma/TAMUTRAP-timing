"""TAMUTRAP timing EPICS IOC.

Making ``ioc`` a package (rather than a loose script) is only so that
``ioc.timing_ioc:main`` can be a console entry point declared in
``pyproject.toml``, keeping the IOC in its own directory instead of moving
it under ``src/``. Running it directly (``python ioc/timing_ioc.py``) still
works as before.
"""
