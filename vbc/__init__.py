"""VBC (Video Batch Compression), new tree of the service split.

See docs/architecture/service-split.md. During the migration the working
application lives in ``vbc_legacy``; this package must not import it except
through the ``vbc run`` bridge in :mod:`vbc.cli`.
"""
